"""Binary Unreal Engine `.replay` fixture builder for deterministic tests."""

from __future__ import annotations

import struct
import uuid
import zlib
from datetime import datetime, timezone
from typing import Iterable, Optional

from detector.replay_analyzer import NETFIELD_STREAM_MAGIC, ContainerUpdate
from detector.replay_parser import (
    FILE_MAGIC,
    NETWORK_DEMO_HEADER_MAGIC,
    ChunkType,
    ReplayVersionHistory,
)


def pack_fstring(value: str, utf16: bool = False) -> bytes:
    if not value:
        return struct.pack("<i", 0)
    if utf16:
        encoded = (value + "\x00").encode("utf-16-le")
        return struct.pack("<i", -(len(value) + 1)) + encoded
    encoded = (value + "\x00").encode("utf-8")
    return struct.pack("<i", len(encoded)) + encoded


def datetime_to_dotnet_ticks(dt: datetime) -> int:
    utc = dt.astimezone(timezone.utc)
    unix_1970_dotnet = 621355968000000000
    return unix_1970_dotnet + int(utc.timestamp() * 10_000_000)


def pack_guid(guid_str: str) -> bytes:
    raw_be = uuid.UUID(guid_str).bytes
    a, b, c, d = struct.unpack(">4I", raw_be)
    return struct.pack("<4I", a, b, c, d)


def build_fnct_stream(
    local_pawns: Iterable[str],
    updates: Iterable[ContainerUpdate],
) -> bytes:
    pawns = list(local_pawns)
    upd_list = list(updates)
    parts = [
        NETFIELD_STREAM_MAGIC,
        struct.pack("<HH", 1, len(pawns)),
    ]
    for p in pawns:
        parts.append(pack_fstring(p))
    parts.append(struct.pack("<I", len(upd_list)))
    for u in upd_list:
        flags = 0
        if u.is_initial_bunch:
            flags |= 0x01
        if u.b_already_searched is not None:
            flags |= 0x02
            if u.b_already_searched:
                flags |= 0x04
        if u.search_animation_count is not None:
            flags |= 0x08
        if u.searching_pawn_guid is not None:
            flags |= 0x10
        if u.b_destroyed:
            flags |= 0x20

        parts.append(struct.pack("<I", u.timestamp_ms))
        parts.append(pack_fstring(u.container_guid))
        parts.append(pack_fstring(u.container_class))
        parts.append(struct.pack("<B", flags))
        if u.search_animation_count is not None:
            parts.append(struct.pack("<I", u.search_animation_count))
        if u.searching_pawn_guid is not None:
            parts.append(pack_fstring(u.searching_pawn_guid))
    return b"".join(parts)


