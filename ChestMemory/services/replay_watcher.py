"""Automatic Fortnite `.replay` directory monitor with write-completion stabilization.

Uses `watchdog` (Windows ReadDirectoryChangesW) when installed, with a resilient
polling fallback so monitoring remains reliable across network drives, synced
folders, and headless test environments.

Before notifying listeners that a replay is ready, `ReplayWatcher` verifies:
1. File exists and has non-zero size.
2. Size and modification timestamp (`st_mtime_ns`) have remained unchanged for
   at least `stable_seconds` (ensuring Fortnite finished flushing disk writes).
3. File can be opened for reading without OS lock errors.
4. Replay binary header does NOT have `IsLive == True` (when a valid UE magic
   header is present).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from detector.replay_parser import detect_default_fortnite_replay_dir, is_replay_live

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except ImportError:  # pragma: no cover - fallback when watchdog isn't installed
    FileSystemEvent = object  # type: ignore[misc, assignment]
    FileSystemEventHandler = object  # type: ignore[misc, assignment]
    Observer = None  # type: ignore[misc, assignment]
    WATCHDOG_AVAILABLE = False


@dataclass(slots=True)
class _PendingFile:
    path: Path
    last_size: int
    last_mtime_ns: int
    last_change_monotonic: float


class ReplayWatcher:
    """Monitors Fortnite's Demos folder and emits stabilized `.replay` file paths."""

    def __init__(
        self,
        watch_dir: str | Path | None,
        on_replay_ready: Callable[[Path], None],
        on_status_change: Optional[Callable[[str, str], None]] = None,
        stable_seconds: float = 4.0,
        poll_interval: float = 0.5,
    ) -> None:
        self.watch_dir: Path = (
            Path(watch_dir) if watch_dir else detect_default_fortnite_replay_dir()
        )
        self._on_replay_ready = on_replay_ready
        self._on_status_change = on_status_change
        self.stable_seconds = max(0.05, float(stable_seconds))
        self.poll_interval = max(0.05, float(poll_interval))

        self._lock = threading.RLock()
        self._pending: dict[Path, _PendingFile] = {}
        self._emitted_signatures: dict[Path, tuple[int, int]] = {}
        self._stop_event = threading.Event()
        self._observer = None
        self._worker_thread: Optional[threading.Thread] = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self, scan_existing: bool = True) -> bool:
        """Start directory observation and stabilization worker thread."""
        with self._lock:
            if self._running:
                return True

            if not self.watch_dir.exists():
                self._emit_status(
                    "warning",
                    f"Папка реплеев не найдена: {self.watch_dir}",
                )
                return False

            self._stop_event.clear()
            self._running = True

            if scan_existing:
                self.enqueue_existing_files()

            if WATCHDOG_AVAILABLE and Observer is not None:
                handler = self._build_watchdog_handler()
                observer = Observer()
                observer.schedule(handler, str(self.watch_dir), recursive=False)
                observer.daemon = True
                observer.start()
                self._observer = observer

            self._worker_thread = threading.Thread(
                target=self._stabilizer_loop,
                name="ChestMemory-ReplayStabilizer",
                daemon=True,
            )
            self._worker_thread.start()
            self._emit_status("active", f"Мониторинг: {self.watch_dir}")
            return True

    def stop(self) -> None:
        """Stop filesystem observer and stabilizer thread cleanly."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._stop_event.set()
            observer = self._observer
            self._observer = None
            worker = self._worker_thread
            self._worker_thread = None

        if observer is not None:
            try:
                observer.stop()
                observer.join(timeout=2.0)
            except Exception:
                pass
        if worker is not None and worker.is_alive():
            worker.join(timeout=2.0)

    def enqueue_existing_files(self) -> list[Path]:
        """Discover all `.replay` files currently in `watch_dir` and queue them."""
        found: list[Path] = []
        if not self.watch_dir.is_dir():
            return found
        for item in sorted(
            self.watch_dir.glob("*.replay"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
        ):
            self.mark_candidate(item)
            found.append(item)
        return found

    def mark_candidate(self, file_path: str | Path) -> None:
        """Track a newly created or modified `.replay` file until it stabilizes."""
        path = Path(file_path)
        if path.suffix.lower() != ".replay":
            return
        if not path.is_file():
            return
        try:
            st = path.stat()
        except OSError:
            return

        now = time.monotonic()
        with self._lock:
            sig = (st.st_size, st.st_mtime_ns)
            if self._emitted_signatures.get(path) == sig:
                return
            existing = self._pending.get(path)
            if existing is None or existing.last_size != st.st_size or existing.last_mtime_ns != st.st_mtime_ns:
                self._pending[path] = _PendingFile(
                    path=path,
                    last_size=st.st_size,
                    last_mtime_ns=st.st_mtime_ns,
                    last_change_monotonic=now,
                )

    def _build_watchdog_handler(self):
        watcher = self

        class _Handler(FileSystemEventHandler):  # type: ignore[valid-type, misc]
            def on_created(self, event: FileSystemEvent) -> None:
                if not getattr(event, "is_directory", False):
                    watcher.mark_candidate(getattr(event, "src_path", ""))

            def on_modified(self, event: FileSystemEvent) -> None:
                if not getattr(event, "is_directory", False):
                    watcher.mark_candidate(getattr(event, "src_path", ""))

            def on_moved(self, event: FileSystemEvent) -> None:
                if not getattr(event, "is_directory", False):
                    watcher.mark_candidate(getattr(event, "dest_path", ""))

        return _Handler()

    def _stabilizer_loop(self) -> None:
        """Poll pending candidates (and scan dir if watchdog isn't active) until stable."""
        last_dir_scan = 0.0
        while not self._stop_event.is_set():
            now = time.monotonic()

            # Periodically scan directory as backup (especially if watchdog is unavailable)
            scan_period = self.poll_interval if not WATCHDOG_AVAILABLE else max(5.0, self.poll_interval * 10)
            if (now - last_dir_scan) >= scan_period:
                self._scan_directory_silently()
                last_dir_scan = now

            ready_paths: list[Path] = []
            with self._lock:
                for path, entry in list(self._pending.items()):
                    if not path.is_file():
                        self._pending.pop(path, None)
                        continue
                    try:
                        st = path.stat()
                    except OSError:
                        continue

                    if st.st_size == 0:
                        entry.last_change_monotonic = now
                        continue

                    if st.st_size != entry.last_size or st.st_mtime_ns != entry.last_mtime_ns:
                        entry.last_size = st.st_size
                        entry.last_mtime_ns = st.st_mtime_ns
                        entry.last_change_monotonic = now
                        continue

                    if (now - entry.last_change_monotonic) < self.stable_seconds:
                        continue

                    if not self._is_file_readable_and_finalized(path):
                        entry.last_change_monotonic = now
                        continue

                    self._pending.pop(path, None)
                    self._emitted_signatures[path] = (st.st_size, st.st_mtime_ns)
                    ready_paths.append(path)

            for path in ready_paths:
                if self._stop_event.is_set():
                    break
                try:
                    self._on_replay_ready(path)
                except Exception as exc:  # pragma: no cover - defensive guard
                    self._emit_status("error", f"Ошибка обработки {path.name}: {exc}")

            self._stop_event.wait(self.poll_interval)

    def _scan_directory_silently(self) -> None:
        if not self.watch_dir.is_dir():
            return
        try:
            for item in self.watch_dir.glob("*.replay"):
                self.mark_candidate(item)
        except OSError:
            pass

    @staticmethod
    def _is_file_readable_and_finalized(path: Path) -> bool:
        """Check file can be read and is not flagged `IsLive=True` by Fortnite."""
        try:
            with path.open("rb") as handle:
                _ = handle.read(64)
        except OSError:
            return False

        live_flag = is_replay_live(path)
        if live_flag is True:
            return False
        return True

    def _emit_status(self, level: str, message: str) -> None:
        if self._on_status_change is not None:
            try:
                self._on_status_change(level, message)
            except Exception:
                pass
