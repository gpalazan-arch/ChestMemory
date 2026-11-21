"""Real binary parser for Unreal Engine / Fortnite `.replay` files.

Implements the `FLocalFileReplayArchive` binary specification:
- File magic `0x1CA2E27F`
- ReplayVersionHistory (v1 through v7+ including encryption & custom versions)
- Chunk demuxer (`Header=0`, `ReplayData=1`, `Checkpoint=2`, `Event=3`)
- `FNetworkDemoHeader` (`0x2CF5A13D`) parser (Match GUID, Engine version, Branch)
- Event chunk decoder (`AthenaMatchStats`, `AthenaMatchTeamStats`, `playerElim`)
- Optional AES-256-ECB decryption (via `cryptography` if installed)
- Optional Oodle decompression via standalone `oo2core_*_win64.dll` (ctypes) or zlib
- Bit-level `UnrealBitReader` for Unreal Engine network bunches & NetFieldExports

Zero interaction with the Fortnite game process, memory, or anti-cheat.
Reads only local `.replay` files on disk.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import struct
import sys
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import IntEnum
from pathlib import Path
from typing import Optional

FILE_MAGIC = 0x1CA2E27F
NETWORK_DEMO_HEADER_MAGIC = 0x2CF5A13D
WINDOWS_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


class ReplayParseError(Exception):
    """Raised when a `.replay` binary file is invalid or truncated."""


class DecompressionUnavailableError(ReplayParseError):
    """Raised when Oodle decompression is required but no `oo2core` DLL is available."""


class DecryptionUnavailableError(ReplayParseError):
    """Raised when AES decryption is required but `cryptography` or key is unavailable."""


class ReplayVersionHistory(IntEnum):
    HISTORY_INITIAL = 0
    HISTORY_FIXEDSIZE_FRIENDLY_NAME = 1
    HISTORY_COMPRESSION = 2
    HISTORY_RECORDED_TIMESTAMP = 3
    HISTORY_STREAM_CHUNK_TIMES = 4
    HISTORY_FRIENDLY_NAME_ENCODING = 5
    HISTORY_ENCRYPTION = 6
    HISTORY_CUSTOM_VERSIONS = 7


class ChunkType(IntEnum):
    HEADER = 0
    REPLAY_DATA = 1
    CHECKPOINT = 2
    EVENT = 3
    UNKNOWN = 0xFFFFFFFF


@dataclass(slots=True)
class ReplayMeta:
    """Top-level `FLocalFileReplayArchive` metadata header."""

    file_version: int
    length_in_ms: int
    network_version: int
    changelist: int
    friendly_name: str
    is_live: bool
    timestamp: Optional[datetime] = None
    is_compressed: bool = False
    is_encrypted: bool = False
    encryption_key: bytes = b""
    custom_versions: list[tuple[str, int]] = field(default_factory=list)


@dataclass(slots=True)
class ReplayHeader:
    """Decoded `FNetworkDemoHeader` from the Header chunk (`ChunkType.HEADER`)."""

    network_version: int = 0
    network_checksum: int = 0
    engine_network_protocol_version: int = 0
    game_network_protocol_version: int = 0
    guid: str = ""
    major: int = 0
    minor: int = 0
    patch: int = 0
    changelist: int = 0
    branch: str = ""
    level_names_and_times: list[tuple[str, int]] = field(default_factory=list)
    flags: int = 0
    game_specific_data: list[str] = field(default_factory=list)
    custom_versions: list[tuple[str, int]] = field(default_factory=list)

    @property
    def engine_version_str(self) -> str:
        if self.major or self.minor or self.patch:
            base = f"{self.major}.{self.minor}.{self.patch}"
            return f"{base} ({self.branch})" if self.branch else base
        return self.branch or f"net_v{self.network_version}"


@dataclass(slots=True)
class ReplayEvent:
    """Decoded `ChunkType.EVENT` entry."""

    event_id: str
    group: str
    metadata: str
    time1_ms: int
    time2_ms: int
    data: bytes


@dataclass(slots=True)
class MatchStatsEvent:
    """Decoded `AthenaMatchStats` event payload."""

    accuracy: float = 0.0
    assists: int = 0
    eliminations: int = 0
    weapon_damage: int = 0
    other_damage: int = 0
    revives: int = 0
    damage_taken: int = 0
    damage_to_structures: int = 0
    materials_gathered: int = 0
    materials_used: int = 0
    total_traveled: int = 0


@dataclass(slots=True)
class TeamStatsEvent:
    """Decoded `AthenaMatchTeamStats` event payload."""

    position: int = 0
    total_players: int = 0


@dataclass(slots=True)
class ReplayChunk:
    """Raw or decompressed chunk from a `.replay` archive."""

    chunk_type: ChunkType
    size_in_bytes: int
    offset: int
    data: bytes
    time1_ms: int = 0
    time2_ms: int = 0
    memory_size_in_bytes: int = 0


@dataclass(slots=True)
class ParsedReplay:
    """Complete parsed representation of a Fortnite `.replay` file."""

    file_path: Path
    file_hash: str
    meta: ReplayMeta
    header: Optional[ReplayHeader] = None
    events: list[ReplayEvent] = field(default_factory=list)
    replay_data_chunks: list[ReplayChunk] = field(default_factory=list)
    checkpoint_chunks: list[ReplayChunk] = field(default_factory=list)
    match_stats: Optional[MatchStatsEvent] = None
    team_stats: Optional[TeamStatsEvent] = None

    @property
    def replay_id(self) -> str:
        if self.header and self.header.guid:
            return self.header.guid
        return f"sha256:{self.file_hash[:32]}"


class BinaryReader:
    """Little-endian binary reader matching Unreal Engine `FArchive` serialization."""

    def __init__(self, data: bytes | memoryview) -> None:
        self._buf = memoryview(data)
        self._pos = 0
        self._size = len(self._buf)

    @property
    def pos(self) -> int:
        return self._pos

    @property
    def remaining(self) -> int:
        return self._size - self._pos

    def seek(self, offset: int) -> None:
        if offset < 0 or offset > self._size:
            raise ReplayParseError(f"Seek out of bounds: {offset} (size={self._size})")
        self._pos = offset

    def skip(self, count: int) -> None:
        self.seek(self._pos + count)

    def read_bytes(self, count: int) -> bytes:
        if count < 0 or self._pos + count > self._size:
            raise ReplayParseError(
                f"Unexpected EOF reading {count} bytes at offset {self._pos} (size={self._size})"
            )
        out = bytes(self._buf[self._pos : self._pos + count])
        self._pos += count
        return out

    def read_uint8(self) -> int:
        return self.read_bytes(1)[0]

    def read_uint16(self) -> int:
        return struct.unpack("<H", self.read_bytes(2))[0]

    def read_int32(self) -> int:
        return struct.unpack("<i", self.read_bytes(4))[0]

    def read_uint32(self) -> int:
        return struct.unpack("<I", self.read_bytes(4))[0]

    def read_int64(self) -> int:
        return struct.unpack("<q", self.read_bytes(8))[0]

    def read_uint64(self) -> int:
        return struct.unpack("<Q", self.read_bytes(8))[0]

    def read_float(self) -> float:
        return struct.unpack("<f", self.read_bytes(4))[0]

    def read_bool32(self) -> bool:
        return self.read_uint32() != 0

    def read_guid(self) -> str:
        raw = self.read_bytes(16)
        # Unreal FGuid stores 4 little-endian uint32 words (A, B, C, D)
        a, b, c, d = struct.unpack("<4I", raw)
        if a == 0 and b == 0 and c == 0 and d == 0:
            return ""
        big_endian = struct.pack(">4I", a, b, c, d)
        return str(uuid.UUID(bytes=big_endian))

    def read_fstring(self) -> str:
        """Read an Unreal Engine `FString` (int32 length; negative => UTF-16LE)."""
        length = self.read_int32()
        if length == 0:
            return ""
        if abs(length) > 1_048_576:
            raise ReplayParseError(f"Implausible FString length {length} at offset {self._pos}")
        if length < 0:
            byte_len = -length * 2
            raw = self.read_bytes(byte_len)
            return raw.decode("utf-16-le", errors="replace").rstrip("\x00")
        raw = self.read_bytes(length)
        return raw.decode("utf-8", errors="replace").rstrip("\x00")

    def read_array_bytes(self) -> bytes:
        length = self.read_int32()
        if length < 0 or length > self.remaining:
            raise ReplayParseError(f"Invalid byte array length {length}")
        return self.read_bytes(length)


class UnrealBitReader:
    """LSB-first bit reader matching Unreal Engine `FBitReader`."""

    def __init__(self, data: bytes, bit_count: Optional[int] = None) -> None:
        self._data = data
        self._bit_pos = 0
        self._max_bits = len(data) * 8 if bit_count is None else min(bit_count, len(data) * 8)

    @property
    def bit_pos(self) -> int:
        return self._bit_pos

    @property
    def bits_left(self) -> int:
        return self._max_bits - self._bit_pos

    def read_bit(self) -> int:
        if self._bit_pos >= self._max_bits:
            raise ReplayParseError("UnrealBitReader reached end of bit stream")
        byte_idx = self._bit_pos >> 3
        bit_idx = self._bit_pos & 7
        val = (self._data[byte_idx] >> bit_idx) & 1
        self._bit_pos += 1
        return val

    def read_bool(self) -> bool:
        return self.read_bit() == 1

    def read_bits(self, count: int) -> int:
        if count < 0 or count > self.bits_left:
            raise ReplayParseError(f"Cannot read {count} bits ({self.bits_left} left)")
        value = 0
        for i in range(count):
            if self.read_bit():
                value |= 1 << i
        return value

    def read_uint8(self) -> int:
        return self.read_bits(8)

    def read_uint16(self) -> int:
        return self.read_bits(16)

    def read_uint32(self) -> int:
        return self.read_bits(32)

    def read_int32(self) -> int:
        raw = self.read_bits(32)
        return raw - 0x100000000 if raw >= 0x80000000 else raw

    def read_int_packed(self) -> int:
        """Read Unreal Engine variable-length packed unsigned integer (7 bits/byte)."""
        value = 0
        shift = 0
        for _ in range(5):
            if self.bits_left == 0:
                break
            byte_val = self.read_bits(8) if self.bits_left >= 8 else self.read_bits(self.bits_left)
            has_more = (byte_val & 1) != 0
            value |= (byte_val >> 1) << shift
            shift += 7
            if not has_more:
                break
        return value

    def read_serialized_int(self, max_value: int) -> int:
        """Read Unreal Engine `SerializeInt(Value, Max)` bit-packed integer."""
        if max_value <= 1:
            return 0
        value = 0
        mask = 1
        while (value + mask) < max_value and mask != 0:
            if self.read_bit():
                value |= mask
            mask <<= 1
        return value

    def read_bytes(self, count: int) -> bytes:
        if count < 0 or count * 8 > self.bits_left:
            raise ReplayParseError(f"Cannot read {count} bytes from bit stream")
        if (self._bit_pos & 7) == 0:
            start = self._bit_pos >> 3
            self._bit_pos += count * 8
            return self._data[start : start + count]
        return bytes(self.read_bits(8) for _ in range(count))

    def read_fstring(self) -> str:
        length = self.read_int32()
        if length == 0:
            return ""
        if abs(length) > 65536:
            raise ReplayParseError(f"Implausible bitstream FString length {length}")
        if length < 0:
            raw = self.read_bytes(-length * 2)
            return raw.decode("utf-16-le", errors="replace").rstrip("\x00")
        raw = self.read_bytes(length)
        return raw.decode("utf-8", errors="replace").rstrip("\x00")

    def align_byte(self) -> None:
        rem = self._bit_pos & 7
        if rem:
            self._bit_pos = min(self._max_bits, self._bit_pos + (8 - rem))


class OodleDecompressor:
    """Loads standalone `oo2core_*_win64.dll` via ctypes for Fortnite Oodle chunk decompression.

    Does NOT inject into Fortnite or access game memory — purely invokes the
    stateless `OodleLZ_Decompress` C export on offline replay bytes read from disk.
    """

    DEFAULT_SEARCH_DIRS = (
        Path(r"C:\Program Files\Epic Games\Fortnite\FortniteGame\Binaries\Win64"),
        Path(r"D:\Program Files\Epic Games\Fortnite\FortniteGame\Binaries\Win64"),
        Path(r"D:\Epic Games\Fortnite\FortniteGame\Binaries\Win64"),
        Path(r"E:\Epic Games\Fortnite\FortniteGame\Binaries\Win64"),
        Path(r"F:\Epic Games\Fortnite\FortniteGame\Binaries\Win64"),
        Path(r"D:\Games\Fortnite\FortniteGame\Binaries\Win64"),
        Path(r"E:\Games\Fortnite\FortniteGame\Binaries\Win64"),
    )

    def __init__(self, explicit_dll_path: str | Path | None = None) -> None:
        self._dll_path: Optional[Path] = self._locate_dll(explicit_dll_path)
        self._lib = None

    @property
    def dll_path(self) -> Optional[Path]:
        return self._dll_path

    @property
    def is_available(self) -> bool:
        return self._dll_path is not None and self._dll_path.exists()

    @staticmethod
    def _find_in_dir(directory: Path) -> Optional[Path]:
        if not directory.is_dir():
            return None
        try:
            matches = sorted(directory.glob("oo2core_*_win64.dll"), reverse=True)
            if matches:
                return matches[0]
        except OSError:
            return None
        return None

    @staticmethod
    def _discover_epic_install_dirs() -> list[Path]:
        """Read Epic Games Launcher install records to locate Fortnite/UE game folders on any drive."""
        found: list[Path] = []
        program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
        installed_dat = program_data / "Epic" / "UnrealEngineLauncher" / "LauncherInstalled.dat"
        if installed_dat.is_file():
            try:
                data = json.loads(installed_dat.read_text(encoding="utf-8", errors="ignore"))
                for item in data.get("InstallationList", []):
                    loc = item.get("InstallLocation")
                    if isinstance(loc, str) and loc:
                        base = Path(loc)
                        found.extend(
                            [
                                base / "FortniteGame" / "Binaries" / "Win64",
                                base / "Engine" / "Binaries" / "Win64",
                                base / "Binaries" / "Win64",
                                base,
                            ]
                        )
            except (OSError, json.JSONDecodeError, TypeError):
                pass
        return found

    def _locate_dll(self, explicit_dll_path: str | Path | None) -> Optional[Path]:
        if explicit_dll_path:
            p = Path(explicit_dll_path)
            if p.is_file():
                return p
            hit = self._find_in_dir(p)
            if hit:
                return hit

        candidate_dirs: list[Path] = []

        # 1. Next to ChestMemory.exe (critical for PyInstaller --onefile builds!)
        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).resolve().parent
            candidate_dirs.extend([exe_dir, exe_dir / "data" / "bin", exe_dir / "bin"])

        # 2. AppData / LocalAppData ChestMemory bin folders
        for env_key in ("APPDATA", "LOCALAPPDATA"):
            base_env = os.environ.get(env_key)
            if base_env:
                candidate_dirs.append(Path(base_env) / "ChestMemory" / "bin")
                candidate_dirs.append(Path(base_env) / "ChestMemory")

        # 3. Source tree ./data/bin
        candidate_dirs.append(Path(__file__).resolve().parent.parent / "data" / "bin")

        # 4. Epic Games Launcher installed apps + standard Fortnite directories
        candidate_dirs.extend(self._discover_epic_install_dirs())
        candidate_dirs.extend(self.DEFAULT_SEARCH_DIRS)

        seen: set[Path] = set()
        for candidate_dir in candidate_dirs:
            if candidate_dir in seen:
                continue
            seen.add(candidate_dir)
            hit = self._find_in_dir(candidate_dir)
            if hit:
                return hit
        return None

    def _ensure_loaded(self) -> None:
        if self._lib is not None:
            return
        if not self.is_available or self._dll_path is None:
            raise DecompressionUnavailableError(
                "Реплей сжат Oodle, но файл oo2core_*_win64.dll не найден "
                "(современный UE5 Fortnite встраивает Oodle в .exe и не кладёт DLL отдельно). "
                "Используйте живой режим Visual CV / Гибридный или укажите путь к oo2core_*_win64.dll в настройках."
            )
        try:
            lib = ctypes.CDLL(str(self._dll_path))
            fn = lib.OodleLZ_Decompress
            fn.argtypes = [
                ctypes.c_void_p,  # compBuf
                ctypes.c_ssize_t,  # compBufSize
                ctypes.c_void_p,  # rawBuf
                ctypes.c_ssize_t,  # rawLen
                ctypes.c_int,  # fuzzSafe
                ctypes.c_int,  # checkCRC
                ctypes.c_int,  # verbosity
                ctypes.c_void_p,  # decBufBase
                ctypes.c_ssize_t,  # decBufSize
                ctypes.c_void_p,  # fpCallback
                ctypes.c_void_p,  # callbackUserData
                ctypes.c_void_p,  # decoderMemory
                ctypes.c_ssize_t,  # decoderMemorySize
                ctypes.c_int,  # threadPhase
            ]
            fn.restype = ctypes.c_ssize_t
            self._lib = lib
        except OSError as exc:
            raise DecompressionUnavailableError(
                f"Failed to load Oodle DLL ({self._dll_path}): {exc}"
            ) from exc

    def decompress(self, compressed: bytes, uncompressed_size: int) -> bytes:
        """Decompress an OodleLZ payload (or standard zlib if header matches 0x78)."""
        if uncompressed_size <= 0:
            return b""
        # Try standard zlib first if payload begins with a valid zlib header
        if len(compressed) >= 2 and compressed[0] == 0x78 and compressed[1] in (0x01, 0x5E, 0x9C, 0xDA):
            try:
                return zlib.decompress(compressed)
            except zlib.error:
                pass

        self._ensure_loaded()
        assert self._lib is not None
        out_buf = ctypes.create_string_buffer(uncompressed_size)
        result = self._lib.OodleLZ_Decompress(
            ctypes.c_char_p(compressed),
            len(compressed),
            out_buf,
            uncompressed_size,
            1,  # fuzzSafe = OodleLZ_FuzzSafe_Yes
            0,  # checkCRC = No
            0,  # verbosity = None
            None,
            0,
            None,
            None,
            None,
            0,
            0,  # threadPhase = Unthreaded
        )
        if result <= 0:
            raise ReplayParseError(
                f"OodleLZ_Decompress failed (returned {result}, expected {uncompressed_size} bytes)"
            )
        return out_buf.raw[:result]


def decrypt_aes_ecb(data: bytes, key: bytes) -> bytes:
    """Decrypt AES-256-ECB encrypted replay chunks if `cryptography` is installed."""
    if not data or not key:
        return data
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise DecryptionUnavailableError(
            "Replay chunks are AES-encrypted, but the 'cryptography' package is not installed. "
            "Install requirements.txt (`pip install cryptography`)."
        ) from exc

    if len(key) not in (16, 24, 32):
        raise ReplayParseError(f"Unsupported AES encryption key length: {len(key)} bytes")
    if len(data) % 16 != 0:
        # Pad or trim trailing non-block bytes defensively only if zero-padded
        raise ReplayParseError(
            f"AES-ECB encrypted chunk size ({len(data)}) is not a multiple of 16 bytes"
        )

    cipher = Cipher(algorithms.AES(key), modes.ECB())
    decryptor = cipher.decryptor()
    return decryptor.update(data) + decryptor.finalize()


def compute_file_sha256(file_path: str | Path) -> str:
    """Compute SHA-256 hex digest of a file using streaming 64 KiB reads."""
    digest = hashlib.sha256()
    with Path(file_path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def filetime_to_datetime(ticks: int) -> Optional[datetime]:
    """Convert a 64-bit Windows FILETIME / .NET tick timestamp to UTC datetime."""
    if ticks <= 0:
        return None
    try:
        # Fortnite `.replay` stores .NET DateTime ticks (100ns since 0001-01-01)
        # OR Windows FILETIME (100ns since 1601-01-01). Detect range automatically.
        unix_1970_dotnet = 621355968000000000
        unix_1970_filetime = 116444736000000000
        if ticks >= unix_1970_dotnet:
            unix_seconds = (ticks - unix_1970_dotnet) / 10_000_000.0
            return datetime.fromtimestamp(unix_seconds, tz=timezone.utc)
        if ticks >= unix_1970_filetime:
            return WINDOWS_FILETIME_EPOCH + timedelta(microseconds=ticks / 10.0)
    except (OverflowError, OSError, ValueError):
        return None
    return None


def is_replay_live(file_path: str | Path) -> Optional[bool]:
    """Quickly inspect the `.replay` file header to check whether `IsLive` is still True.

    Returns:
        True  - Fortnite is still recording this replay (do not analyze yet)
        False - Replay has been finalized on disk
        None  - File is unreadable or not yet a valid `.replay` header
    """
    path = Path(file_path)
    if not path.is_file():
        return None
    try:
        with path.open("rb") as handle:
            head = handle.read(8192)
        if len(head) < 24:
            return None
        reader = BinaryReader(head)
        magic = reader.read_uint32()
        if magic != FILE_MAGIC:
            return None
        file_version = reader.read_uint32()
        if file_version >= ReplayVersionHistory.HISTORY_CUSTOM_VERSIONS:
            cv_count = reader.read_int32()
            if cv_count < 0 or cv_count > 4096:
                return None
            reader.skip(cv_count * 20)  # 16-byte FGuid + 4-byte int32 version
        _length_ms = reader.read_uint32()
        _net_ver = reader.read_uint32()
        _changelist = reader.read_uint32()
        if file_version >= ReplayVersionHistory.HISTORY_FRIENDLY_NAME_ENCODING:
            _friendly = reader.read_fstring()
        elif file_version >= ReplayVersionHistory.HISTORY_FIXEDSIZE_FRIENDLY_NAME:
            reader.skip(256 * 2)
        else:
            _friendly = reader.read_fstring()
        return reader.read_bool32()
    except (OSError, ReplayParseError):
        return None


class ReplayParser:
    """Parses Fortnite `.replay` files into structured metadata, headers, events, and chunks."""

    def __init__(self, oodle_dll_path: str | Path | None = None) -> None:
        self.oodle = OodleDecompressor(oodle_dll_path)

    def parse_file(self, file_path: str | Path, decompress_chunks: bool = True) -> ParsedReplay:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Replay file not found: {path}")

        raw = path.read_bytes()
        file_hash = hashlib.sha256(raw).hexdigest()
        return self.parse_bytes(
            raw,
            file_path=path,
            file_hash=file_hash,
            decompress_chunks=decompress_chunks,
        )

    def parse_bytes(
        self,
        raw: bytes,
        file_path: Path = Path("<memory>.replay"),
        file_hash: Optional[str] = None,
        decompress_chunks: bool = True,
    ) -> ParsedReplay:
        if len(raw) < 24:
            raise ReplayParseError("Replay file is too small to contain a valid UE header")

        digest = file_hash or hashlib.sha256(raw).hexdigest()
        reader = BinaryReader(raw)
        meta = self._parse_meta(reader)

        parsed = ParsedReplay(
            file_path=file_path,
            file_hash=digest,
            meta=meta,
        )

        # If replay is still live, return metadata immediately without forcing chunk decode
        if meta.is_live:
            return parsed

        while reader.remaining >= 8:
            chunk_offset = reader.pos
            raw_chunk_type = reader.read_uint32()
            chunk_size = reader.read_int32()
            if chunk_size < 0 or chunk_size > reader.remaining:
                raise ReplayParseError(
                    f"Corrupted chunk size {chunk_size} at offset {chunk_offset} "
                    f"(remaining={reader.remaining})"
                )

            chunk_payload = reader.read_bytes(chunk_size)
            try:
                chunk_type = ChunkType(raw_chunk_type)
            except ValueError:
                chunk_type = ChunkType.UNKNOWN

            if chunk_type == ChunkType.HEADER:
                parsed.header = self._parse_header_chunk(chunk_payload)
            elif chunk_type == ChunkType.EVENT:
                event = self._parse_event_chunk(chunk_payload, meta)
                parsed.events.append(event)
                self._maybe_decode_known_event(event, parsed)
            elif chunk_type == ChunkType.REPLAY_DATA:
                chunk = self._parse_data_chunk(
                    chunk_payload,
                    chunk_type=ChunkType.REPLAY_DATA,
                    offset=chunk_offset,
                    meta=meta,
                    decompress=decompress_chunks,
                )
                parsed.replay_data_chunks.append(chunk)
            elif chunk_type == ChunkType.CHECKPOINT:
                chunk = self._parse_data_chunk(
                    chunk_payload,
                    chunk_type=ChunkType.CHECKPOINT,
                    offset=chunk_offset,
                    meta=meta,
                    decompress=decompress_chunks,
                )
                parsed.checkpoint_chunks.append(chunk)

        return parsed

    def _parse_meta(self, reader: BinaryReader) -> ReplayMeta:
        magic = reader.read_uint32()
        if magic != FILE_MAGIC:
            raise ReplayParseError(
                f"Invalid replay magic 0x{magic:08X} (expected 0x{FILE_MAGIC:08X})"
            )

        file_version = reader.read_uint32()

        custom_versions: list[tuple[str, int]] = []
        if file_version >= ReplayVersionHistory.HISTORY_CUSTOM_VERSIONS:
            cv_count = reader.read_int32()
            if cv_count < 0 or cv_count > 4096 or (cv_count * 20) > reader.remaining:
                raise ReplayParseError(
                    f"Invalid CustomVersions count {cv_count} for FileVersion={file_version}"
                )
            for _ in range(cv_count):
                cv_guid = reader.read_guid()
                cv_ver = reader.read_int32()
                custom_versions.append((cv_guid, cv_ver))

        length_in_ms = reader.read_uint32()
        network_version = reader.read_uint32()
        changelist = reader.read_uint32()

        if file_version >= ReplayVersionHistory.HISTORY_FRIENDLY_NAME_ENCODING:
            friendly_name = reader.read_fstring()
        elif file_version >= ReplayVersionHistory.HISTORY_FIXEDSIZE_FRIENDLY_NAME:
            raw_name = reader.read_bytes(256 * 2)
            friendly_name = raw_name.decode("utf-16-le", errors="replace").rstrip("\x00")
        else:
            friendly_name = reader.read_fstring()

        is_live = reader.read_bool32()

        timestamp: Optional[datetime] = None
        if file_version >= ReplayVersionHistory.HISTORY_RECORDED_TIMESTAMP:
            ticks = reader.read_int64()
            timestamp = filetime_to_datetime(ticks)

        is_compressed = False
        if file_version >= ReplayVersionHistory.HISTORY_COMPRESSION:
            is_compressed = reader.read_bool32()

        is_encrypted = False
        encryption_key = b""
        if file_version >= ReplayVersionHistory.HISTORY_ENCRYPTION:
            is_encrypted = reader.read_bool32()
            encryption_key = reader.read_array_bytes()

        if not is_live and is_encrypted and len(encryption_key) == 0:
            raise ReplayParseError("Replay marked encrypted but encryption key is empty")

        return ReplayMeta(
            file_version=file_version,
            length_in_ms=length_in_ms,
            network_version=network_version,
            changelist=changelist,
            friendly_name=friendly_name,
            is_live=is_live,
            timestamp=timestamp,
            is_compressed=is_compressed,
            is_encrypted=is_encrypted,
            encryption_key=encryption_key,
            custom_versions=custom_versions,
        )

    def _parse_header_chunk(self, payload: bytes) -> ReplayHeader:
        reader = BinaryReader(payload)
        magic = reader.read_uint32()
        if magic != NETWORK_DEMO_HEADER_MAGIC:
            raise ReplayParseError(
                f"Invalid FNetworkDemoHeader magic 0x{magic:08X} "
                f"(expected 0x{NETWORK_DEMO_HEADER_MAGIC:08X})"
            )

        network_version = reader.read_uint32()

        # UE 5.1+ (ENetworkVersionHistory >= 18 / HISTORY_CUSTOM_VERSIONS) stores
        # CustomVersions right after NetworkVersion in FNetworkDemoHeader.
        header_custom_versions: list[tuple[str, int]] = []
        if network_version >= 18 and reader.remaining >= 4:
            saved_pos = reader.pos
            cv_count = reader.read_int32()
            if 0 <= cv_count <= 2048 and (cv_count * 20) <= (reader.remaining - 12):
                for _ in range(cv_count):
                    header_custom_versions.append((reader.read_guid(), reader.read_int32()))
            else:
                # Fallback for legacy/custom headers without CustomVersions block
                reader.seek(saved_pos)

        network_checksum = reader.read_uint32()
        engine_proto = reader.read_uint32()
        game_proto = reader.read_uint32()

        guid = ""
        if network_version >= 12 and reader.remaining >= 16:
            guid = reader.read_guid()

        major = minor = patch = changelist = 0
        branch = ""
        if reader.remaining >= 14:
            major = reader.read_uint16()
            minor = reader.read_uint16()
            patch = reader.read_uint16()
            changelist = reader.read_uint32()
            branch = reader.read_fstring()

        level_names_and_times: list[tuple[str, int]] = []
        if reader.remaining >= 4:
            level_count = reader.read_int32()
            if 0 <= level_count <= 256:
                for _ in range(level_count):
                    if reader.remaining < 8:
                        break
                    lvl_name = reader.read_fstring()
                    lvl_time = reader.read_uint32()
                    level_names_and_times.append((lvl_name, lvl_time))

        flags = 0
        if reader.remaining >= 4:
            flags = reader.read_uint32()

        game_specific_data: list[str] = []
        if reader.remaining >= 4:
            gsd_count = reader.read_int32()
            if 0 <= gsd_count <= 128:
                for _ in range(gsd_count):
                    if reader.remaining < 4:
                        break
                    game_specific_data.append(reader.read_fstring())

        return ReplayHeader(
            network_version=network_version,
            network_checksum=network_checksum,
            engine_network_protocol_version=engine_proto,
            game_network_protocol_version=game_proto,
            guid=guid,
            major=major,
            minor=minor,
            patch=patch,
            changelist=changelist,
            branch=branch,
            level_names_and_times=level_names_and_times,
            flags=flags,
            game_specific_data=game_specific_data,
            custom_versions=header_custom_versions,
        )

    def _parse_event_chunk(self, payload: bytes, meta: ReplayMeta) -> ReplayEvent:
        reader = BinaryReader(payload)
        event_id = reader.read_fstring()
        group = reader.read_fstring()
        metadata = reader.read_fstring()
        time1 = reader.read_uint32()
        time2 = reader.read_uint32()
        size = reader.read_int32()
        if size < 0 or size > reader.remaining:
            raise ReplayParseError(f"Invalid event data size {size} for event '{event_id}'")
        event_bytes = reader.read_bytes(size)
        if meta.is_encrypted and meta.encryption_key and event_bytes:
            event_bytes = decrypt_aes_ecb(event_bytes, meta.encryption_key)
        return ReplayEvent(
            event_id=event_id,
            group=group,
            metadata=metadata,
            time1_ms=time1,
            time2_ms=time2,
            data=event_bytes,
        )

    def _maybe_decode_known_event(self, event: ReplayEvent, parsed: ParsedReplay) -> None:
        if event.group == "playerElim":
            return
        if event.metadata == "AthenaMatchStats" and len(event.data) >= 48:
            try:
                r = BinaryReader(event.data)
                r.skip(4)  # unknown int32
                parsed.match_stats = MatchStatsEvent(
                    accuracy=r.read_float(),
                    assists=r.read_uint32(),
                    eliminations=r.read_uint32(),
                    weapon_damage=r.read_uint32(),
                    other_damage=r.read_uint32(),
                    revives=r.read_uint32(),
                    damage_taken=r.read_uint32(),
                    damage_to_structures=r.read_uint32(),
                    materials_gathered=r.read_uint32(),
                    materials_used=r.read_uint32(),
                    total_traveled=r.read_uint32(),
                )
            except ReplayParseError:
                pass
        elif event.metadata == "AthenaMatchTeamStats" and len(event.data) >= 12:
            try:
                r = BinaryReader(event.data)
                r.skip(4)  # unknown int32
                parsed.team_stats = TeamStatsEvent(
                    position=r.read_uint32(),
                    total_players=r.read_uint32(),
                )
            except ReplayParseError:
                pass

    def _parse_data_chunk(
        self,
        payload: bytes,
        chunk_type: ChunkType,
        offset: int,
        meta: ReplayMeta,
        decompress: bool,
    ) -> ReplayChunk:
        reader = BinaryReader(payload)
        time1 = time2 = 0
        mem_size = 0

        if chunk_type == ChunkType.REPLAY_DATA:
            if meta.file_version >= ReplayVersionHistory.HISTORY_STREAM_CHUNK_TIMES:
                time1 = reader.read_uint32()
                time2 = reader.read_uint32()
                size_in_bytes = reader.read_int32()
            else:
                size_in_bytes = reader.remaining
            if meta.file_version >= ReplayVersionHistory.HISTORY_ENCRYPTION:
                mem_size = reader.read_int32()
        else:
            # Checkpoint chunk begins with id/group/metadata FStrings + time1/time2 + size
            _cp_id = reader.read_fstring()
            _cp_group = reader.read_fstring()
            _cp_meta = reader.read_fstring()
            time1 = reader.read_uint32()
            time2 = reader.read_uint32()
            size_in_bytes = reader.read_int32()

        if size_in_bytes < 0 or size_in_bytes > reader.remaining:
            raise ReplayParseError(
                f"Invalid {chunk_type.name} payload size {size_in_bytes} (remaining={reader.remaining})"
            )

        chunk_bytes = reader.read_bytes(size_in_bytes)

        if decompress and chunk_bytes:
            if meta.is_encrypted and meta.encryption_key:
                chunk_bytes = decrypt_aes_ecb(chunk_bytes, meta.encryption_key)
            if meta.is_compressed:
                inner = BinaryReader(chunk_bytes)
                if inner.remaining >= 8:
                    decompressed_size = inner.read_int32()
                    compressed_size = inner.read_int32()
                    if 0 < compressed_size <= inner.remaining and decompressed_size > 0:
                        comp_payload = inner.read_bytes(compressed_size)
                        chunk_bytes = self.oodle.decompress(comp_payload, decompressed_size)

        return ReplayChunk(
            chunk_type=chunk_type,
            size_in_bytes=len(chunk_bytes),
            offset=offset,
            data=chunk_bytes,
            time1_ms=time1,
            time2_ms=time2,
            memory_size_in_bytes=mem_size,
        )


def detect_default_fortnite_replay_dir() -> Path:
    """Locate the standard Windows Fortnite replay directory."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "FortniteGame" / "Saved" / "Demos"
    return Path.home() / "AppData" / "Local" / "FortniteGame" / "Saved" / "Demos"
