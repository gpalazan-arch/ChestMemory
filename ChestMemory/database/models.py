"""Database data models and domain enums for ChestMemory."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class DetectionSource(str, Enum):
    """How the opened chest count for a match was obtained."""

    REPLAY_PARSER = "replay_parser"
    EXTERNAL_REPLAY_BRIDGE = "external_replay_bridge"
    VISUAL_CV = "visual_cv"


class AnalysisStatus(str, Enum):
    """Outcome of analyzing a replay file or visual session."""

    SUCCESS = "success"
    LIVE_REPLAY = "live_replay"
    DECOMPRESSION_REQUIRED = "decompression_required"
    ENCRYPTED_UNSUPPORTED = "encrypted_unsupported"
    UNSUPPORTED_FORMAT = "unsupported_format"
    CORRUPTED = "corrupted"
    FILE_NOT_FOUND = "file_not_found"
    NO_DATA_EXTRACTED = "no_data_extracted"
    ERROR = "error"


class AttributionMode(str, Enum):
    """Indicates whether chest counts were filtered strictly to the local player."""

    LOCAL_PLAYER = "local_player"
    PROXIMITY_FALLBACK = "proximity_fallback"
    VISUAL_LOCAL_HUD = "visual_local_hud"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class ChestEventRecord:
    """Single verified chest-opening event detected in a match."""

    timestamp_ms: int
    container_guid: str
    container_class: str
    search_count: int = 1
    opened_by_local_player: bool = True
    confidence: float = 1.0


@dataclass(slots=True)
class AnalysisResult:
    """Structured result returned by ReplayAnalyzer or VisualChestDetector."""

    status: AnalysisStatus
    replay_id: str
    file_path: str
    file_hash: str
    chests_opened: Optional[int] = None
    source: DetectionSource = DetectionSource.REPLAY_PARSER
    attribution_mode: AttributionMode = AttributionMode.UNKNOWN
    match_timestamp: Optional[datetime] = None
    match_duration_ms: int = 0
    friendly_name: str = ""
    engine_version: str = ""
    eliminations: Optional[int] = None
    placement: Optional[int] = None
    error_message: str = ""
    diagnostic_details: str = ""
    events: list[ChestEventRecord] = field(default_factory=list)

    @property
    def is_reliable(self) -> bool:
        """True only when analysis succeeded with a verified non-negative integer."""
        return (
            self.status == AnalysisStatus.SUCCESS
            and self.chests_opened is not None
            and self.chests_opened >= 0
        )


@dataclass(slots=True)
class MatchRecord:
    """Persisted match record stored in SQLite."""

    id: Optional[int]
    replay_id: str
    file_hash: str
    file_name: str
    chests_opened: int
    played_at: datetime
    processed_at: datetime
    source: DetectionSource
    attribution_mode: AttributionMode = AttributionMode.LOCAL_PLAYER
    match_duration_ms: int = 0
    friendly_name: str = ""
    engine_version: str = ""
    eliminations: Optional[int] = None
    placement: Optional[int] = None

    def played_at_utc(self) -> datetime:
        if self.played_at.tzinfo is None:
            return self.played_at.replace(tzinfo=timezone.utc)
        return self.played_at.astimezone(timezone.utc)


@dataclass(slots=True)
class FailedAnalysisRecord:
    """Audit record for replays that could not be reliably analyzed.

    Stored separately from `matches` so failed parses NEVER pollute the chest
    counter with bogus zeros, while preventing infinite retry loops until the
    file changes or the user triggers a re-scan.
    """

    id: Optional[int]
    replay_id: str
    file_hash: str
    file_name: str
    status: AnalysisStatus
    error_message: str
    attempted_at: datetime


@dataclass(slots=True)
class StatsSnapshot:
    """Aggregated statistics displayed in the ChestMemory main window."""

    total_chests: int = 0
    total_matches: int = 0
    today_chests: int = 0
    week_chests: int = 0
    month_chests: int = 0
    last_match_chests: Optional[int] = None
    last_match_at: Optional[datetime] = None
    average_per_match: float = 0.0
    best_match_chests: int = 0
    failed_replays_count: int = 0


@dataclass(slots=True)
class AppConfig:
    """User-configurable settings persisted in SQLite `settings` table."""

    replay_dir: str = ""
    always_on_top: bool = True
    minimize_to_tray: bool = True
    auto_scan_existing: bool = True
    detection_mode: str = "hybrid"  # "replay", "visual", "hybrid"
    oodle_dll_path: str = ""
    external_parser_cmd: str = ""
    window_x: Optional[int] = None
    window_y: Optional[int] = None
    window_opacity: float = 0.96
    backup_retention_count: int = 15
    auto_backup_enabled: bool = True
    visual_fps: float = 10.0
    visual_monitor_index: int = 1
    file_stable_seconds: float = 4.0
