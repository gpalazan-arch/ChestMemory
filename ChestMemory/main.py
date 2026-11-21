"""ChestMemory — Windows desktop entry point & headless CLI utilities."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Ensure ChestMemory package root is importable when run directly or via PyInstaller
APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from core.controller import ChestMemoryController  # noqa: E402
from core.statistics import format_chest_number  # noqa: E402


def resolve_data_dir(custom_dir: str | None = None) -> Path:
    """Resolve persistent storage directory (portable ./data or %APPDATA%/ChestMemory)."""
    if custom_dir:
        return Path(custom_dir)
    if getattr(sys, "frozen", False):
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "ChestMemory"
        return Path(sys.executable).resolve().parent / "data"
    return APP_ROOT / "data"


def _run_cli(args: argparse.Namespace, controller: ChestMemoryController) -> int:
    if args.analyze:
        target = Path(args.analyze)
        if target.suffix.lower() == ".replay":
            result = controller.process_replay_file(target, force_retry_failed=True)
        else:
            result = controller.analyze_video_file(target)

        if result is None:
            print(f"[SKIP] Already processed: {target.name}")
            return 0
        if result.is_reliable:
            print(
                f"[OK] {target.name}: {result.chests_opened} opened chests "
                f"(source={result.source.value}, attribution={result.attribution_mode.value})"
            )
            return 0
        print(f"[ERROR] {target.name} ({result.status.value}): {result.error_message}")
        return 2

    if args.scan:
        count = controller.scan_replay_directory_now()
        print(f"Processed {count} new replay(s).")

    if args.backup:
        info = controller.create_manual_backup()
        print(f"Backup created: {info.path} (sha256={info.sha256[:12]})")

    if args.stats or args.scan or args.backup:
        snap = controller.get_stats()
        print(f"CHEST MEMORY TOTAL : {format_chest_number(snap.total_chests)}")
        print(f"Matches            : {snap.total_matches}")
        print(f"Today              : {format_chest_number(snap.today_chests)}")
        print(f"Week               : {format_chest_number(snap.week_chests)}")
        print(f"Month              : {format_chest_number(snap.month_chests)}")
        last_str = "—" if snap.last_match_chests is None else str(snap.last_match_chests)
        print(f"Last Match         : {last_str}")
        print(f"Failed Replays     : {snap.failed_replays_count}")
        return 0
    return 0


def _launch_gui(controller: ChestMemoryController) -> int:
    try:
        from PySide6.QtNetwork import QLocalServer, QLocalSocket
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        print(
            "PySide6 is not installed. Run `pip install -r requirements.txt` first.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    from interface.main_window import ChestMemoryMainWindow
    from interface.tray import build_app_icon

    app = QApplication(sys.argv)
    app.setApplicationName("ChestMemory")
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(build_app_icon(64))

    # Single-instance guard so ChestMemory never spawns duplicate watchers
    ipc_name = "ChestMemory_SingleInstance_IPC"
    probe = QLocalSocket()
    probe.connectToServer(ipc_name)
    if probe.waitForConnected(250):
        probe.write(b"SHOW")
        probe.flush()
        probe.waitForBytesWritten(250)
        probe.disconnectFromServer()
        return 0

    QLocalServer.removeServer(ipc_name)
    ipc_server = QLocalServer()
    ipc_server.listen(ipc_name)

    window = ChestMemoryMainWindow(controller)

    def _on_second_instance() -> None:
        conn = ipc_server.nextPendingConnection()
        if conn is not None:
            conn.close()
        window.show()
        window.raise_()
        window.activateWindow()

    ipc_server.newConnection.connect(_on_second_instance)
    window.show()
    controller.start_monitoring()

    exit_code = app.exec()
    controller.stop_monitoring()
    ipc_server.close()
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ChestMemory",
        description="Automatic Fortnite opened chest counter for Windows.",
    )
    parser.add_argument("--data-dir", help="Custom directory for SQLite DB and backups.")
    parser.add_argument("--analyze", help="Analyze a single .replay or gameplay video file and exit.")
    parser.add_argument("--scan", action="store_true", help="Scan Fortnite replay directory once and exit.")
    parser.add_argument("--stats", action="store_true", help="Print aggregated chest statistics and exit.")
    parser.add_argument("--backup", action="store_true", help="Create a verified SQLite backup and exit.")
    args = parser.parse_args(argv)

    controller = ChestMemoryController(data_dir=resolve_data_dir(args.data_dir))
    if args.analyze or args.scan or args.stats or args.backup:
        return _run_cli(args, controller)
    return _launch_gui(controller)


if __name__ == "__main__":
    raise SystemExit(main())
