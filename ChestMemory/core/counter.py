"""Counter state and smooth ease-out animation interpolator for ChestMemory."""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(slots=True)
class CounterAnimationState:
    """Pure-logic ease-out cubic interpolator for the hero chest counter."""

    start_value: int = 0
    target_value: int = 0
    displayed_value: int = 0
    elapsed_ms: float = 0.0
    duration_ms: float = 750.0
    is_animating: bool = False

    def set_immediate(self, value: int) -> None:
        val = max(0, int(value))
        self.start_value = val
        self.target_value = val
        self.displayed_value = val
        self.elapsed_ms = self.duration_ms
        self.is_animating = False

    def animate_to(self, new_target: int, duration_ms: float = 750.0) -> bool:
        """Start an animation from the current displayed value to `new_target`.

        Returns True if an animation was started, False if value was unchanged.
        """
        target = max(0, int(new_target))
        if target == self.target_value and not self.is_animating:
            return False
        self.start_value = self.displayed_value
        self.target_value = target
        self.elapsed_ms = 0.0
        self.duration_ms = max(50.0, float(duration_ms))
        self.is_animating = self.start_value != self.target_value
        if not self.is_animating:
            self.displayed_value = self.target_value
        return self.is_animating

    def step(self, delta_ms: float) -> int:
        """Advance animation by `delta_ms` and return the current integer value."""
        if not self.is_animating:
            return self.displayed_value

        self.elapsed_ms += max(0.0, float(delta_ms))
        progress = min(1.0, self.elapsed_ms / self.duration_ms)
        # Smooth ease-out cubic: 1 - (1 - t)^3
        eased = 1.0 - (1.0 - progress) ** 3
        interpolated = self.start_value + (self.target_value - self.start_value) * eased
        self.displayed_value = int(round(interpolated))

        if progress >= 1.0:
            self.displayed_value = self.target_value
            self.is_animating = False
        return self.displayed_value


class ChestCounter:
    """Thread-safe in-memory chest counter synchronized with persistent totals."""

    def __init__(self, initial_total: int = 0) -> None:
        self._lock = threading.RLock()
        self._total = max(0, int(initial_total))
        self._session_chests = 0
        self._live_match_chests = 0

    @property
    def total(self) -> int:
        with self._lock:
            return self._total

    @property
    def session_chests(self) -> int:
        with self._lock:
            return self._session_chests

    @property
    def live_match_chests(self) -> int:
        with self._lock:
            return self._live_match_chests

    def sync_total(self, authoritative_total: int) -> int:
        """Synchronize total with the authoritative SQLite aggregate."""
        with self._lock:
            new_total = max(0, int(authoritative_total))
            delta = new_total - self._total
            if delta > 0:
                self._session_chests += delta
            self._total = new_total
            return self._total

    def increment_live_match(self, delta: int = 1) -> int:
        """Increment live visual-detector match tally before final persistence."""
        with self._lock:
            self._live_match_chests += max(0, int(delta))
            return self._live_match_chests

    def reset_live_match(self) -> int:
        """Reset live visual match tally and return the previous count."""
        with self._lock:
            prev = self._live_match_chests
            self._live_match_chests = 0
            return prev
