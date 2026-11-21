"""Chest detection engines: Fortnite .replay binary parser, analyzer, and OpenCV visual detector."""

from .replay_analyzer import ContainerTracker, ReplayAnalyzer
from .replay_parser import (
    ChunkType,
    ParsedReplay,
    ReplayChunk,
    ReplayEvent,
    ReplayHeader,
    ReplayMeta,
    ReplayParseError,
    ReplayParser,
    UnrealBitReader,
    compute_file_sha256,
    is_replay_live,
)
from .visual_detector import (
    ChestOpeningFSM,
    FrameFeatures,
    LiveVisualMonitor,
    VisualChestDetector,
    VisualDetectorConfig,
    VisualState,
)

__all__ = [
    "ChestOpeningFSM",
    "ChunkType",
    "ContainerTracker",
    "FrameFeatures",
    "LiveVisualMonitor",
    "ParsedReplay",
    "ReplayAnalyzer",
    "ReplayChunk",
    "ReplayEvent",
    "ReplayHeader",
    "ReplayMeta",
    "ReplayParseError",
    "ReplayParser",
    "UnrealBitReader",
    "VisualChestDetector",
    "VisualDetectorConfig",
    "VisualState",
    "compute_file_sha256",
    "is_replay_live",
]
