"""Independent OpenCV Computer Vision detector for Fortnite chest openings.

CRITICAL DESIGN PRINCIPLE:
This detector does NOT count chests merely visible on the map.
It specifically detects the *completion of a chest-opening interaction* using a
4-stage temporal state machine (`ChestOpeningFSM`):
  1. IDLE -> PROMPT_VISIBLE / SEARCHING:
     Detects the local HUD interact prompt (`[E] Search` / `Обыскать`) AND/OR
     the radial progress ring beginning to fill smoothly around screen center.
  2. SEARCHING:
     Tracks monotonic radial arc coverage (`ring_fill_ratio`) over 0.30s–1.60s.
     If the player cancels the search early (ring vanishes before reaching >=78%
     fill), the FSM aborts cleanly back to IDLE without incrementing the counter.
  3. CONFIRMING -> OPENED:
     Once the progress ring completes (>=78% fill) and clears, confirms the exact
     opening event via golden loot-burst luminance/saturation spike or HUD XP
     accolade flash (or sustained >=88% full-ring completion), then increments
     the chest counter by +1 and enters a 1.8s cooldown to prevent duplicates.

Completely independent of Fortnite's game process and memory (100% EAC-safe).
Supports both offline video analysis (`analyze_video`) and live desktop screen
monitoring (`LiveVisualMonitor` via `mss` + OpenCV).
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from database.models import (
    AnalysisResult,
    AnalysisStatus,
    AttributionMode,
    ChestEventRecord,
    DetectionSource,
)


class VisualState(str, Enum):
    IDLE = "idle"
    PROMPT_VISIBLE = "prompt_visible"
    SEARCHING = "searching"
    CONFIRMING = "confirming"
    COOLDOWN = "cooldown"


@dataclass(slots=True)
class VisualDetectorConfig:
    """Tunable thresholds for the temporal chest-opening CV pipeline."""

    prompt_threshold: float = 0.42
    ring_start_threshold: float = 0.12
    ring_complete_threshold: float = 0.78
    ring_high_confidence_threshold: float = 0.88
    ring_clear_threshold: float = 0.14
    min_search_duration_s: float = 0.28
    max_search_duration_s: float = 1.75
    confirm_window_s: float = 0.95
    burst_threshold: float = 0.48
    xp_threshold: float = 0.45
    cooldown_s: float = 1.80
    prompt_timeout_s: float = 2.00
    min_monotonic_steps: int = 2


@dataclass(slots=True)
class FrameFeatures:
    """Extracted per-frame HUD and visual confirmation features (normalized 0.0..1.0)."""

    timestamp_s: float
    prompt_score: float = 0.0
    ring_fill_ratio: float = 0.0
    loot_burst_score: float = 0.0
    xp_banner_score: float = 0.0


class ChestOpeningFSM:
    """Deterministic temporal state machine verifying genuine chest-opening completions.

    Pure Python logic decoupled from OpenCV so it can be exhaustively unit-tested
    across edge cases (looking at closed chests, aborted searches, rapid openings,
    and false-positive flashes).
    """

    def __init__(self, config: Optional[VisualDetectorConfig] = None) -> None:
        self.cfg = config or VisualDetectorConfig()
        self.state: VisualState = VisualState.IDLE
        self._events: list[ChestEventRecord] = []

        self._prompt_seen_at: float = 0.0
        self._search_started_at: float = 0.0
        self._search_peak_fill: float = 0.0
        self._last_ring_fill: float = 0.0
        self._monotonic_steps: int = 0
        self._confirm_started_at: float = 0.0
        self._confirm_peak_fill: float = 0.0
        self._confirm_had_prompt: bool = False
        self._cooldown_until: float = 0.0
        self._event_seq: int = 0

    @property
    def opened_count(self) -> int:
        return len(self._events)

    @property
    def events(self) -> list[ChestEventRecord]:
        return list(self._events)

    def reset(self) -> None:
        self.state = VisualState.IDLE
        self._events.clear()
        self._prompt_seen_at = 0.0
        self._search_started_at = 0.0
        self._search_peak_fill = 0.0
        self._last_ring_fill = 0.0
        self._monotonic_steps = 0
        self._confirm_started_at = 0.0
        self._confirm_peak_fill = 0.0
        self._confirm_had_prompt = False
        self._cooldown_until = 0.0
        self._event_seq = 0

    def update(self, features: FrameFeatures) -> Optional[ChestEventRecord]:
        """Process one frame's extracted features; return ChestEventRecord on verified open."""
        t = float(features.timestamp_s)

        if self.state == VisualState.COOLDOWN:
            if t >= self._cooldown_until:
                self.state = VisualState.IDLE
            else:
                return None

        if self.state == VisualState.IDLE:
            if features.prompt_score >= self.cfg.prompt_threshold:
                self.state = VisualState.PROMPT_VISIBLE
                self._prompt_seen_at = t
                self._confirm_had_prompt = True
            if features.ring_fill_ratio >= self.cfg.ring_start_threshold:
                self._enter_searching(t, features.ring_fill_ratio, had_prompt=self._confirm_had_prompt)
            return None

        if self.state == VisualState.PROMPT_VISIBLE:
            if features.ring_fill_ratio >= self.cfg.ring_start_threshold:
                self._enter_searching(t, features.ring_fill_ratio, had_prompt=True)
                return None
            if features.prompt_score >= self.cfg.prompt_threshold:
                self._prompt_seen_at = t
            elif (t - self._prompt_seen_at) > self.cfg.prompt_timeout_s:
                self._reset_to_idle()
            return None

        if self.state == VisualState.SEARCHING:
            elapsed = t - self._search_started_at
            if elapsed > self.cfg.max_search_duration_s:
                self._reset_to_idle()
                return None

            fill = features.ring_fill_ratio
            if fill > self._last_ring_fill + 0.03:
                self._monotonic_steps += 1
            if fill > self._search_peak_fill:
                self._search_peak_fill = fill
            self._last_ring_fill = fill

            # Ring cleared -> either completed or cancelled early
            if fill <= self.cfg.ring_clear_threshold:
                if (
                    self._search_peak_fill >= self.cfg.ring_complete_threshold
                    and elapsed >= self.cfg.min_search_duration_s
                    and self._monotonic_steps >= self.cfg.min_monotonic_steps
                ):
                    self.state = VisualState.CONFIRMING
                    self._confirm_started_at = t
                    self._confirm_peak_fill = self._search_peak_fill
                    # Check if burst/XP is already visible on this exact completion frame
                    return self._check_confirmation(features)
                # Aborted search before completion!
                self._reset_to_idle()
                return None

            # Immediate confirmation if ring hit >= 92% AND burst/XP already triggered
            if (
                fill >= 0.92
                and elapsed >= self.cfg.min_search_duration_s
                and self._monotonic_steps >= self.cfg.min_monotonic_steps
                and (
                    features.loot_burst_score >= self.cfg.burst_threshold
                    or features.xp_banner_score >= self.cfg.xp_threshold
                )
            ):
                return self._emit_open_event(t, confidence=0.95)
            return None

        if self.state == VisualState.CONFIRMING:
            return self._check_confirmation(features)

        return None

    def _enter_searching(self, timestamp_s: float, initial_fill: float, had_prompt: bool) -> None:
        self.state = VisualState.SEARCHING
        self._search_started_at = timestamp_s
        self._search_peak_fill = initial_fill
        self._last_ring_fill = initial_fill
        self._monotonic_steps = 1 if initial_fill >= self.cfg.ring_start_threshold else 0
        self._confirm_had_prompt = had_prompt

    def _check_confirmation(self, features: FrameFeatures) -> Optional[ChestEventRecord]:
        t = features.timestamp_s
        has_burst = features.loot_burst_score >= self.cfg.burst_threshold
        has_xp = features.xp_banner_score >= self.cfg.xp_threshold

        if has_burst or has_xp:
            conf = 0.96 if (has_burst and has_xp) else 0.90
            return self._emit_open_event(t, confidence=conf)

        if (t - self._confirm_started_at) >= self.cfg.confirm_window_s:
            # High-confidence full ring completion paired with detected interact prompt
            if (
                self._confirm_had_prompt
                and self._confirm_peak_fill >= self.cfg.ring_high_confidence_threshold
            ):
                return self._emit_open_event(t, confidence=0.82)
            self._reset_to_idle()
        return None

    def _emit_open_event(self, timestamp_s: float, confidence: float) -> ChestEventRecord:
        self._event_seq += 1
        event = ChestEventRecord(
            timestamp_ms=int(round(timestamp_s * 1000.0)),
            container_guid=f"visual_chest_{self._event_seq:04d}",
            container_class="Visual_Chest_Opened",
            search_count=1,
            opened_by_local_player=True,
            confidence=confidence,
        )
        self._events.append(event)
        self.state = VisualState.COOLDOWN
        self._cooldown_until = timestamp_s + self.cfg.cooldown_s
        self._search_peak_fill = 0.0
        self._last_ring_fill = 0.0
        self._monotonic_steps = 0
        self._confirm_had_prompt = False
        return event

    def _reset_to_idle(self) -> None:
        self.state = VisualState.IDLE
        self._search_peak_fill = 0.0
        self._last_ring_fill = 0.0
        self._monotonic_steps = 0
        self._confirm_had_prompt = False


