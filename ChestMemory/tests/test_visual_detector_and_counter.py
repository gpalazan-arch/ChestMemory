"""Unit tests for VisualChestDetector FSM and CounterAnimationState."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.counter import ChestCounter, CounterAnimationState
from detector.visual_detector import ChestOpeningFSM, FrameFeatures, VisualState


class TestVisualDetectorFSM(unittest.TestCase):
    def test_looking_at_closed_chest_never_counts(self) -> None:
        fsm = ChestOpeningFSM()
        for step in range(30):
            ev = fsm.update(
                FrameFeatures(
                    timestamp_s=step * 0.1,
                    prompt_score=0.80,
                    ring_fill_ratio=0.0,
                    loot_burst_score=0.25,
                )
            )
            self.assertIsNone(ev)
        self.assertEqual(fsm.opened_count, 0)

    def test_aborted_chest_search_at_45_percent_never_counts(self) -> None:
        fsm = ChestOpeningFSM()
        frames = [
            FrameFeatures(0.0, prompt_score=0.75, ring_fill_ratio=0.0),
            FrameFeatures(0.1, prompt_score=0.70, ring_fill_ratio=0.18),
            FrameFeatures(0.2, prompt_score=0.60, ring_fill_ratio=0.33),
            FrameFeatures(0.3, prompt_score=0.50, ring_fill_ratio=0.45),
            # Player lets go of E -> ring disappears early
            FrameFeatures(0.4, prompt_score=0.0, ring_fill_ratio=0.0, loot_burst_score=0.60),
            FrameFeatures(0.5, prompt_score=0.0, ring_fill_ratio=0.0, xp_banner_score=0.60),
        ]
        for f in frames:
            self.assertIsNone(fsm.update(f))
        self.assertEqual(fsm.opened_count, 0)
        self.assertEqual(fsm.state, VisualState.IDLE)

    def test_full_search_plus_loot_burst_counts_exactly_once_with_cooldown(self) -> None:
        fsm = ChestOpeningFSM()
        sequence = [
            FrameFeatures(1.00, prompt_score=0.80, ring_fill_ratio=0.00),
            FrameFeatures(1.10, prompt_score=0.70, ring_fill_ratio=0.20),
            FrameFeatures(1.20, prompt_score=0.60, ring_fill_ratio=0.42),
            FrameFeatures(1.30, prompt_score=0.50, ring_fill_ratio=0.68),
            FrameFeatures(1.42, prompt_score=0.30, ring_fill_ratio=0.90),
            # Ring completes and clears -> golden burst + XP flash
            FrameFeatures(1.50, prompt_score=0.00, ring_fill_ratio=0.02, loot_burst_score=0.75),
            # Consecutive frames during burst/cooldown MUST NOT double count
            FrameFeatures(1.60, prompt_score=0.00, ring_fill_ratio=0.00, loot_burst_score=0.82, xp_banner_score=0.85),
            FrameFeatures(1.80, prompt_score=0.80, ring_fill_ratio=0.95, loot_burst_score=0.80),
        ]
        events = [ev for f in sequence if (ev := fsm.update(f)) is not None]
        self.assertEqual(len(events), 1)
        self.assertEqual(fsm.opened_count, 1)
        self.assertEqual(events[0].timestamp_ms, 1500)
        self.assertTrue(events[0].opened_by_local_player)
        self.assertGreaterEqual(events[0].confidence, 0.90)

    def test_multiple_sequential_chests_count_accurately(self) -> None:
        fsm = ChestOpeningFSM()
        for idx in range(3):
            base = idx * 4.0
            fsm.update(FrameFeatures(base + 0.0, prompt_score=0.70))
            fsm.update(FrameFeatures(base + 0.1, prompt_score=0.70, ring_fill_ratio=0.22))
            fsm.update(FrameFeatures(base + 0.25, prompt_score=0.60, ring_fill_ratio=0.54))
            fsm.update(FrameFeatures(base + 0.40, prompt_score=0.40, ring_fill_ratio=0.86))
            fsm.update(FrameFeatures(base + 0.50, ring_fill_ratio=0.00, xp_banner_score=0.72))
        self.assertEqual(fsm.opened_count, 3)

    def test_random_gold_flash_without_search_never_counts(self) -> None:
        fsm = ChestOpeningFSM()
        for step in range(20):
            ev = fsm.update(
                FrameFeatures(
                    timestamp_s=step * 0.1,
                    loot_burst_score=0.95,
                    xp_banner_score=0.95,
                )
            )
            self.assertIsNone(ev)
        self.assertEqual(fsm.opened_count, 0)


class TestCounterAnimationAndState(unittest.TestCase):
    def test_ease_out_cubic_animation_reaches_target_monotonically(self) -> None:
        anim = CounterAnimationState()
        anim.set_immediate(100)
        self.assertTrue(anim.animate_to(118, duration_ms=500.0))

        prev = 100
        for _ in range(40):
            val = anim.step(16.0)
            self.assertGreaterEqual(val, prev)
            self.assertLessEqual(val, 118)
            prev = val

        self.assertEqual(anim.displayed_value, 118)
        self.assertFalse(anim.is_animating)

    def test_chest_counter_sync_tracks_session_delta(self) -> None:
        counter = ChestCounter(initial_total=12440)
        counter.sync_total(12458)
        self.assertEqual(counter.total, 12458)
        self.assertEqual(counter.session_chests, 18)


if __name__ == "__main__":
    unittest.main()
