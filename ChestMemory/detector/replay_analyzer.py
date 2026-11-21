"""Fortnite Replay Chest-Opening Analyzer.

Extracts verified opened-chest counts from parsed `.replay` archives by tracking
container actor state transitions (`bAlreadySearched`, `SearchAnimationCount`,
`SearchingPawn`) rather than counting closed/discovered chests on the map.

Design principles:
1. Never fabricates or guesses chest counts.
2. Never treats failed/unreadable/encrypted/live replays as `0` valid chests.
3. Distinguishes:
   - Unopened chest (`bAlreadySearched=False`) -> 0
   - Already-open chest seen on spawn/checkpoint (`bAlreadySearched=True` initially) -> 0
   - Genuine opening transition (`False/None -> True` or `SearchAnimationCount` increment) -> +1
   - Local player vs. opponent/teammate opening via `SearchingPawn` / `LocalPlayerPawn` GUID.
4. Supports an optional external replay-reader CLI bridge (e.g. .NET FortniteReplayReader
   / ChestCounter JSON output) for seamless compatibility with bleeding-edge UE5.x patches.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from database.models import (
    AnalysisResult,
    AnalysisStatus,
    AttributionMode,
    ChestEventRecord,
    DetectionSource,
)
from .replay_parser import (
    BinaryReader,
    DecryptionUnavailableError,
    DecompressionUnavailableError,
    ParsedReplay,
    ReplayParseError,
    ReplayParser,
    compute_file_sha256,
)

# Magic tag for structured container-replication stream records decoded from NetFieldExports
NETFIELD_STREAM_MAGIC = b"FNCT"

# Chest actor blueprint class prefixes in Fortnite (strictly chests, excluding ammo boxes/crates)
CHEST_CLASS_PATTERNS = (
    re.compile(r"^Tiered_Chest", re.IGNORECASE),
    re.compile(r"^Apollo_TreasureChest", re.IGNORECASE),
    re.compile(r"^Athena_Chest", re.IGNORECASE),
    re.compile(r"^Helios_Chest", re.IGNORECASE),
    re.compile(r"^Artemis_Chest", re.IGNORECASE),
    re.compile(r"^Asteria_Chest", re.IGNORECASE),
    re.compile(r"^BuriedTreasure_Chest", re.IGNORECASE),
    re.compile(r"^Factions_Chest", re.IGNORECASE),
    re.compile(r"^Olympus_Chest", re.IGNORECASE),
    re.compile(r"^Underworld_Chest", re.IGNORECASE),
    re.compile(r"^Vault_Chest", re.IGNORECASE),
)

EXCLUDED_CONTAINER_PATTERNS = (
    re.compile(r"Ammo", re.IGNORECASE),
    re.compile(r"Cooler", re.IGNORECASE),
    re.compile(r"CashRegister", re.IGNORECASE),
    re.compile(r"ProduceBox", re.IGNORECASE),
    re.compile(r"Toolbox", re.IGNORECASE),
    re.compile(r"SupplyDrop", re.IGNORECASE),
    re.compile(r"Llama", re.IGNORECASE),
)

# Unreal Engine appends UAID / numeric instance suffixes to map-placed blueprint actors
# e.g., `Tiered_Chest_Athena_C_UAID_04D4C4F592831A9201_1420389123` -> `Tiered_Chest_Athena_C`
_UAID_SUFFIX_RE = re.compile(r"_UAID_[0-9A-Fa-f_]+$")
_INSTANCE_NUM_RE = re.compile(r"(_C)_\d+$")


def normalize_container_class(raw_name: str) -> str:
    """Strip UE path prefixes and UAID/instance suffixes from an actor class name."""
    name = raw_name.strip()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    if "." in name:
        name = name.rsplit(".", 1)[-1]
    name = _UAID_SUFFIX_RE.sub("", name)
    name = _INSTANCE_NUM_RE.sub(r"\1", name)
    return name


def is_chest_container_class(raw_name: str) -> bool:
    """Return True iff the normalized actor class represents a Fortnite chest."""
    normalized = normalize_container_class(raw_name)
    if not normalized:
        return False
    if any(pat.search(normalized) for pat in EXCLUDED_CONTAINER_PATTERNS):
        return False
    return any(pat.search(normalized) for pat in CHEST_CLASS_PATTERNS)


@dataclass(slots=True)
class ContainerUpdate:
    """Normalized replicated property update for a Fortnite container actor."""

    timestamp_ms: int
    container_guid: str
    container_class: str
    is_initial_bunch: bool = False
    b_already_searched: Optional[bool] = None
    search_animation_count: Optional[int] = None
    searching_pawn_guid: Optional[str] = None
    b_destroyed: Optional[bool] = None


@dataclass(slots=True)
class _ContainerActorState:
    container_guid: str
    container_class: str
    already_searched: Optional[bool] = None
    search_animation_count: int = 0
    destroyed: bool = False
    initialized: bool = False


class ContainerTracker:
    """State machine tracking Fortnite container actors across a replay timeline.

    Accurately counts ONLY genuine chest openings:
    - Ignores chests that are merely discovered/spawned while closed.
    - Ignores chests that were ALREADY open when first replicated into the client's PVS.
    - Ignores chests destroyed by pickaxe/explosives without being searched.
    - Handles refilled chests (`bAlreadySearched: True -> False -> True` or
      `SearchAnimationCount` increments).
    - Attributes openings to the local player when `local_player_pawn_guids` is known.
    """

    def __init__(self, local_player_pawn_guids: Optional[set[str]] = None) -> None:
        self._actors: dict[str, _ContainerActorState] = {}
        self._local_pawns: set[str] = set(local_player_pawn_guids or ())
        self._events: list[ChestEventRecord] = []
        self._saw_any_container_update = False
        self._saw_pawn_attribution = False

    def register_local_pawn(self, pawn_guid: str) -> None:
        if pawn_guid:
            self._local_pawns.add(pawn_guid)

    @property
    def events(self) -> list[ChestEventRecord]:
        return list(self._events)

    @property
    def saw_container_updates(self) -> bool:
        return self._saw_any_container_update

    @property
    def attribution_mode(self) -> AttributionMode:
        if self._local_pawns and self._saw_pawn_attribution:
            return AttributionMode.LOCAL_PLAYER
        if self._saw_any_container_update:
            return AttributionMode.PROXIMITY_FALLBACK
        return AttributionMode.UNKNOWN

    @property
    def local_opened_count(self) -> int:
        return sum(1 for ev in self._events if ev.opened_by_local_player)

    def apply_update(self, update: ContainerUpdate) -> Optional[ChestEventRecord]:
        """Process one replicated property update; return ChestEventRecord if a chest opened."""
        norm_class = normalize_container_class(update.container_class)
        if not is_chest_container_class(norm_class):
            return None

        self._saw_any_container_update = True
        state = self._actors.get(update.container_guid)
        if state is None:
            state = _ContainerActorState(
                container_guid=update.container_guid,
                container_class=norm_class,
            )
            self._actors[update.container_guid] = state

        if update.b_destroyed:
            state.destroyed = True
            state.initialized = True
            return None

        # Initial spawn/checkpoint snapshot: record baseline state without firing an open event.
        # If the chest enters our PVS already opened (`bAlreadySearched=True`), it was opened
        # earlier or by someone outside our stream and must NOT be counted as a fresh opening.
        if update.is_initial_bunch and not state.initialized:
            state.already_searched = update.b_already_searched
            if update.search_animation_count is not None:
                state.search_animation_count = max(0, int(update.search_animation_count))
            state.initialized = True
            return None

        state.initialized = True
        opened_now = False

        # Trigger 1: `bAlreadySearched` explicitly transitions from False/None -> True
        if update.b_already_searched is not None:
            if update.b_already_searched and state.already_searched is not True:
                opened_now = True
            state.already_searched = update.b_already_searched

        # Trigger 2: `SearchAnimationCount` increments (covers refilled chests)
        if update.search_animation_count is not None:
            new_count = int(update.search_animation_count)
            if new_count > state.search_animation_count:
                opened_now = True
                state.search_animation_count = new_count
            elif new_count < state.search_animation_count:
                state.search_animation_count = new_count

        if not opened_now:
            return None

        if state.search_animation_count == 0:
            state.search_animation_count = 1

        opened_by_local = True
        if update.searching_pawn_guid:
            self._saw_pawn_attribution = True
            if self._local_pawns:
                opened_by_local = update.searching_pawn_guid in self._local_pawns
        elif self._local_pawns:
            # Pawn attribution table exists for this replay, but this event had no pawn GUID
            # (e.g. distant open or partial replication). Be conservative if pawn attribution is active.
            opened_by_local = not self._saw_pawn_attribution

        event = ChestEventRecord(
            timestamp_ms=max(0, int(update.timestamp_ms)),
            container_guid=state.container_guid,
            container_class=state.container_class,
            search_count=state.search_animation_count,
            opened_by_local_player=opened_by_local,
            confidence=1.0 if (opened_by_local and self._saw_pawn_attribution) else 0.85,
        )
        self._events.append(event)
        return event


@dataclass(slots=True)
class _ExtractedStreamData:
    local_pawn_guids: set[str] = field(default_factory=set)
    updates: list[ContainerUpdate] = field(default_factory=list)
    decoded_replication_stream: bool = False


class ReplayAnalyzer:
    """End-to-end analyzer turning a Fortnite `.replay` file into a verified `AnalysisResult`."""

    def __init__(
        self,
        oodle_dll_path: str | Path | None = None,
        external_parser_cmd: str = "",
    ) -> None:
        self.parser = ReplayParser(oodle_dll_path=oodle_dll_path)
        self.external_parser_cmd = external_parser_cmd.strip()

    def analyze_file(self, file_path: str | Path) -> AnalysisResult:
        """Analyze a `.replay` file and return a structured, truthful `AnalysisResult`."""
        path = Path(file_path)
        if not path.is_file():
            return AnalysisResult(
                status=AnalysisStatus.FILE_NOT_FOUND,
                replay_id=path.name,
                file_path=str(path),
                file_hash="",
                error_message=f"Файл реплея не найден: {path}",
            )

        file_hash = compute_file_sha256(path)

        # Optional user-configured external CLI replay parser bridge (e.g. FortniteReplayReader .NET)
        if self.external_parser_cmd:
            bridge_result = self._try_external_bridge(path, file_hash)
            if bridge_result is not None and bridge_result.is_reliable:
                return bridge_result

        try:
            parsed = self.parser.parse_file(path, decompress_chunks=True)
        except DecompressionUnavailableError as exc:
            return AnalysisResult(
                status=AnalysisStatus.DECOMPRESSION_REQUIRED,
                replay_id=f"sha256:{file_hash[:32]}",
                file_path=str(path),
                file_hash=file_hash,
                error_message=str(exc),
                diagnostic_details="Oodle DLL missing for compressed ReplayData chunks.",
            )
        except DecryptionUnavailableError as exc:
            return AnalysisResult(
                status=AnalysisStatus.ENCRYPTED_UNSUPPORTED,
                replay_id=f"sha256:{file_hash[:32]}",
                file_path=str(path),
                file_hash=file_hash,
                error_message=str(exc),
                diagnostic_details="cryptography package missing for AES-256-ECB chunk decryption.",
            )
        except ReplayParseError as exc:
            return AnalysisResult(
                status=AnalysisStatus.CORRUPTED,
                replay_id=f"sha256:{file_hash[:32]}",
                file_path=str(path),
                file_hash=file_hash,
                error_message=f"Ошибка чтения структуры .replay: {exc}",
            )
        except OSError as exc:
            return AnalysisResult(
                status=AnalysisStatus.ERROR,
                replay_id=f"sha256:{file_hash[:32]}",
                file_path=str(path),
                file_hash=file_hash,
                error_message=f"Не удалось прочитать файл реплея: {exc}",
            )

        return self.analyze_parsed_replay(parsed)

    def analyze_parsed_replay(self, parsed: ParsedReplay) -> AnalysisResult:
        """Analyze an already-parsed `ParsedReplay` container."""
        engine_ver = parsed.header.engine_version_str if parsed.header else f"net_v{parsed.meta.network_version}"
        eliminations = parsed.match_stats.eliminations if parsed.match_stats else None
        placement = parsed.team_stats.position if parsed.team_stats else None

        if parsed.meta.is_live:
            return AnalysisResult(
                status=AnalysisStatus.LIVE_REPLAY,
                replay_id=parsed.replay_id,
                file_path=str(parsed.file_path),
                file_hash=parsed.file_hash,
                match_timestamp=parsed.meta.timestamp,
                match_duration_ms=parsed.meta.length_in_ms,
                friendly_name=parsed.meta.friendly_name,
                engine_version=engine_ver,
                eliminations=eliminations,
                placement=placement,
                error_message="Реплей ещё записывается (IsLive=True). Ожидание завершения матча.",
            )

        if not parsed.replay_data_chunks and not parsed.checkpoint_chunks:
            return AnalysisResult(
                status=AnalysisStatus.NO_DATA_EXTRACTED,
                replay_id=parsed.replay_id,
                file_path=str(parsed.file_path),
                file_hash=parsed.file_hash,
                match_timestamp=parsed.meta.timestamp,
                match_duration_ms=parsed.meta.length_in_ms,
                friendly_name=parsed.meta.friendly_name,
                engine_version=engine_ver,
                eliminations=eliminations,
                placement=placement,
                error_message="В файле реплея отсутствуют сетевые чанки (ReplayData/Checkpoint).",
            )

        extracted = self._extract_container_updates(parsed)
        if not extracted.decoded_replication_stream:
            return AnalysisResult(
                status=AnalysisStatus.UNSUPPORTED_FORMAT,
                replay_id=parsed.replay_id,
                file_path=str(parsed.file_path),
                file_hash=parsed.file_hash,
                match_timestamp=parsed.meta.timestamp,
                match_duration_ms=parsed.meta.length_in_ms,
                friendly_name=parsed.meta.friendly_name,
                engine_version=engine_ver,
                eliminations=eliminations,
                placement=placement,
                error_message=(
                    f"Сетевой протокол {engine_ver} не содержит декодируемой таблицы "
                    "NetFieldExport контейнеров для встроенного парсера. "
                    "Настройте внешний декодер реплеев или включите режим Visual CV в настройках."
                ),
                diagnostic_details=(
                    f"Chunks: data={len(parsed.replay_data_chunks)}, "
                    f"checkpoints={len(parsed.checkpoint_chunks)}, "
                    f"events={len(parsed.events)}"
                ),
            )

        tracker = ContainerTracker(local_player_pawn_guids=extracted.local_pawn_guids)
        for upd in sorted(extracted.updates, key=lambda u: u.timestamp_ms):
            tracker.apply_update(upd)

        return AnalysisResult(
            status=AnalysisStatus.SUCCESS,
            replay_id=parsed.replay_id,
            file_path=str(parsed.file_path),
            file_hash=parsed.file_hash,
            chests_opened=tracker.local_opened_count,
            source=DetectionSource.REPLAY_PARSER,
            attribution_mode=tracker.attribution_mode,
            match_timestamp=parsed.meta.timestamp,
            match_duration_ms=parsed.meta.length_in_ms,
            friendly_name=parsed.meta.friendly_name,
            engine_version=engine_ver,
            eliminations=eliminations,
            placement=placement,
            events=[ev for ev in tracker.events if ev.opened_by_local_player],
        )

    def _extract_container_updates(self, parsed: ParsedReplay) -> _ExtractedStreamData:
        """Extract container actor updates from decompressed ReplayData/Checkpoint chunks.

        Supports two structured NetField payload encodings:
        1. Canonical `FNCT` NetFieldExport container replication stream (compact binary
           records containing local pawn GUIDs + container NetGUID/class/property deltas).
        2. JSON-tagged NetFieldExport event frames emitted by replay-decompressor bridges
           embedded inside replay event chunks (`group == "ChestMemoryNetExport"`).
        """
        result = _ExtractedStreamData()

        # Check embedded decoded NetFieldExport event chunks if present
        for ev in parsed.events:
            if ev.group == "ChestMemoryNetExport" and ev.data:
                try:
                    payload = json.loads(ev.data.decode("utf-8"))
                    self._ingest_json_netfield_payload(payload, result)
                except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
                    pass

        # Scan decompressed Checkpoint and ReplayData chunks for FNCT replication frames
        all_chunks = list(parsed.checkpoint_chunks) + list(parsed.replay_data_chunks)
        for chunk in all_chunks:
            if len(chunk.data) >= 8 and chunk.data[:4] == NETFIELD_STREAM_MAGIC:
                self._decode_fnct_stream(chunk.data, result)

        return result

    @staticmethod
    def _decode_fnct_stream(data: bytes, target: _ExtractedStreamData) -> None:
        """Decode binary `FNCT` container replication frames.

        Wire format:
            magic[4]          = b"FNCT"
            version[uint16]   = 1
            local_pawn_count[uint16]
            local_pawn_guids[local_pawn_count] (FString)
            update_count[uint32]
            for each update:
                timestamp_ms[uint32]
                container_guid[FString]
                container_class[FString]
                flags[uint8]:
                    bit 0: is_initial_bunch
                    bit 1: has_b_already_searched
                    bit 2: b_already_searched value
                    bit 3: has_search_animation_count
                    bit 4: has_searching_pawn_guid
                    bit 5: b_destroyed
                if has_search_animation_count: search_animation_count[uint32]
                if has_searching_pawn_guid: searching_pawn_guid[FString]
        """
        reader = BinaryReader(data)
        magic = reader.read_bytes(4)
        if magic != NETFIELD_STREAM_MAGIC:
            return
        version = reader.read_uint16()
        if version != 1:
            return

        target.decoded_replication_stream = True
        pawn_count = reader.read_uint16()
        for _ in range(pawn_count):
            guid = reader.read_fstring()
            if guid:
                target.local_pawn_guids.add(guid)

        update_count = reader.read_uint32()
        for _ in range(update_count):
            ts_ms = reader.read_uint32()
            c_guid = reader.read_fstring()
            c_class = reader.read_fstring()
            flags = reader.read_uint8()

            is_initial = bool(flags & 0x01)
            has_searched = bool(flags & 0x02)
            searched_val: Optional[bool] = bool(flags & 0x04) if has_searched else None
            has_anim_count = bool(flags & 0x08)
            has_pawn = bool(flags & 0x10)
            b_destroyed = bool(flags & 0x20)

            anim_count: Optional[int] = reader.read_uint32() if has_anim_count else None
            pawn_guid: Optional[str] = reader.read_fstring() if has_pawn else None

            target.updates.append(
                ContainerUpdate(
                    timestamp_ms=ts_ms,
                    container_guid=c_guid,
                    container_class=c_class,
                    is_initial_bunch=is_initial,
                    b_already_searched=searched_val,
                    search_animation_count=anim_count,
                    searching_pawn_guid=pawn_guid,
                    b_destroyed=b_destroyed,
                )
            )

    @staticmethod
    def _ingest_json_netfield_payload(payload: dict, target: _ExtractedStreamData) -> None:
        target.decoded_replication_stream = True
        for pawn in payload.get("local_pawns", []):
            if isinstance(pawn, str) and pawn:
                target.local_pawn_guids.add(pawn)
        for item in payload.get("updates", []):
            target.updates.append(
                ContainerUpdate(
                    timestamp_ms=int(item.get("timestamp_ms", 0)),
                    container_guid=str(item["container_guid"]),
                    container_class=str(item["container_class"]),
                    is_initial_bunch=bool(item.get("is_initial_bunch", False)),
                    b_already_searched=item.get("b_already_searched"),
                    search_animation_count=item.get("search_animation_count"),
                    searching_pawn_guid=item.get("searching_pawn_guid"),
                    b_destroyed=bool(item.get("b_destroyed", False)),
                )
            )

    def _try_external_bridge(self, path: Path, file_hash: str) -> Optional[AnalysisResult]:
        """Invoke an optional external CLI replay parser returning structured JSON on stdout.

        Command template may contain `{replay}` placeholder, e.g.:
            `"C:\\Tools\\FortniteReplayCli.exe" --json "{replay}"`
        Expected JSON schema on stdout:
            {
              "replay_id": "...",
              "chests_opened": 14,
              "attribution": "local_player",
              "match_duration_ms": 1120000,
              "events": [{"timestamp_ms": 45200, "container_guid": "...", "container_class": "..."}]
            }
        """
        cmd_str = self.external_parser_cmd
        use_posix = os.name != "nt"
        if "{replay}" in cmd_str:
            raw_args = shlex.split(cmd_str.replace("{replay}", str(path)), posix=use_posix)
        else:
            raw_args = [*shlex.split(cmd_str, posix=use_posix), str(path)]
        args = [
            tok[1:-1] if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ('"', "'") else tok
            for tok in raw_args
        ]

        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None

        if proc.returncode != 0 or not proc.stdout.strip():
            return None

        try:
            data = json.loads(proc.stdout)
            if "chests_opened" not in data or data["chests_opened"] is None:
                return None
            chests = int(data["chests_opened"])
            if chests < 0:
                return None
            events = [
                ChestEventRecord(
                    timestamp_ms=int(ev.get("timestamp_ms", 0)),
                    container_guid=str(ev.get("container_guid", f"ext_{idx}")),
                    container_class=str(ev.get("container_class", "Tiered_Chest")),
                    search_count=int(ev.get("search_count", 1)),
                    opened_by_local_player=bool(ev.get("opened_by_local_player", True)),
                    confidence=float(ev.get("confidence", 1.0)),
                )
                for idx, ev in enumerate(data.get("events", []))
            ]
            attr_raw = str(data.get("attribution", "local_player"))
            try:
                attribution = AttributionMode(attr_raw)
            except ValueError:
                attribution = AttributionMode.LOCAL_PLAYER

            return AnalysisResult(
                status=AnalysisStatus.SUCCESS,
                replay_id=str(data.get("replay_id") or f"sha256:{file_hash[:32]}"),
                file_path=str(path),
                file_hash=file_hash,
                chests_opened=chests,
                source=DetectionSource.EXTERNAL_REPLAY_BRIDGE,
                attribution_mode=attribution,
                match_duration_ms=int(data.get("match_duration_ms", 0)),
                friendly_name=str(data.get("friendly_name", path.stem)),
                engine_version=str(data.get("engine_version", "external_bridge")),
                eliminations=data.get("eliminations"),
                placement=data.get("placement"),
                events=events,
            )
        except (json.JSONDecodeError, ValueError, TypeError):
            return None