class VisualChestDetector:
    """OpenCV-based frame feature extractor + ChestOpeningFSM pipeline."""

    def __init__(self, config: Optional[VisualDetectorConfig] = None) -> None:
        self.config = config or VisualDetectorConfig()
        self.fsm = ChestOpeningFSM(self.config)
        self._prev_burst_luma: Optional[float] = None

    def reset(self) -> None:
        self.fsm.reset()
        self._prev_burst_luma = None

    @staticmethod
    def is_opencv_available() -> bool:
        try:
            import cv2  # noqa: F401
            import numpy  # noqa: F401

            return True
        except ImportError:
            return False

    def extract_features_bgr(self, frame_bgr, timestamp_s: float) -> FrameFeatures:
        """Extract normalized chest-opening features from an OpenCV BGR frame.

        Resolution-independent normalized ROIs (calibrated for 16:9 Fortnite HUD):
        - Interact Prompt ROI: x in [0.43, 0.62], y in [0.54, 0.67]
        - Radial Progress Ring ROI: centered at (0.50, 0.50), annulus radius [2.4%, 4.2%] of height
        - Golden Loot Burst ROI: x in [0.36, 0.64], y in [0.34, 0.66]
        - XP Accolade Banner ROI: x in [0.36, 0.64], y in [0.10, 0.24]
        """
        import cv2
        import numpy as np

        h, w = frame_bgr.shape[:2]
        if h < 120 or w < 160:
            return FrameFeatures(timestamp_s=timestamp_s)

        hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)

        # 1. Interact Prompt Badge Score (high-contrast bright text/keycap in interact ROI)
        px1, px2 = int(w * 0.43), int(w * 0.62)
        py1, py2 = int(h * 0.54), int(h * 0.67)
        prompt_hsv = hsv[py1:py2, px1:px2]
        bright_white = (prompt_hsv[:, :, 1] < 55) & (prompt_hsv[:, :, 2] > 215)
        dark_backing = prompt_hsv[:, :, 2] < 70
        white_ratio = float(np.mean(bright_white))
        dark_ratio = float(np.mean(dark_backing))
        # Fortnite prompt pill has bright white key/text over dark translucent backdrop
        prompt_score = 0.0
        if 0.015 <= white_ratio <= 0.35 and dark_ratio >= 0.15:
            prompt_score = min(1.0, (white_ratio / 0.10) * 0.6 + min(dark_ratio / 0.35, 1.0) * 0.4)

        # 2. Radial Search Progress Ring Coverage around screen center (72 angular bins)
        cx, cy = w * 0.5, h * 0.5
        r_inner = max(8.0, h * 0.025)
        r_outer = max(14.0, h * 0.043)
        bins = 72
        filled_bins = 0
        for b in range(bins):
            angle = (2.0 * math.pi * b) / bins - (math.pi / 2.0)
            cos_a = math.cos(angle)
            sin_a = math.sin(angle)
            bin_hit = False
            for frac in (0.25, 0.5, 0.75):
                r = r_inner + (r_outer - r_inner) * frac
                sx = int(round(cx + cos_a * r))
                sy = int(round(cy + sin_a * r))
                if 0 <= sx < w and 0 <= sy < h:
                    _hue, sat, val = hsv[sy, sx]
                    # Bright white/gold progress arc stroke
                    if val >= 225 and (sat <= 65 or (15 <= _hue <= 38 and sat >= 100)):
                        bin_hit = True
                        break
            if bin_hit:
                filled_bins += 1
        ring_fill_ratio = filled_bins / float(bins)

        # 3. Golden Loot Burst Spike (warm gold hues H in [15..36], S >= 110, V >= 190)
        bx1, bx2 = int(w * 0.36), int(w * 0.64)
        by1, by2 = int(h * 0.34), int(h * 0.66)
        burst_hsv = hsv[by1:by2, bx1:bx2]
        gold_mask = (
            (burst_hsv[:, :, 0] >= 15)
            & (burst_hsv[:, :, 0] <= 36)
            & (burst_hsv[:, :, 1] >= 110)
            & (burst_hsv[:, :, 2] >= 190)
        )
        gold_ratio = float(np.mean(gold_mask))
        mean_luma = float(np.mean(burst_hsv[:, :, 2])) / 255.0
        luma_delta = 0.0
        if self._prev_burst_luma is not None:
            luma_delta = max(0.0, mean_luma - self._prev_burst_luma)
        self._prev_burst_luma = mean_luma
        loot_burst_score = min(1.0, gold_ratio * 4.5 + luma_delta * 2.5)

        # 4. XP Accolade Toast Flash (amber/cyan bright banner in upper-center HUD)
        xx1, xx2 = int(w * 0.36), int(w * 0.64)
        xy1, xy2 = int(h * 0.10), int(h * 0.24)
        xp_hsv = hsv[xy1:xy2, xx1:xx2]
        xp_gold = (
            (xp_hsv[:, :, 0] >= 16)
            & (xp_hsv[:, :, 0] <= 35)
            & (xp_hsv[:, :, 1] >= 120)
            & (xp_hsv[:, :, 2] >= 200)
        )
        xp_cyan = (
            (xp_hsv[:, :, 0] >= 85)
            & (xp_hsv[:, :, 0] <= 112)
            & (xp_hsv[:, :, 1] >= 100)
            & (xp_hsv[:, :, 2] >= 200)
        )
        xp_ratio = float(np.mean(xp_gold | xp_cyan))
        xp_banner_score = min(1.0, xp_ratio * 6.0)

        return FrameFeatures(
            timestamp_s=timestamp_s,
            prompt_score=prompt_score,
            ring_fill_ratio=ring_fill_ratio,
            loot_burst_score=loot_burst_score,
            xp_banner_score=xp_banner_score,
        )

    def process_frame_bgr(self, frame_bgr, timestamp_s: float) -> Optional[ChestEventRecord]:
        features = self.extract_features_bgr(frame_bgr, timestamp_s)
        return self.fsm.update(features)

    def analyze_video(
        self,
        video_path: str | Path,
        sample_fps: float = 10.0,
    ) -> AnalysisResult:
        """Analyze a recorded Fortnite gameplay video (.mp4/.mkv/.avi) for opened chests."""
        path = Path(video_path)
        if not path.is_file():
            return AnalysisResult(
                status=AnalysisStatus.FILE_NOT_FOUND,
                replay_id=path.name,
                file_path=str(path),
                file_hash="",
                source=DetectionSource.VISUAL_CV,
                error_message=f"Видеофайл не найден: {path}",
            )

        if not self.is_opencv_available():
            return AnalysisResult(
                status=AnalysisStatus.ERROR,
                replay_id=path.name,
                file_path=str(path),
                file_hash="",
                source=DetectionSource.VISUAL_CV,
                error_message="OpenCV (opencv-python) не установлен. Установите зависимости из requirements.txt.",
            )

        import cv2

        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        file_hash = digest.hexdigest()

        self.reset()
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            return AnalysisResult(
                status=AnalysisStatus.CORRUPTED,
                replay_id=f"video:{file_hash[:24]}",
                file_path=str(path),
                file_hash=file_hash,
                source=DetectionSource.VISUAL_CV,
                error_message=f"Не удалось открыть видеофайл через OpenCV: {path.name}",
            )

        try:
            native_fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            step = max(1, int(round(native_fps / max(1.0, sample_fps))))
            frame_idx = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if frame_idx % step == 0:
                    ts = frame_idx / native_fps
                    self.process_frame_bgr(frame, ts)
                frame_idx += 1
            duration_ms = int((frame_count / native_fps) * 1000.0) if native_fps > 0 else 0
        finally:
            cap.release()

        return AnalysisResult(
            status=AnalysisStatus.SUCCESS,
            replay_id=f"video:{file_hash[:24]}",
            file_path=str(path),
            file_hash=file_hash,
            chests_opened=self.fsm.opened_count,
            source=DetectionSource.VISUAL_CV,
            attribution_mode=AttributionMode.VISUAL_LOCAL_HUD,
            match_timestamp=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
            match_duration_ms=duration_ms,
            friendly_name=path.stem,
            engine_version="OpenCV-VisualFSM",
            events=self.fsm.events,
        )


