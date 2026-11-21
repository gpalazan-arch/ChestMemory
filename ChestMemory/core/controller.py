"""Central application controller orchestrating detection, persistence, backups, and UI updates."""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from core.counter import ChestCounter
from core.statistics import StatisticsService
from database.database import DatabaseManager
from database.models import (
    AnalysisResult,
    AnalysisStatus,
    AppConfig,
    AttributionMode,
    ChestEventRecord,
    DetectionSource,
    FailedAnalysisRecord,
    MatchRecord,
    StatsSnapshot,
)
from detector.replay_analyzer import ReplayAnalyzer
from detector.replay_parser import compute_file_sha256, detect_default_fortnite_replay_dir
from detector.visual_detector import (
    LiveVisualMonitor,
    VisualChestDetector,
    VisualDiagnostics,
)
from services.backup_service import BackupInfo, BackupService
from services.replay_watcher import ReplayWatcher


@dataclass(slots=True)
class StatusEvent:
    """Status indicator payload shown in the ChestMemory footer bar."""

    level: str  # "active", "analyzing", "warning", "error", "idle"
    label: str  # Short uppercase badge text, e.g. "MONITORING ACTIVE"
    detail: str = ""  # Human-readable tooltip / detail message


class ChestMemoryController:
    """Framework-agnostic application controller driving ChestMemory end-to-end."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "chestmemory.db"
        self.backup_dir = self.data_dir / "backups"

        self.backup_service = BackupService(self.db_path, self.backup_dir)
        self._recover_database_if_corrupted()

        self.db = DatabaseManager(self.db_path)
        self.config: AppConfig = self.db.load_config()
        if not self.config.replay_dir:
            self.config.replay_dir = str(detect_default_fortnite_replay_dir())
            self.db.save_config(self.config)

        self.backup_service.retention_count = max(1, self.config.backup_retention_count)
        self.stats_service = StatisticsService(self.db)
        initial_stats = self.stats_service.get_snapshot()
        self.counter = ChestCounter(initial_total=initial_stats.total_chests)

        self.replay_analyzer = ReplayAnalyzer(
            oodle_dll_path=self.config.oodle_dll_path or None,
            external_parser_cmd=self.config.external_parser_cmd,
        )
        self.visual_detector = VisualChestDetector()

        self._watcher: Optional[ReplayWatcher] = None
        self._live_visual: Optional[LiveVisualMonitor] = None
        self._live_visual_events: list[ChestEventRecord] = []
        self._live_visual_session_id: Optional[str] = None
        self._live_visual_last_event_mono: float = 0.0
        self._visual_black_alerted: bool = False
        self._lock = threading.RLock()

        # UI listener callbacks (connected to Qt Signals in PySide6 interface)
        self._stats_listeners: list[Callable[[StatsSnapshot], None]] = []
        self._status_listeners: list[Callable[[StatusEvent], None]] = []
        self._match_listeners: list[Callable[[MatchRecord], None]] = []
        self._error_listeners: list[Callable[[str, str], None]] = []

        self._current_status = StatusEvent(
            level="idle",
            label="READY",
            detail="Ожидание запуска мониторинга",
        )

    def _recover_database_if_corrupted(self) -> None:
        if not self.db_path.exists():
            return
        ok, _msg = self.backup_service.verify_sqlite_file(self.db_path)
        if not ok:
            self.backup_service.restore_latest_valid_backup()

    # ------------------------------------------------------------------
    # Listener registration
    # ------------------------------------------------------------------
    def on_stats_updated(self, callback: Callable[[StatsSnapshot], None]) -> None:
        self._stats_listeners.append(callback)

    def on_status_changed(self, callback: Callable[[StatusEvent], None]) -> None:
        self._status_listeners.append(callback)

    def on_match_recorded(self, callback: Callable[[MatchRecord], None]) -> None:
        self._match_listeners.append(callback)

    def on_analysis_error(self, callback: Callable[[str, str], None]) -> None:
        self._error_listeners.append(callback)

    @property
    def current_status(self) -> StatusEvent:
        return self._current_status

    def get_stats(self) -> StatsSnapshot:
        return self.stats_service.get_snapshot()

    def get_recent_matches(self, limit: int = 100) -> list[MatchRecord]:
        return self.stats_service.get_recent_matches(limit=limit)

    def get_failed_analyses(self, limit: int = 50) -> list[FailedAnalysisRecord]:
        return self.db.get_failed_analyses(limit=limit)

    def get_visual_diagnostics(self) -> VisualDiagnostics:
        with self._lock:
            visual = self._live_visual
        if visual is None:
            return VisualDiagnostics(running=False)
        return visual.diagnostics

    # ------------------------------------------------------------------
    # Monitoring lifecycle
    # ------------------------------------------------------------------
    def start_monitoring(self) -> bool:
        """Start automatic replay watching and/or visual CV monitoring per config."""
        with self._lock:
            self.stop_monitoring()
            mode = self.config.detection_mode
            started_any = False

            if mode in ("replay", "hybrid"):
                self._watcher = ReplayWatcher(
                    watch_dir=self.config.replay_dir,
                    on_replay_ready=self._handle_replay_file,
                    on_status_change=self._handle_watcher_status,
                    stable_seconds=self.config.file_stable_seconds,
                )
                if self._watcher.start(scan_existing=self.config.auto_scan_existing):
                    started_any = True

            if mode in ("visual", "hybrid"):
                self._visual_black_alerted = False
                self._live_visual = LiveVisualMonitor(
                    on_chest_opened=self._handle_live_visual_chest,
                    on_error=self._handle_visual_error,
                    on_diagnostics=self._handle_visual_diagnostics,
                    fps=self.config.visual_fps,
                    monitor_index=self.config.visual_monitor_index,
                )
                if self._live_visual.start():
                    started_any = True

            if started_any:
                label = (
                    "MONITORING ACTIVE"
                    if mode != "visual"
                    else "VISUAL CV ACTIVE"
                )
                self._emit_status(
                    StatusEvent(
                        level="active",
                        label=label,
                        detail=f"Режим: {mode} | Папка: {self.config.replay_dir}",
                    )
                )
            else:
                self._emit_status(
                    StatusEvent(
                        level="warning",
                        label="REPLAY FOLDER MISSING",
                        detail=f"Укажите папку реплеев Fortnite в настройках ({self.config.replay_dir})",
                    )
                )
            return started_any

    def stop_monitoring(self) -> None:
        """Stop all background monitors cleanly."""
        with self._lock:
            watcher = self._watcher
            self._watcher = None
            visual = self._live_visual
            self._live_visual = None

        if watcher is not None:
            watcher.stop()
        if visual is not None:
            visual.stop()
        self._emit_status(StatusEvent(level="idle", label="MONITORING PAUSED"))

    def scan_replay_directory_now(self) -> int:
        """Manually scan the configured replay folder for unprocessed `.replay` files."""
        replay_dir = Path(self.config.replay_dir)
        if not replay_dir.is_dir():
            self._emit_status(
                StatusEvent(
                    level="warning",
                    label="REPLAY FOLDER MISSING",
                    detail=f"Папка не найдена: {replay_dir}",
                )
            )
            return 0

        processed = 0
        for path in sorted(replay_dir.glob("*.replay"), key=lambda p: p.stat().st_mtime):
            res = self.process_replay_file(path)
            if res is not None and res.is_reliable:
                processed += 1
        return processed

    # ------------------------------------------------------------------
    # Replay & Visual processing
    # ------------------------------------------------------------------
    def _handle_replay_file(self, path: Path) -> None:
        self.process_replay_file(path)

    def process_replay_file(
        self,
        file_path: str | Path,
        force_retry_failed: bool = False,
    ) -> Optional[AnalysisResult]:
        """Analyze a `.replay` file, enforce deduplication, and persist verified counts."""
        path = Path(file_path)
        if not path.is_file():
            return None

        file_hash = compute_file_sha256(path)
        fallback_id = f"sha256:{file_hash[:32]}"

        if self.db.is_replay_processed(fallback_id, file_hash):
            return None
        if not force_retry_failed and self.db.is_failed_hash_recorded(file_hash):
            return None

        self._emit_status(
            StatusEvent(
                level="analyzing",
                label="ANALYZING REPLAY...",
                detail=f"Анализ {path.name}",
            )
        )

        result = self.replay_analyzer.analyze_file(path)

        # Deduplicate also by parsed Match GUID (even if file was copied/renamed)
        if self.db.is_replay_processed(result.replay_id, result.file_hash):
            self._restore_monitoring_status()
            return result

        # If replay binary stream could not be decoded in hybrid mode, check whether
        # LiveVisualMonitor captured verified chest openings during this match!
        if not result.is_reliable and self.config.detection_mode == "hybrid":
            with self._lock:
                visual_events = list(self._live_visual_events)
                if visual_events:
                    self._live_visual_events.clear()
                    self.counter.reset_live_match()
                    result = AnalysisResult(
                        status=AnalysisStatus.SUCCESS,
                        replay_id=result.replay_id,
                        file_path=str(path),
                        file_hash=file_hash,
                        chests_opened=len(visual_events),
                        source=DetectionSource.VISUAL_CV,
                        attribution_mode=AttributionMode.VISUAL_LOCAL_HUD,
                        match_timestamp=result.match_timestamp or datetime.now(timezone.utc),
                        match_duration_ms=result.match_duration_ms,
                        friendly_name=result.friendly_name or path.stem,
                        engine_version=f"{result.engine_version} + VisualCV",
                        eliminations=result.eliminations,
                        placement=result.placement,
                        events=visual_events,
                    )

        if result.is_reliable:
            # Clear live visual buffer when authoritative replay succeeded
            with self._lock:
                self._live_visual_events.clear()
                self.counter.reset_live_match()

            inserted, match = self.db.save_analysis_result(result)
            if inserted and match is not None:
                self._after_match_saved(match)
            self._restore_monitoring_status()
            return result

        # Unreliable / failed analysis: NEVER record 0 as a valid match!
        if result.status != AnalysisStatus.LIVE_REPLAY:
            self.db.record_failed_analysis(result)
            self._emit_error(path.name, result.error_message)
            self._emit_status(
                StatusEvent(
                    level="error",
                    label="ANALYSIS ERROR",
                    detail=result.error_message,
                )
            )
        else:
            self._restore_monitoring_status()
        return result

    def analyze_video_file(self, video_path: str | Path) -> AnalysisResult:
        """Analyze a recorded Fortnite gameplay video with OpenCV and persist if verified."""
        path = Path(video_path)
        self._emit_status(
            StatusEvent(
                level="analyzing",
                label="ANALYZING VIDEO...",
                detail=f"CV анализ {path.name}",
            )
        )
        result = self.visual_detector.analyze_video(path, sample_fps=self.config.visual_fps)
        if result.is_reliable:
            inserted, match = self.db.save_analysis_result(result)
            if inserted and match is not None:
                self._after_match_saved(match)
            self._restore_monitoring_status()
        else:
            self._emit_error(path.name, result.error_message)
            self._emit_status(
                StatusEvent(
                    level="error",
                    label="CV ANALYSIS ERROR",
                    detail=result.error_message,
                )
            )
        return result

    def commit_live_visual_session(self, session_label: str = "Live CV Match") -> Optional[MatchRecord]:
        """Persist accumulated live OpenCV chest openings as a verified match record."""
        with self._lock:
            events = list(self._live_visual_events)
            if not events:
                return None
            self._live_visual_events.clear()
            self.counter.reset_live_match()

        now = datetime.now(timezone.utc)
        raw_sig = f"live:{now.isoformat()}:{len(events)}:{events[-1].timestamp_ms}".encode("utf-8")
        digest = hashlib.sha256(raw_sig).hexdigest()
        result = AnalysisResult(
            status=AnalysisStatus.SUCCESS,
            replay_id=f"visual:{digest[:24]}",
            file_path=f"{session_label}.cv",
            file_hash=digest,
            chests_opened=len(events),
            source=DetectionSource.VISUAL_CV,
            attribution_mode=AttributionMode.VISUAL_LOCAL_HUD,
            match_timestamp=now,
            match_duration_ms=events[-1].timestamp_ms,
            friendly_name=session_label,
            engine_version="OpenCV-LiveVisualFSM",
            events=events,
        )
        inserted, match = self.db.save_analysis_result(result)
        if inserted and match is not None:
            self._after_match_saved(match)
            return match
        return None

    def _handle_live_visual_chest(self, event: ChestEventRecord) -> None:
        now_mono = time.monotonic()
        with self._lock:
            self._live_visual_events.append(event)
            live_count = self.counter.increment_live_match(1)
            # Start a fresh live session ID if idle > 4 minutes between matches
            if (
                self._live_visual_session_id is None
                or (now_mono - self._live_visual_last_event_mono) > 240.0
            ):
                self._live_visual_session_id = datetime.now(timezone.utc).strftime(
                    "%Y%m%d_%H%M%S_%f"
                )
                self.counter.reset_live_match()
                live_count = self.counter.increment_live_match(1)
            session_id = self._live_visual_session_id
            self._live_visual_last_event_mono = now_mono

        if self.config.detection_mode == "visual":
            # Persist immediately into SQLite and animate counter in real time!
            self.db.record_live_visual_chest(
                session_id=session_id,
                event=event,
                friendly_name="Live Visual CV Match",
            )
            snap = self.stats_service.get_snapshot()
            self.counter.sync_total(snap.total_chests)
            self._emit_stats(snap)
            self._emit_status(
                StatusEvent(
                    level="active",
                    label=f"CHEST +{live_count} (LIVE CV)",
                    detail=f"Открыт сундук ({event.confidence:.0%} уверенность)",
                )
            )
        else:
            # Hybrid mode: show live CV detections while waiting for .replay completion
            self._emit_status(
                StatusEvent(
                    level="active",
                    label=f"MONITORING (+{live_count} CV)",
                    detail=f"Обнаружено открытий в текущей катке (Visual CV): {live_count}",
                )
            )

    def _handle_visual_diagnostics(self, diag: VisualDiagnostics) -> None:
        if diag.black_frame_warning and not self._visual_black_alerted:
            self._visual_black_alerted = True
            self._emit_status(
                StatusEvent(
                    level="warning",
                    label="BLACK SCREEN — SET BORDERLESS",
                    detail=(
                        "Захват экрана получает чёрный кадр (Полноэкранный режим Fortnite). "
                        "Переключите Fortnite в «В окне без рамки» (Windowed Fullscreen)."
                    ),
                )
            )
        elif not diag.black_frame_warning and self._visual_black_alerted:
            self._visual_black_alerted = False
            self._restore_monitoring_status()

    def _handle_visual_error(self, message: str) -> None:
        self._emit_error("VisualDetector", message)

    def _handle_watcher_status(self, level: str, message: str) -> None:
        label = "MONITORING ACTIVE" if level == "active" else level.upper()
        self._emit_status(StatusEvent(level=level, label=label, detail=message))

    def _after_match_saved(self, match: MatchRecord) -> None:
        snap = self.stats_service.get_snapshot()
        self.counter.sync_total(snap.total_chests)
        if self.config.auto_backup_enabled:
            try:
                self.backup_service.create_backup(tag="auto")
            except Exception:
                pass
        for cb in self._match_listeners:
            cb(match)
        self._emit_stats(snap)

    # ------------------------------------------------------------------
    # Settings, backups, retry & recovery
    # ------------------------------------------------------------------
    def update_config(self, new_config: AppConfig, restart_monitoring: bool = True) -> None:
        self.config = new_config
        self.db.save_config(new_config)
        self.backup_service.retention_count = max(1, new_config.backup_retention_count)
        self.replay_analyzer = ReplayAnalyzer(
            oodle_dll_path=new_config.oodle_dll_path or None,
            external_parser_cmd=new_config.external_parser_cmd,
        )
        if restart_monitoring:
            self.start_monitoring()

    def retry_failed_replays(self) -> int:
        self.db.clear_failed_analyses()
        return self.scan_replay_directory_now()

    def create_manual_backup(self) -> BackupInfo:
        return self.backup_service.create_backup(tag="manual")

    def list_backups(self) -> list[BackupInfo]:
        return self.backup_service.list_backups()

    def restore_from_backup(self, backup_path: str | Path) -> StatsSnapshot:
        was_running = self._watcher is not None or self._live_visual is not None
        self.stop_monitoring()
        self.backup_service.restore_backup(backup_path)
        self.db = DatabaseManager(self.db_path)
        self.stats_service = StatisticsService(self.db)
        snap = self.stats_service.get_snapshot()
        self.counter.sync_total(snap.total_chests)
        self._emit_stats(snap)
        if was_running:
            self.start_monitoring()
        return snap

    def _restore_monitoring_status(self) -> None:
        if self._watcher is not None and self._watcher.is_running:
            self._emit_status(
                StatusEvent(
                    level="active",
                    label="MONITORING ACTIVE",
                    detail=f"Папка: {self.config.replay_dir}",
                )
            )
        elif self._live_visual is not None and self._live_visual.is_running:
            self._emit_status(
                StatusEvent(
                    level="active",
                    label="VISUAL CV ACTIVE",
                    detail="Мониторинг экрана активен",
                )
            )
        else:
            self._emit_status(StatusEvent(level="idle", label="READY"))

    def _emit_stats(self, snapshot: StatsSnapshot) -> None:
        for cb in self._stats_listeners:
            cb(snapshot)

    def _emit_status(self, status: StatusEvent) -> None:
        self._current_status = status
        for cb in self._status_listeners:
            cb(status)

    def _emit_error(self, source: str, message: str) -> None:
        for cb in self._error_listeners:
            cb(source, message)
