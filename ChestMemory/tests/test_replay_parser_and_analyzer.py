"""Unit tests for binary Fortnite `.replay` parser and ContainerTracker / ReplayAnalyzer."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.models import AnalysisStatus, AttributionMode, DetectionSource
from detector.replay_analyzer import (
    ContainerTracker,
    ContainerUpdate,
    ReplayAnalyzer,
    is_chest_container_class,
    normalize_container_class,
)
from detector.replay_parser import (
    ReplayParser,
    UnrealBitReader,
    is_replay_live,
)
from tests.replay_fixture_builder import build_replay_bytes


class TestReplayParserAndAnalyzer(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.parser = ReplayParser()
        self.analyzer = ReplayAnalyzer()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_class_normalization_and_chest_filtering(self) -> None:
        self.assertEqual(
            normalize_container_class(
                "/Game/Building/ActorBlueprints/Containers/Tiered_Chest_Athena_C_UAID_04D4C4F59283_14203"
            ),
            "Tiered_Chest_Athena_C",
        )
        self.assertEqual(
            normalize_container_class("Apollo_TreasureChest_C_42"),
            "Apollo_TreasureChest_C",
        )
        self.assertTrue(is_chest_container_class("Tiered_Chest_Athena_C_UAID_9999"))
        self.assertTrue(is_chest_container_class("Helios_Chest_Rare_C"))
        self.assertFalse(is_chest_container_class("Tiered_Ammo_Athena_C"))
        self.assertFalse(is_chest_container_class("B_Cooler_Container_C"))
        self.assertFalse(is_chest_container_class("SupplyDrop_Athena_C"))

    def test_unreal_bit_reader(self) -> None:
        # 0b1010_0101 = 0xA5 (LSB first: 1,0,1,0,0,1,0,1)
        br = UnrealBitReader(bytes([0xA5, 0x0C]))
        self.assertEqual(br.read_bit(), 1)
        self.assertEqual(br.read_bit(), 0)
        self.assertEqual(br.read_bits(6), 0b101001)
        # 0x0C = 0b00001100 -> packed int: low bit 0 (stop), value = 0x0C >> 1 = 6
        self.assertEqual(br.read_int_packed(), 6)

    def test_container_tracker_counts_only_genuine_local_openings(self) -> None:
        tracker = ContainerTracker(local_player_pawn_guids={"pawn_local"})

        # 1. Closed chest spawned on map -> MUST NOT COUNT
        self.assertIsNone(
            tracker.apply_update(
                ContainerUpdate(
                    timestamp_ms=1000,
                    container_guid="chest_1",
                    container_class="Tiered_Chest_Athena_C_UAID_AA11",
                    is_initial_bunch=True,
                    b_already_searched=False,
                )
            )
        )
        # 2. Already-open chest entering PVS on initial bunch -> MUST NOT COUNT
        self.assertIsNone(
            tracker.apply_update(
                ContainerUpdate(
                    timestamp_ms=1500,
                    container_guid="chest_already_open",
                    container_class="Tiered_Chest_Athena_C",
                    is_initial_bunch=True,
                    b_already_searched=True,
                )
            )
        )
        # 3. Ammo box opened by local player -> MUST NOT COUNT
        self.assertIsNone(
            tracker.apply_update(
                ContainerUpdate(
                    timestamp_ms=2000,
                    container_guid="ammo_1",
                    container_class="Tiered_Ammo_Athena_C",
                    b_already_searched=True,
                    searching_pawn_guid="pawn_local",
                )
            )
        )
        # 4. Chest destroyed without opening -> MUST NOT COUNT
        self.assertIsNone(
            tracker.apply_update(
                ContainerUpdate(
                    timestamp_ms=2500,
                    container_guid="chest_destroyed",
                    container_class="Tiered_Chest_Athena_C",
                    b_destroyed=True,
                )
            )
        )
        # 5. Chest opened by OPPONENT in PVS -> excluded from local count
        ev_enemy = tracker.apply_update(
            ContainerUpdate(
                timestamp_ms=3000,
                container_guid="chest_enemy",
                container_class="Tiered_Chest_Athena_C",
                b_already_searched=True,
                searching_pawn_guid="pawn_opponent",
            )
        )
        self.assertIsNotNone(ev_enemy)
        self.assertFalse(ev_enemy.opened_by_local_player)

        # 6. Chest #1 opened by LOCAL player -> +1
        ev_local_1 = tracker.apply_update(
            ContainerUpdate(
                timestamp_ms=4000,
                container_guid="chest_1",
                container_class="Tiered_Chest_Athena_C_UAID_AA11",
                b_already_searched=True,
                search_animation_count=1,
                searching_pawn_guid="pawn_local",
            )
        )
        self.assertIsNotNone(ev_local_1)
        self.assertTrue(ev_local_1.opened_by_local_player)

        # Duplicate property re-replication with same True/1 state -> MUST NOT double count
        self.assertIsNone(
            tracker.apply_update(
                ContainerUpdate(
                    timestamp_ms=4500,
                    container_guid="chest_1",
                    container_class="Tiered_Chest_Athena_C_UAID_AA11",
                    b_already_searched=True,
                    search_animation_count=1,
                    searching_pawn_guid="pawn_local",
                )
            )
        )

        # 7. Refilled chest opened again by LOCAL player (SearchAnimationCount 1 -> 2) -> +1
        ev_refill = tracker.apply_update(
            ContainerUpdate(
                timestamp_ms=9000,
                container_guid="chest_1",
                container_class="Tiered_Chest_Athena_C_UAID_AA11",
                b_already_searched=True,
                search_animation_count=2,
                searching_pawn_guid="pawn_local",
            )
        )
        self.assertIsNotNone(ev_refill)
        self.assertEqual(tracker.local_opened_count, 2)
        self.assertEqual(tracker.attribution_mode, AttributionMode.LOCAL_PLAYER)

    def test_binary_replay_parse_and_zlib_decompression(self) -> None:
        ts = datetime(2026, 11, 21, 16, 30, 0, tzinfo=timezone.utc)
        updates = [
            ContainerUpdate(1000, "c1", "Tiered_Chest_Athena_C", is_initial_bunch=True, b_already_searched=False),
            ContainerUpdate(5000, "c1", "Tiered_Chest_Athena_C", b_already_searched=True, searching_pawn_guid="local_p"),
            ContainerUpdate(8000, "c2", "Helios_Chest_Rare_C_UAID_ABCD", b_already_searched=True, searching_pawn_guid="local_p"),
            ContainerUpdate(9000, "c3", "Tiered_Chest_Athena_C", b_already_searched=True, searching_pawn_guid="enemy_p"),
            ContainerUpdate(10000, "a1", "Tiered_Ammo_Athena_C", b_already_searched=True, searching_pawn_guid="local_p"),
        ]
        raw = build_replay_bytes(
            match_guid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            friendly_name="Solo Match UTF16",
            utf16_friendly_name=True,
            timestamp=ts,
            is_compressed=True,
            compress_with_zlib=True,
            local_pawns=["local_p"],
            updates=updates,
            eliminations=7,
            placement=1,
        )
        replay_file = self.root / "match_zlib.replay"
        replay_file.write_bytes(raw)

        self.assertFalse(is_replay_live(replay_file))
        result = self.analyzer.analyze_file(replay_file)
        self.assertTrue(result.is_reliable)
        self.assertEqual(result.status, AnalysisStatus.SUCCESS)
        self.assertEqual(result.chests_opened, 2)
        self.assertEqual(result.replay_id, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        self.assertEqual(result.eliminations, 7)
        self.assertEqual(result.placement, 1)
        self.assertEqual(result.attribution_mode, AttributionMode.LOCAL_PLAYER)

    def test_live_oodle_encrypted_corrupted_and_unsupported_fail_honestly(self) -> None:
        # 1. IsLive=True replay
        live_path = self.root / "live.replay"
        live_path.write_bytes(build_replay_bytes(is_live=True))
        self.assertTrue(is_replay_live(live_path))
        r_live = self.analyzer.analyze_file(live_path)
        self.assertFalse(r_live.is_reliable)
        self.assertEqual(r_live.status, AnalysisStatus.LIVE_REPLAY)
        self.assertIsNone(r_live.chests_opened)

        # 2. Oodle-compressed replay without oo2core DLL
        oodle_path = self.root / "oodle.replay"
        oodle_path.write_bytes(build_replay_bytes(is_compressed=True, compress_with_zlib=False))
        r_oodle = self.analyzer.analyze_file(oodle_path)
        self.assertFalse(r_oodle.is_reliable)
        self.assertEqual(r_oodle.status, AnalysisStatus.DECOMPRESSION_REQUIRED)
        self.assertIsNone(r_oodle.chests_opened)

        # 3. Unrecognized raw UE5 ReplayData bitstream -> UNSUPPORTED_FORMAT (never 0!)
        raw_ue5_path = self.root / "ue5_unknown.replay"
        raw_ue5_path.write_bytes(build_replay_bytes(raw_replay_data=b"\xDE\xAD\xBE\xEF" * 32))
        r_unsup = self.analyzer.analyze_file(raw_ue5_path)
        self.assertFalse(r_unsup.is_reliable)
        self.assertEqual(r_unsup.status, AnalysisStatus.UNSUPPORTED_FORMAT)
        self.assertIsNone(r_unsup.chests_opened)

        # 4. Corrupted garbage file -> CORRUPTED
        bad_path = self.root / "corrupted.replay"
        bad_path.write_bytes(b"INVALID_REPLAY_BYTES_1234567890" * 5)
        r_bad = self.analyzer.analyze_file(bad_path)
        self.assertFalse(r_bad.is_reliable)
        self.assertEqual(r_bad.status, AnalysisStatus.CORRUPTED)
        self.assertIsNone(r_bad.chests_opened)

    def test_external_cli_bridge_integration(self) -> None:
        # Simulate an external CLI replay reader script emitting valid JSON
        bridge_script = self.root / "mock_bridge.py"
        bridge_script.write_text(
            "import json, sys\n"
            "print(json.dumps({\n"
            "  'replay_id': 'ext-guid-001',\n"
            "  'chests_opened': 19,\n"
            "  'attribution': 'local_player',\n"
            "  'match_duration_ms': 1020000,\n"
            "  'eliminations': 6\n"
            "}))\n",
            encoding="utf-8",
        )
        replay_file = self.root / "season_latest.replay"
        replay_file.write_bytes(build_replay_bytes(raw_replay_data=b"\x00" * 32))

        cmd = f'"{sys.executable}" "{bridge_script}" "{{replay}}"'
        analyzer = ReplayAnalyzer(external_parser_cmd=cmd)
        result = analyzer.analyze_file(replay_file)
        self.assertTrue(result.is_reliable)
        self.assertEqual(result.chests_opened, 19)
        self.assertEqual(result.source, DetectionSource.EXTERNAL_REPLAY_BRIDGE)
        self.assertEqual(result.replay_id, "ext-guid-001")


if __name__ == "__main__":
    unittest.main()
