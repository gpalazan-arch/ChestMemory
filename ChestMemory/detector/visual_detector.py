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

from collections import deque

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

    prompt_threshold: float = 0.34
    ring_start_threshold: float = 0.12
    ring_complete_threshold: float = 0.66
    ring_high_confidence_threshold: float = 0.80
    ring_clear_threshold: float = 0.15
    min_search_duration_s: float = 0.18
    max_search_duration_s: float = 1.90
    confirm_window_s: float = 0.95
    burst_threshold: float = 0.40
    xp_threshold: float = 0.38
    cooldown_s: float = 1.50
    prompt_timeout_s: float = 2.20
    min_monotonic_steps: int = 2


@dataclass(slots=True)
class FrameFeatures:
    """Extracted per-frame HUD and visual confirmation features (normalized 0.0..1.0)."""

    timestamp_s: float
    prompt_score: float = 0.0
    ring_fill_ratio: float = 0.0
    loot_burst_score: float = 0.0
    xp_banner_score: float = 0.0
    frame_mean_luma: float = 128.0


@dataclass(slots=True)
class VisualDiagnostics:
    """Live telemetry snapshot for verifying that OpenCV sees Fortnite clearly."""

    running: bool = False
    monitor_index: int = 1
    fps: float = 0.0
    frame_width: int = 0
    frame_height: int = 0
    frame_mean_luma: float = 0.0
    black_frame_warning: bool = False
    fsm_state: str = "idle"
    last_prompt_score: float = 0.0
    last_ring_fill: float = 0.0
    last_burst_score: float = 0.0
    last_xp_score: float = 0.0
    peak_prompt_10s: float = 0.0
    peak_ring_10s: float = 0.0
    peak_burst_10s: float = 0.0
    peak_xp_10s: float = 0.0
    last_event_note: str = ""
    detected_session_chests: int = 0


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
        self.last_note: str = "Ожидание обыска сундука..."

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
                self.last_note = (
                    f"Сброс обыска: ring_peak={self._search_peak_fill:.2f}, "
                    f"t={elapsed:.2f}s, steps={self._monotonic_steps}"
                )
                self._reset_to_idle()
                return None

            # Immediate confirmation if ring hit >= 90% AND burst/XP already triggered
            if (
                fill >= 0.90
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
            self.last_note = (
                f"Не подтверждено (нет burst/xp): ring_peak={self._confirm_peak_fill:.2f}, "
                f"prompt={self._confirm_had_prompt}"
            )
            self._reset_to_idle()
        return None

    def _emit_open_event(self, timestamp_s: float, confidence: float) -> ChestEventRecord:
        self._event_seq += 1
        self.last_note = f"Засчитан сундук #{self._event_seq} (уверенность {confidence:.0%})"
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

        Resolution- and HUD-scale-independent pipeline calibrated for Fortnite:
        - Detects black/occluded capture frames (e.g. Exclusive Fullscreen).
        - Locates the radial interact progress ring both at crosshair center
          `(0.50, 0.50)` AND floating world-space interact positions inside
          `x in [0.34..0.66], y in [0.34..0.70]` across multiple radii
          (`1.5%..4.6%` of height), requiring dark-inner / bright-arc annulus
          contrast so bright sky/snow never causes false positives.
        - Measures golden chest-lid burst and HUD XP accolade flashes.
        """
        import cv2
        import numpy as np

        h, w = frame_bgr.shape[:2]
        if h < 120 or w < 160:
            return FrameFeatures(timestamp_s=timestamp_s, frame_mean_luma=0.0)

        # Downscale 1440p/4K frames proportionally to ~1080p height for fast, consistent CV
        if h > 1080:
            scale = 1080.0 / float(h)
            frame_bgr = cv2.resize(
                frame_bgr,
                (int(round(w * scale)), 1080),
                interpolation=cv2.INTER_AREA,
            )
            h, w = frame_bgr.shape[:2]

        hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
        mean_luma_frame = float(np.mean(hsv[::8, ::8, 2]))
        if mean_luma_frame < 2.0:
            # Exclusive fullscreen black capture — do not feed garbage into FSM
            return FrameFeatures(timestamp_s=timestamp_s, frame_mean_luma=mean_luma_frame)

        # 1. Interact Prompt Badge Score (wide central zone covering HUD scale 50%..125%)
        px1, px2 = int(w * 0.30), int(w * 0.70)
        py1, py2 = int(h * 0.32), int(h * 0.72)
        prompt_hsv = hsv[py1:py2, px1:px2]
        bright_white = (prompt_hsv[:, :, 1] < 70) & (prompt_hsv[:, :, 2] > 200)
        dark_backing = prompt_hsv[:, :, 2] < 85
        white_ratio = float(np.mean(bright_white))
        dark_ratio = float(np.mean(dark_backing))
        prompt_score = 0.0
        if 0.004 <= white_ratio <= 0.35 and dark_ratio >= 0.06:
            prompt_score = min(
                1.0,
                (white_ratio / 0.045) * 0.60 + min(dark_ratio / 0.22, 1.0) * 0.40,
            )

        # 2. Multi-scale Annulus-Contrast Radial Progress Ring Detection
        # Candidate centers: crosshair center + bright compact keycap/ring centroids in zone
        candidate_centers: list[tuple[float, float]] = [(w * 0.50, h * 0.50)]

        gray_zone = hsv[py1:py2, px1:px2, 2]
        bright_mask = ((hsv[py1:py2, px1:px2, 1] < 80) & (gray_zone >= 205)).astype(np.uint8) * 255
        contours, _ = cv2.findContours(bright_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        zone_candidates: list[tuple[float, float, float]] = []
        min_area = max(9.0, (h * 0.006) ** 2)
        max_area = (h * 0.10) ** 2
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if min_area <= area <= max_area:
                bx, by, bw, bh = cv2.boundingRect(cnt)
                aspect = bw / float(max(1, bh))
                if 0.50 <= aspect <= 1.95:
                    cx_c = px1 + bx + bw * 0.5
                    cy_c = py1 + by + bh * 0.5
                    dist_center = math.hypot(cx_c - w * 0.5, cy_c - h * 0.52)
                    zone_candidates.append((dist_center, cx_c, cy_c))
        zone_candidates.sort(key=lambda item: item[0])
        for _d, cx_c, cy_c in zone_candidates[:5]:
            candidate_centers.append((cx_c, cy_c))

        radii = [
            max(8.0, h * frac)
            for frac in (0.011, 0.015, 0.020, 0.026, 0.033, 0.042)
        ]
        ring_fill_ratio = self._measure_best_annulus_ring(hsv, candidate_centers, radii)

        # 3. Golden Loot Burst Spike (central chest-opening area)
        bx1, bx2 = int(w * 0.30), int(w * 0.70)
        by1, by2 = int(h * 0.30), int(h * 0.72)
        burst_hsv = hsv[by1:by2, bx1:bx2]
        gold_mask = (
            (burst_hsv[:, :, 0] >= 14)
            & (burst_hsv[:, :, 0] <= 38)
            & (burst_hsv[:, :, 1] >= 100)
            & (burst_hsv[:, :, 2] >= 185)
        )
        gold_ratio = float(np.mean(gold_mask))
        mean_luma = float(np.mean(burst_hsv[:, :, 2])) / 255.0
        luma_delta = 0.0
        if self._prev_burst_luma is not None:
            luma_delta = max(0.0, mean_luma - self._prev_burst_luma)
        self._prev_burst_luma = mean_luma
        loot_burst_score = min(1.0, gold_ratio * 4.8 + luma_delta * 2.8)

        # 4. XP Accolade Toast Flash (check both upper-center AND lower-left XP bar/feed ROIs)
        xp_rois = (
            hsv[int(h * 0.08) : int(h * 0.25), int(w * 0.34) : int(w * 0.66)],
            hsv[int(h * 0.72) : int(h * 0.90), int(w * 0.04) : int(w * 0.32)],
        )
        best_xp = 0.0
        for xp_hsv in xp_rois:
            if xp_hsv.size == 0:
                continue
            xp_gold = (
                (xp_hsv[:, :, 0] >= 15)
                & (xp_hsv[:, :, 0] <= 36)
                & (xp_hsv[:, :, 1] >= 115)
                & (xp_hsv[:, :, 2] >= 195)
            )
            xp_cyan = (
                (xp_hsv[:, :, 0] >= 84)
                & (xp_hsv[:, :, 0] <= 114)
                & (xp_hsv[:, :, 1] >= 95)
                & (xp_hsv[:, :, 2] >= 195)
            )
            best_xp = max(best_xp, min(1.0, float(np.mean(xp_gold | xp_cyan)) * 6.5))

        return FrameFeatures(
            timestamp_s=timestamp_s,
            prompt_score=prompt_score,
            ring_fill_ratio=ring_fill_ratio,
            loot_burst_score=loot_burst_score,
            xp_banner_score=best_xp,
            frame_mean_luma=mean_luma_frame,
        )

    @staticmethod
    def _measure_best_annulus_ring(
        hsv,
        centers: list[tuple[float, float]],
        radii: list[float],
    ) -> float:
        """Measure radial arc coverage requiring contrast between the bright ring stroke
        and its darker inner/outer track (eliminates bright sky / white wall false hits)."""
        import numpy as np

        h, w = hsv.shape[:2]
        bins = 48
        angles = np.linspace(-math.pi / 2.0, 1.5 * math.pi, bins, endpoint=False)
        cos_a = np.cos(angles)
        sin_a = np.sin(angles)

        best_fill = 0.0
        for cx, cy in centers:
            for radius in radii:
                if (
                    cx - radius - 5 < 0
                    or cx + radius + 5 >= w
                    or cy - radius - 5 < 0
                    or cy + radius + 5 >= h
                ):
                    continue

                rx = np.rint(cx + cos_a * radius).astype(np.int32)
                ry = np.rint(cy + sin_a * radius).astype(np.int32)
                r_sat = hsv[ry, rx, 1].astype(np.int32)
                r_val = hsv[ry, rx, 2].astype(np.int32)
                r_hue = hsv[ry, rx, 0].astype(np.int32)

                # Inner and outer comparison rings to verify stroke contrast (rejects flat bright sky)
                r_in = max(3.0, radius * 0.68)
                r_out = radius * 1.32
                ix = np.clip(np.rint(cx + cos_a * r_in).astype(np.int32), 0, w - 1)
                iy = np.clip(np.rint(cy + sin_a * r_in).astype(np.int32), 0, h - 1)
                ox = np.clip(np.rint(cx + cos_a * r_out).astype(np.int32), 0, w - 1)
                oy = np.clip(np.rint(cy + sin_a * r_out).astype(np.int32), 0, h - 1)

                in_val = hsv[iy, ix, 2].astype(np.int32)
                out_val = hsv[oy, ox, 2].astype(np.int32)
                bg_val = np.minimum(in_val, out_val)

                bright_stroke = (r_val >= 200) & (
                    (r_sat <= 80) | ((r_hue >= 14) & (r_hue <= 40) & (r_sat >= 85))
                )
                has_contrast = (r_val - bg_val) >= 30
                hits = bright_stroke & has_contrast

                fill = float(np.mean(hits))
                if fill > best_fill:
                    best_fill = fill
        return best_fill

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


def enumerate_monitors() -> list[tuple[int, str]]:
    """Return available desktop monitors for Visual CV selection via `mss`."""
    try:
        import mss

        with mss.mss() as sct:
            result: list[tuple[int, str]] = []
            for idx, mon in enumerate(sct.monitors[1:], start=1):
                w = mon.get("width", 0)
                h = mon.get("height", 0)
                result.append((idx, f"Монитор {idx} ({w}x{h})"))
            return result or [(1, "Монитор 1 (Основной)")]
    except Exception:
        return [(1, "Монитор 1 (Основной)")]


class LiveVisualMonitor:
    """Background thread capturing desktop frames via `mss` and running `VisualChestDetector`.

    Zero game hooks, zero memory reads — captures standard OS desktop frames only.
    Automatically recovers from transient Windows capture errors (Alt-Tab / UAC /
    display mode switches), detects black frames (Exclusive Fullscreen), and exposes
    real-time + 10-second peak `VisualDiagnostics`.
    """

    def __init__(
        self,
        on_chest_opened: Callable[[ChestEventRecord], None],
        on_error: Optional[Callable[[str], None]] = None,
        on_diagnostics: Optional[Callable[[VisualDiagnostics], None]] = None,
        fps: float = 15.0,
        monitor_index: int = 1,
        config: Optional[VisualDetectorConfig] = None,
    ) -> None:
        self._on_chest_opened = on_chest_opened
        self._on_error = on_error
        self._on_diagnostics = on_diagnostics
        self._fps = max(8.0, min(30.0, float(fps)))
        self._monitor_index = max(1, int(monitor_index))
        self._detector = VisualChestDetector(config=config)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._diag_lock = threading.Lock()
        self._diagnostics = VisualDiagnostics(monitor_index=self._monitor_index)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def diagnostics(self) -> VisualDiagnostics:
        with self._diag_lock:
            d = self._diagnostics
            return VisualDiagnostics(
                running=self.is_running,
                monitor_index=d.monitor_index,
                fps=d.fps,
                frame_width=d.frame_width,
                frame_height=d.frame_height,
                frame_mean_luma=d.frame_mean_luma,
                black_frame_warning=d.black_frame_warning,
                fsm_state=d.fsm_state,
                last_prompt_score=d.last_prompt_score,
                last_ring_fill=d.last_ring_fill,
                last_burst_score=d.last_burst_score,
                last_xp_score=d.last_xp_score,
                peak_prompt_10s=d.peak_prompt_10s,
                peak_ring_10s=d.peak_ring_10s,
                peak_burst_10s=d.peak_burst_10s,
                peak_xp_10s=d.peak_xp_10s,
                last_event_note=d.last_event_note,
                detected_session_chests=d.detected_session_chests,
            )

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
        with self._diag_lock:
            self._diagnostics.running = False

    def _run_loop(self) -> None:
        import cv2
        import mss
        import numpy as np

        interval = 1.0 / self._fps
        t0 = time.monotonic()
        frames_in_window = 0
        fps_window_start = t0
        measured_fps = 0.0
        black_streak = 0
        last_diag_emit = 0.0
        # Rolling 10-second feature history for human-readable peak telemetry
        peak_window: deque[tuple[float, float, float, float, float]] = deque()

        while not self._stop_event.is_set():
            try:
                with mss.mss() as sct:
                    mon_idx = min(self._monitor_index, len(sct.monitors) - 1)
                    monitor = sct.monitors[mon_idx]
                    while not self._stop_event.is_set():
                        tick_start = time.monotonic()
                        raw_img = np.asarray(sct.grab(monitor))
                        frame_bgr = cv2.cvtColor(raw_img, cv2.COLOR_BGRA2BGR)
                        features = self._detector.extract_features_bgr(frame_bgr, tick_start - t0)

                        if features.frame_mean_luma < 2.0:
                            black_streak += 1
                            event = None
                        else:
                            black_streak = 0
                            event = self._detector.fsm.update(features)

                        frames_in_window += 1
                        dt_fps = tick_start - fps_window_start
                        if dt_fps >= 1.0:
                            measured_fps = frames_in_window / dt_fps
                            frames_in_window = 0
                            fps_window_start = tick_start

                        peak_window.append(
                            (
                                tick_start,
                                features.prompt_score,
                                features.ring_fill_ratio,
                                features.loot_burst_score,
                                features.xp_banner_score,
                            )
                        )
                        cutoff = tick_start - 10.0
                        while peak_window and peak_window[0][0] < cutoff:
                            peak_window.popleft()

                        pk_p = max((x[1] for x in peak_window), default=0.0)
                        pk_r = max((x[2] for x in peak_window), default=0.0)
                        pk_b = max((x[3] for x in peak_window), default=0.0)
                        pk_x = max((x[4] for x in peak_window), default=0.0)

                        black_warn = black_streak >= int(self._fps * 1.5)
                        h, w = frame_bgr.shape[:2]
                        snap = VisualDiagnostics(
                            running=True,
                            monitor_index=mon_idx,
                            fps=round(measured_fps, 1),
                            frame_width=w,
                            frame_height=h,
                            frame_mean_luma=round(features.frame_mean_luma, 1),
                            black_frame_warning=black_warn,
                            fsm_state=self._detector.fsm.state.value,
                            last_prompt_score=round(features.prompt_score, 3),
                            last_ring_fill=round(features.ring_fill_ratio, 3),
                            last_burst_score=round(features.loot_burst_score, 3),
                            last_xp_score=round(features.xp_banner_score, 3),
                            peak_prompt_10s=round(pk_p, 2),
                            peak_ring_10s=round(pk_r, 2),
                            peak_burst_10s=round(pk_b, 2),
                            peak_xp_10s=round(pk_x, 2),
                            last_event_note=self._detector.fsm.last_note,
                            detected_session_chests=self._detector.fsm.opened_count,
                        )
                        with self._diag_lock:
                            self._diagnostics = snap

                        if self._on_diagnostics and (tick_start - last_diag_emit) >= 1.0:
                            last_diag_emit = tick_start
                            try:
                                self._on_diagnostics(snap)
                            except Exception:
                                pass

                        if event is not None:
                            try:
                                self._on_chest_opened(event)
                            except Exception as cb_exc:
                                if self._on_error:
                                    self._on_error(f"Ошибка сохранения сундука CV: {cb_exc}")

                        sleep_for = interval - (time.monotonic() - tick_start)
                        if sleep_for > 0:
                            self._stop_event.wait(sleep_for)
            except Exception as exc:  # pragma: no cover - transient OS capture recovery
                with self._diag_lock:
                    self._diagnostics.last_event_note = f"Восстановление захвата экрана: {exc}"
                # Brief backoff and auto-recover `mss` handle (survives Alt-Tab / UAC / mode change)
                self._stop_event.wait(0.75)
