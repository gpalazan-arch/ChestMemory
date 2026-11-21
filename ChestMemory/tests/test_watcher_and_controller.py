"""Integration tests for ReplayWatcher stabilization, ChestMemoryController, and CLI."""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as cli_main
from core.controller import ChestMemoryController
from database.models import AnalysisStatus, ChestEventRecord
from detector.replay_analyzer import ContainerUpdate
from detector.visual_detector import VisualDiagnostics
from services.replay_watcher import ReplayWatcher
from tests.replay_fixture_builder import build_replay_bytes


class TestWatcherAndController(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.demos_dir = self.root / "Demos"
        self.data_dir = self.root / "Data"
        self.demos_dir.mkdir()
        self.data_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_watcher_waits_for_is_live_false_and_stability(self) -> None:
        ready_files: list[Path] = []
        ready_event = threading.Event()

        def on_ready(path: Path) -> None:
            ready_files.append(path)
            ready_event.set()

        watcher = ReplayWatcher(
            watch_dir=self.demos_dir,
            on_replay_ready=on_ready,
            stable_seconds=0.15,
            poll_interval=0.05,
        )
        self.assertTrue(watcher.start(scan_existing=False))
        try:
            target = self.demos_dir / "active_match.replay"
            # Step 1: Write live replay (IsLive=True) -> MUST NOT trigger yet
            target.write_bytes(build_replay_bytes(is_live=True))
            watcher.mark_candidate(target)
            time.sleep(0.35)
            self.assertEqual(len(ready_files), 0)

            # Step 2: Fortnite finalizes replay (IsLive=False) -> triggers once stabilized
            target.write_bytes(
                build_replay_bytes(
                    is_live=False,
                    local_pawns=["p1"],
                    updates=[
                        ContainerUpdate(
                            1000,
                            "c1",
                            "Tiered_Chest_Athena_C",
                            b_already_searched=True,
                            searching_pawn_guid="p1",
                        )
                    ],
                )
            )
            watcher.mark_candidate(target)
            self.assertTrue(ready_event.wait(timeout=3.0))
            self.assertEqual(ready_files, [target])
        finally:
            watcher.stop()

    def test_controller_end_to_end_pipeline_dedup_backup_and_error_isolation(self) -> None:
        controller = ChestMemoryController(data_dir=self.data_dir)
        cfg = replace(
            controller.config,
            replay_dir=str(self.demos_dir),
            detection_mode="replay",
            file_stable_seconds=0.12,
            auto_backup_enabled=True,
        )
        controller.update_config(cfg, restart_monitoring=False)

        recorded_matches = []
        errors = []
        match_event = threading.Event()
        error_event = threading.Event()

        controller.on_match_recorded(lambda m: (recorded_matches.append(m), match_event.set()))
        controller.on_analysis_error(lambda src, msg: (errors.append((src, msg)), error_event.set()))

        self.assertTrue(controller.start_monitoring())
        try:
            # Drop a genuine finalized replay with 3 local chest openings
            valid_replay = self.demos_dir / "match_01.replay"
            valid_replay.write_bytes(
                build_replay_bytes(
                    match_guid="11111111-2222-3333-4444-555555555555",
                    timestamp=datetime.now(timezone.utc),
                    local_pawns=["hero_pawn"],
                    updates=[
                        ContainerUpdate(2000, "c1", "Tiered_Chest_Athena_C", b_already_searched=True, searching_pawn_guid="hero_pawn"),
                        ContainerUpdate(4000, "c2", "Tiered_Chest_Athena_C", b_already_searched=True, searching_pawn_guid="hero_pawn"),
                        ContainerUpdate(6000, "c3", "Helios_Chest_C", b_already_searched=True, searching_pawn_guid="hero_pawn"),
                        ContainerUpdate(7000, "c4", "Tiered_Chest_Athena_C", b_already_searched=True, searching_pawn_guid="enemy_pawn"),
                    ],
                )
            )
            self.assertTrue(match_event.wait(timeout=5.0))
            self.assertEqual(len(recorded_matches), 1)
            self.assertEqual(recorded_matches[0].chests_opened, 3)

            snap = controller.get_stats()
            self.assertEqual(snap.total_chests, 3)
            self.assertEqual(snap.today_chests, 3)
            self.assertEqual(snap.last_match_chests, 3)
            self.assertGreaterEqual(len(controller.list_backups()), 1)

            # Drop an unreadable Oodle replay -> must raise error, NOT record 0!
            broken_replay = self.demos_dir / "match_02_oodle.replay"
            broken_replay.write_bytes(build_replay_bytes(is_compressed=True, compress_with_zlib=False))
            self.assertTrue(error_event.wait(timeout=5.0))

            snap_after = controller.get_stats()
            self.assertEqual(snap_after.total_chests, 3)
            self.assertEqual(snap_after.total_matches, 1)
            self.assertEqual(snap_after.last_match_chests, 3)
            self.assertEqual(snap_after.failed_replays_count, 1)

            # Re-scanning the exact same valid replay must NOT double count
            self.assertEqual(controller.scan_replay_directory_now(), 0)
            self.assertEqual(controller.get_stats().total_chests, 3)
        finally:
            controller.stop_monitoring()

    def test_cli_analyze_and_stats_commands(self) -> None:
        replay_path = self.demos_dir / "cli_match.replay"
        replay_path.write_bytes(
            build_replay_bytes(
                match_guid="99999999-8888-7777-6666-555555555555",
                timestamp=datetime.now(timezone.utc),
                local_pawns=["p_cli"],
                updates=[
                    ContainerUpdate(1500, "c1", "Tiered_Chest_Athena_C", b_already_searched=True, searching_pawn_guid="p_cli"),
                    ContainerUpdate(3000, "c2", "Tiered_Chest_Athena_C", b_already_searched=True, searching_pawn_guid="p_cli"),
                ],
            )
        )
        rc1 = cli_main.main(["--data-dir", str(self.data_dir), "--analyze", str(replay_path)])
        self.assertEqual(rc1, 0)
        rc2 = cli_main.main(["--data-dir", str(self.data_dir), "--stats"])
        self.assertEqual(rc2, 0)

        ctrl = ChestMemoryController(data_dir=self.data_dir)
        self.assertEqual(ctrl.get_stats().total_chests, 2)

    def test_visual_mode_persists_chests_immediately_and_warns_on_black_frame(self) -> None:
        ctrl = ChestMemoryController(data_dir=self.data_dir)
        cfg = replace(ctrl.config, detection_mode="visual", auto_backup_enabled=False)
        ctrl.update_config(cfg, restart_monitoring=False)

        emitted_stats = []
        emitted_statuses = []
        ctrl.on_stats_updated(emitted_stats.append)
        ctrl.on_status_changed(emitted_statuses.append)

        # Simulate two live visual chest openings
        ctrl._handle_live_visual_chest(
            ChestEventRecord(timestamp_ms=12_000, container_guid="v1", container_class="Visual_Chest_Opened")
        )
        ctrl._handle_live_visual_chest(
            ChestEventRecord(timestamp_ms=25_000, container_guid="v2", container_class="Visual_Chest_Opened")
        )

        self.assertEqual(len(emitted_stats), 2)
        self.assertEqual(emitted_stats[-1].total_chests, 2)
        self.assertEqual(emitted_stats[-1].last_match_chests, 2)
        self.assertEqual(emitted_stats[-1].total_matches, 1)

        # Verify persisted across controller restart
        reloaded = ChestMemoryController(data_dir=self.data_dir)
        self.assertEqual(reloaded.get_stats().total_chests, 2)

        # Verify Exclusive Fullscreen black-frame warning surfaces clearly
        ctrl._handle_visual_diagnostics(
            VisualDiagnostics(running=True, frame_mean_luma=0.0, black_frame_warning=True)
        )
        self.assertEqual(emitted_statuses[-1].level, "warning")
        self.assertIn("BLACK SCREEN", emitted_statuses[-1].label)


if __name__ == "__main__":
    unittest.main()