def build_replay_bytes(
    *,
    match_guid: str = "12345678-1234-5678-1234-567812345678",
    friendly_name: str = "UnsavedReplay-2026.11.21",
    length_in_ms: int = 920_000,
    file_version: int = int(ReplayVersionHistory.HISTORY_ENCRYPTION),
    custom_versions: Iterable[tuple[str, int]] = (),
    is_live: bool = False,
    is_compressed: bool = False,
    compress_with_zlib: bool = False,
    is_encrypted: bool = False,
    encryption_key: bytes = b"",
    timestamp: Optional[datetime] = None,
    local_pawns: Iterable[str] = (),
    updates: Iterable[ContainerUpdate] = (),
    raw_replay_data: Optional[bytes] = None,
    eliminations: Optional[int] = None,
    placement: Optional[int] = None,
    total_players: int = 100,
    utf16_friendly_name: bool = False,
) -> bytes:
    """Construct a valid Unreal Engine / Fortnite `.replay` binary archive."""
    ts = timestamp or datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
    cv_list = list(custom_versions)

    meta = bytearray()
    meta += struct.pack("<II", FILE_MAGIC, file_version)
    if file_version >= int(ReplayVersionHistory.HISTORY_CUSTOM_VERSIONS):
        meta += struct.pack("<i", len(cv_list))
        for cv_guid, cv_ver in cv_list:
            meta += pack_guid(cv_guid)
            meta += struct.pack("<i", cv_ver)
    meta += struct.pack("<III", length_in_ms, 23, 39100231)
    meta += pack_fstring(friendly_name, utf16=utf16_friendly_name)
    meta += struct.pack("<I", 1 if is_live else 0)
    meta += struct.pack("<q", datetime_to_dotnet_ticks(ts))
    meta += struct.pack("<I", 1 if is_compressed else 0)
    meta += struct.pack("<I", 1 if is_encrypted else 0)
    meta += struct.pack("<i", len(encryption_key)) + encryption_key

    chunks = bytearray()

    # 1. Header Chunk (FNetworkDemoHeader)
    header_payload = bytearray()
    header_payload += struct.pack("<II", NETWORK_DEMO_HEADER_MAGIC, 23)
    if file_version >= int(ReplayVersionHistory.HISTORY_CUSTOM_VERSIONS):
        header_payload += struct.pack("<i", len(cv_list))
        for cv_guid, cv_ver in cv_list:
            header_payload += pack_guid(cv_guid)
            header_payload += struct.pack("<i", cv_ver)
    header_payload += struct.pack("<III", 0xAABBCCDD, 28, 35)
    header_payload += pack_guid(match_guid)
    header_payload += struct.pack("<HHHI", 5, 4, 2, 39100231)
    header_payload += pack_fstring("++Fortnite+Release-32.10")
    header_payload += struct.pack("<i", 1)
    header_payload += pack_fstring("/Game/Athena/Apollo/Maps/Helios_Terrain")
    header_payload += struct.pack("<I", 0)
    header_payload += struct.pack("<I", 1)  # flags
    header_payload += struct.pack("<i", 0)  # game_specific_data
    chunks += struct.pack("<Ii", int(ChunkType.HEADER), len(header_payload)) + header_payload

    # 2. Optional AthenaMatchStats event chunk
    if eliminations is not None:
        ev_data = struct.pack(
            "<IfIIIIIIIIII",
            0,
            0.34,
            2,
            eliminations,
            1250,
            140,
            1,
            320,
            450,
            600,
            420,
            3100,
        )
        ev_chunk = (
            pack_fstring("evt_stats")
            + pack_fstring("AthenaReplayBrowserEvents")
            + pack_fstring("AthenaMatchStats")
            + struct.pack("<IIi", 0, length_in_ms, len(ev_data))
            + ev_data
        )
        chunks += struct.pack("<Ii", int(ChunkType.EVENT), len(ev_chunk)) + ev_chunk

    # 3. Optional AthenaMatchTeamStats event chunk
    if placement is not None:
        team_data = struct.pack("<III", 0, placement, total_players)
        team_chunk = (
            pack_fstring("evt_team")
            + pack_fstring("AthenaReplayBrowserEvents")
            + pack_fstring("AthenaMatchTeamStats")
            + struct.pack("<IIi", 0, length_in_ms, len(team_data))
            + team_data
        )
        chunks += struct.pack("<Ii", int(ChunkType.EVENT), len(team_chunk)) + team_chunk

    # 4. ReplayData chunk
    if raw_replay_data is not None:
        stream_bytes = raw_replay_data
    else:
        stream_bytes = build_fnct_stream(local_pawns=local_pawns, updates=updates)

    if is_compressed:
        if compress_with_zlib:
            comp = zlib.compress(stream_bytes)
            stream_bytes = struct.pack("<ii", len(stream_bytes), len(comp)) + comp
        else:
            # Simulated Oodle compressed payload (header not starting with 0x78)
            fake_oodle = b"\x8c\x06\x00\x01" + stream_bytes
            stream_bytes = struct.pack("<ii", len(stream_bytes), len(fake_oodle)) + fake_oodle

    data_chunk = (
        struct.pack("<IIii", 0, length_in_ms, len(stream_bytes), len(stream_bytes))
        + stream_bytes
    )
    chunks += struct.pack("<Ii", int(ChunkType.REPLAY_DATA), len(data_chunk)) + data_chunk

    return bytes(meta + chunks)