class LiveVisualMonitor:
    """Background thread capturing desktop frames via `mss` and running `VisualChestDetector`.

    Zero game hooks, zero memory reads — captures standard OS desktop frames only.
    """

    def __init__(
        self,
        on_chest_opened: Callable[[ChestEventRecord], None],
        on_error: Optional[Callable[[str], None]] = None,
        fps: float = 10.0,
        monitor_index: int = 1,
        config: Optional[VisualDetectorConfig] = None,
    ) -> None:
        self._on_chest_opened = on_chest_opened
        self._on_error = on_error
        self._fps = max(2.0, min(30.0, float(fps)))
        self._monitor_index = max(1, int(monitor_index))
        self._detector = VisualChestDetector(config=config)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if self.is_running:
            return True
        if not VisualChestDetector.is_opencv_available():
            if self._on_error:
                self._on_error("OpenCV не установлен — визуальный мониторинг недоступен.")
            return False
        try:
            import mss  # noqa: F401
        except ImportError:
            if self._on_error:
                self._on_error("Пакет 'mss' не установлен — захват экрана недоступен.")
            return False

        self._detector.reset()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="ChestMemory-VisualMonitor",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _run_loop(self) -> None:
        import cv2
        import mss
        import numpy as np

        interval = 1.0 / self._fps
        t0 = time.monotonic()
        try:
            with mss.mss() as sct:
                mon_idx = min(self._monitor_index, len(sct.monitors) - 1)
                monitor = sct.monitors[mon_idx]
                while not self._stop_event.is_set():
                    tick_start = time.monotonic()
                    raw_img = np.asarray(sct.grab(monitor))
                    frame_bgr = cv2.cvtColor(raw_img, cv2.COLOR_BGRA2BGR)
                    event = self._detector.process_frame_bgr(frame_bgr, tick_start - t0)
                    if event is not None:
                        self._on_chest_opened(event)
                    sleep_for = interval - (time.monotonic() - tick_start)
                    if sleep_for > 0:
                        self._stop_event.wait(sleep_for)
        except Exception as exc:  # pragma: no cover - OS capture errors on Windows
            if self._on_error:
                self._on_error(f"Ошибка визуального мониторинга: {exc}")
