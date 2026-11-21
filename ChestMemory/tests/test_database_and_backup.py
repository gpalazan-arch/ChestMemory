"""Unit tests for SQLite DatabaseManager, StatisticsService, and BackupService."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.statistics import StatisticsService, format_chest_number
from database.database import DatabaseManager
from database.models import (
    AnalysisResult,
    AnalysisStatus,
    AppConfig,
    AttributionMode,
    ChestEventRecord,
    DetectionSource,
)
from services.backup_service import BackupService


class TestDatabaseAndBackup(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "chestmemory.db"
        self.backup_dir = self.root / "backups"
        self.db = DatabaseManager(self.db_path)
        self.stats = StatisticsService(self.db)
        self.backups = BackupService(self.db_path, self.backup_dir, retention_count=3)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_result(
        self,
        replay_id: str,
        file_hash: str,
        chests: int,
        played_at: datetime,
    ) -> AnalysisResult:
        return AnalysisResult(
            status=AnalysisStatus.SUCCESS,
            replay_id=replay_id,
            file_path=f"/Demos/{replay_id}.replay",
            file_hash=file_hash,
            chests_opened=chests,
            source=DetectionSource.REPLAY_PARSER,
            attribution_mode=AttributionMode.LOCAL_PLAYER,
            match_timestamp=played_at,
            match_duration_ms=840_000,
            eliminations=5,
            placement=2,
            events=[
                ChestEventRecord(
                    timestamp_ms=10_000 + i * 5000,
                    container_guid=f"guid_{i}",
                    container_class="Tiered_Chest_Athena_C",
                )
                for i in range(chests)
            ],
        )

    def test_deduplication_by_replay_id_and_file_hash(self) -> None:
        now = datetime(2026, 11, 21, 14, 0, tzinfo=timezone.utc)
        r1 = self._make_result("guid-1", "hash-1", 12, now)
        inserted1, m1 = self.db.save_analysis_result(r1)
        self.assertTrue(inserted1)
        self.assertIsNotNone(m1)

        # Duplicate replay_id with different file hash must NOT be double-counted
        r_dup_id = self._make_result("guid-1", "hash-different", 12, now)
        inserted2, _ = self.db.save_analysis_result(r_dup_id)
        self.assertFalse(inserted2)

        # Duplicate file_hash with different replay_id must NOT be double-counted
        r_dup_hash = self._make_result("guid-different", "hash-1", 12, now)
        inserted3, _ = self.db.save_analysis_result(r_dup_hash)
        self.assertFalse(inserted3)

        snap = self.db.get_stats_snapshot(now=now)
        self.assertEqual(snap.total_chests, 12)
        self.assertEqual(snap.total_matches, 1)

    def test_refuses_to_persist_unreliable_result_as_zero(self) -> None:
        bad = AnalysisResult(
            status=AnalysisStatus.DECOMPRESSION_REQUIRED,
            replay_id="bad-1",
            file_path="bad.replay",
            file_hash="bad-hash",
            chests_opened=None,
            error_message="Oodle DLL missing",
        )
        with self.assertRaises(ValueError):
            self.db.save_analysis_result(bad)

        self.db.record_failed_analysis(bad)
        snap = self.db.get_stats_snapshot()
        self.assertEqual(snap.total_chests, 0)
        self.assertEqual(snap.total_matches, 0)
        self.assertIsNone(snap.last_match_chests)
        self.assertEqual(snap.failed_replays_count, 1)

    def test_period_statistics_today_week_month_last_match(self) -> None:
        # Saturday 2026-11-21
        ref_now = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)
        today_1 = ref_now - timedelta(hours=3)
        today_2 = ref_now - timedelta(hours=1)
        this_week = ref_now - timedelta(days=3)  # Wednesday same week
        this_month_older = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
        last_month = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)

        self.db.save_analysis_result(self._make_result("m1", "h1", 10, last_month))
        self.db.save_analysis_result(self._make_result("m2", "h2", 20, this_month_older))
        self.db.save_analysis_result(self._make_result("m3", "h3", 15, this_week))
        self.db.save_analysis_result(self._make_result("m4", "h4", 8, today_1))
        self.db.save_analysis_result(self._make_result("m5", "h5", 18, today_2))

        snap = self.stats.get_snapshot(now=ref_now)
        self.assertEqual(snap.total_chests, 71)
        self.assertEqual(snap.total_matches, 5)
        self.assertEqual(snap.today_chests, 26)
        self.assertEqual(snap.week_chests, 41)
        self.assertEqual(snap.month_chests, 61)
        self.assertEqual(snap.last_match_chests, 18)
        self.assertEqual(snap.best_match_chests, 20)
        self.assertEqual(format_chest_number(12458), "12 458")

        # Export CSV and JSON
        csv_path = self.stats.export_csv(self.root / "out.csv")
        json_path = self.stats.export_json(self.root / "out.json")
        self.assertTrue(csv_path.is_file())
        exported = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(exported["summary"]["total_chests"], 71)
        self.assertEqual(len(exported["matches"]), 5)

    def test_config_roundtrip(self) -> None:
        cfg = AppConfig(
            replay_dir=r"C:\Custom\Demos",
            always_on_top=False,
            detection_mode="visual",
            window_x=220,
            window_y=140,
            window_opacity=0.88,
        )
        self.db.save_config(cfg)
        loaded = self.db.load_config()
        self.assertEqual(loaded.replay_dir, r"C:\Custom\Demos")
        self.assertFalse(loaded.always_on_top)
        self.assertEqual(loaded.detection_mode, "visual")
        self.assertEqual(loaded.window_x, 220)
        self.assertAlmostEqual(loaded.window_opacity, 0.88)

    def test_backup_retention_and_corrupted_db_recovery(self) -> None:
        now = datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc)
        self.db.save_analysis_result(self._make_result("bak-1", "bh1", 14, now))

        b1 = self.backups.create_backup(tag="t1")
        self.db.save_analysis_result(self._make_result("bak-2", "bh2", 9, now + timedelta(minutes=5)))
        b2 = self.backups.create_backup(tag="t2")
        b3 = self.backups.create_backup(tag="t3")
        b4 = self.backups.create_backup(tag="t4")

        # Retention count is 3 -> oldest backup pruned
        listed = self.backups.list_backups()
        self.assertEqual(len(listed), 3)
        self.assertFalse(b1.path.exists())
        self.assertTrue(b4.path.exists())

        # Corrupt primary database file on disk
        self.db_path.write_bytes(b"NOT A VALID SQLITE DATABASE HEADER" * 100)
        ok, _ = self.backups.verify_sqlite_file(self.db_path)
        self.assertFalse(ok)

        # Recover automatically from latest valid backup
        restored = self.backups.restore_latest_valid_backup()
        self.assertIsNotNone(restored)
        recovered_db = DatabaseManager(self.db_path)
        snap = recovered_db.get_stats_snapshot(now=now + timedelta(minutes=10))
        self.assertEqual(snap.total_chests, 23)
        self.assertEqual(snap.total_matches, 2)
        self.assertTrue(b2.path.exists() or b3.path.exists())


if __name__ == "__main__":
    unittest.main()
