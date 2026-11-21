"""SQLite persistence layer for ChestMemory with deduplication and integrity checks."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

from .models import (
    AnalysisResult,
    AnalysisStatus,
    AppConfig,
    AttributionMode,
    ChestEventRecord,
    DetectionSource,
    FailedAnalysisRecord,
    MatchRecord,
    StatsSnapshot,
)

SCHEMA_VERSION = 1


class DatabaseError(RuntimeError):
    """Raised when a SQLite operation or integrity validation fails."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _dt_to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _iso_to_dt(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class DatabaseManager:
    """Thread-safe SQLite repository storing matches, chest events, failures, and settings."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=15.0,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA foreign_keys = ON;")
                conn.execute("PRAGMA journal_mode = WAL;")
                conn.execute("PRAGMA synchronous = NORMAL;")
                yield conn
            finally:
                conn.close()

    def _initialize_schema(self) -> None:
        with self._connect() as conn:
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS schema_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS matches (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        replay_id TEXT NOT NULL UNIQUE,
                        file_hash TEXT NOT NULL UNIQUE,
                        file_name TEXT NOT NULL,
                        chests_opened INTEGER NOT NULL CHECK (chests_opened >= 0),
                        played_at TEXT NOT NULL,
                        processed_at TEXT NOT NULL,
                        source TEXT NOT NULL,
                        attribution_mode TEXT NOT NULL DEFAULT 'local_player',
                        match_duration_ms INTEGER NOT NULL DEFAULT 0,
                        friendly_name TEXT NOT NULL DEFAULT '',
                        engine_version TEXT NOT NULL DEFAULT '',
                        eliminations INTEGER,
                        placement INTEGER
                    );

                    CREATE INDEX IF NOT EXISTS idx_matches_played_at
                        ON matches (played_at DESC);

                    CREATE TABLE IF NOT EXISTS chest_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        match_id INTEGER NOT NULL,
                        timestamp_ms INTEGER NOT NULL,
                        container_guid TEXT NOT NULL,
                        container_class TEXT NOT NULL,
                        search_count INTEGER NOT NULL DEFAULT 1,
                        opened_by_local_player INTEGER NOT NULL DEFAULT 1,
                        confidence REAL NOT NULL DEFAULT 1.0,
                        FOREIGN KEY (match_id) REFERENCES matches(id) ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_chest_events_match_id
                        ON chest_events (match_id);

                    CREATE TABLE IF NOT EXISTS failed_analyses (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        replay_id TEXT NOT NULL,
                        file_hash TEXT NOT NULL UNIQUE,
                        file_name TEXT NOT NULL,
                        status TEXT NOT NULL,
                        error_message TEXT NOT NULL,
                        attempted_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    """
                )
                conn.execute(
                    "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', ?);",
                    (str(SCHEMA_VERSION),),
                )
            except sqlite3.Error as exc:
                if conn.in_transaction:
                    conn.execute("ROLLBACK;")
                raise DatabaseError(f"Failed to initialize database schema: {exc}") from exc

    def check_integrity(self) -> tuple[bool, str]:
        """Run SQLite PRAGMA integrity_check and return (ok, message)."""
        try:
            with self._connect() as conn:
                rows = conn.execute("PRAGMA integrity_check;").fetchall()
                messages = [row[0] for row in rows]
                if messages == ["ok"]:
                    return True, "ok"
                return False, "; ".join(messages)
        except sqlite3.Error as exc:
            return False, str(exc)

    def is_replay_processed(self, replay_id: str, file_hash: str) -> bool:
        """Return True if a replay with this ID or SHA-256 hash was already saved."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM matches
                WHERE replay_id = ? OR file_hash = ?
                LIMIT 1;
                """,
                (replay_id, file_hash),
            ).fetchone()
            return row is not None

    def is_failed_hash_recorded(self, file_hash: str) -> bool:
        """Return True if this exact file hash already failed analysis."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM failed_analyses WHERE file_hash = ? LIMIT 1;",
                (file_hash,),
            ).fetchone()
            return row is not None

    def save_analysis_result(self, result: AnalysisResult) -> tuple[bool, Optional[MatchRecord]]:
        """Persist a verified AnalysisResult.

        Returns (inserted, MatchRecord | None).
        - Raises ValueError if result.is_reliable is False (never writes fake 0s!).
        - Returns (False, existing_record) if the replay was already processed.
        """
        if not result.is_reliable or result.chests_opened is None:
            raise ValueError(
                "Refusing to save unreliable analysis result as a valid match. "
                "Use record_failed_analysis() instead."
            )

        played_at = result.match_timestamp or _utc_now()
        processed_at = _utc_now()
        file_name = Path(result.file_path).name if result.file_path else result.replay_id

        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT * FROM matches
                WHERE replay_id = ? OR file_hash = ?
                LIMIT 1;
                """,
                (result.replay_id, result.file_hash),
            ).fetchone()
            if existing is not None:
                return False, self._row_to_match(existing)

            conn.execute("BEGIN;")
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO matches (
                        replay_id, file_hash, file_name, chests_opened,
                        played_at, processed_at, source, attribution_mode,
                        match_duration_ms, friendly_name, engine_version,
                        eliminations, placement
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        result.replay_id,
                        result.file_hash,
                        file_name,
                        int(result.chests_opened),
                        _dt_to_iso(played_at),
                        _dt_to_iso(processed_at),
                        result.source.value,
                        result.attribution_mode.value,
                        int(result.match_duration_ms),
                        result.friendly_name,
                        result.engine_version,
                        result.eliminations,
                        result.placement,
                    ),
                )
                match_id = int(cursor.lastrowid)

                if result.events:
                    conn.executemany(
                        """
                        INSERT INTO chest_events (
                            match_id, timestamp_ms, container_guid, container_class,
                            search_count, opened_by_local_player, confidence
                        ) VALUES (?, ?, ?, ?, ?, ?, ?);
                        """,
                        [
                            (
                                match_id,
                                int(ev.timestamp_ms),
                                ev.container_guid,
                                ev.container_class,
                                int(ev.search_count),
                                1 if ev.opened_by_local_player else 0,
                                float(ev.confidence),
                            )
                            for ev in result.events
                        ],
                    )

                # Clear any earlier failure record for this file hash now that it succeeded
                conn.execute(
                    "DELETE FROM failed_analyses WHERE file_hash = ? OR replay_id = ?;",
                    (result.file_hash, result.replay_id),
                )
                conn.execute("COMMIT;")

                record = MatchRecord(
                    id=match_id,
                    replay_id=result.replay_id,
                    file_hash=result.file_hash,
                    file_name=file_name,
                    chests_opened=int(result.chests_opened),
                    played_at=played_at,
                    processed_at=processed_at,
                    source=result.source,
                    attribution_mode=result.attribution_mode,
                    match_duration_ms=int(result.match_duration_ms),
                    friendly_name=result.friendly_name,
                    engine_version=result.engine_version,
                    eliminations=result.eliminations,
                    placement=result.placement,
                )
                return True, record
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK;")
                existing = conn.execute(
                    "SELECT * FROM matches WHERE replay_id = ? OR file_hash = ? LIMIT 1;",
                    (result.replay_id, result.file_hash),
                ).fetchone()
                return False, self._row_to_match(existing) if existing else None
            except sqlite3.Error as exc:
                conn.execute("ROLLBACK;")
                raise DatabaseError(f"Failed to save match result: {exc}") from exc

    def record_live_visual_chest(
        self,
        session_id: str,
        event: ChestEventRecord,
        friendly_name: str = "Live Visual CV Match",
        played_at: Optional[datetime] = None,
    ) -> MatchRecord:
        """Atomically create or increment a live Visual CV match row on each opened chest."""
        now = played_at or _utc_now()
        replay_id = f"visual_live:{session_id}"
        file_hash = f"visual_live_hash:{session_id}"
        file_name = f"live_cv_{session_id}.cv"

        with self._connect() as conn:
            conn.execute("BEGIN;")
            try:
                existing = conn.execute(
                    "SELECT * FROM matches WHERE replay_id = ? LIMIT 1;",
                    (replay_id,),
                ).fetchone()

                if existing is None:
                    cur = conn.execute(
                        """
                        INSERT INTO matches (
                            replay_id, file_hash, file_name, chests_opened,
                            played_at, processed_at, source, attribution_mode,
                            match_duration_ms, friendly_name, engine_version
                        ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            replay_id,
                            file_hash,
                            file_name,
                            _dt_to_iso(now),
                            _dt_to_iso(now),
                            DetectionSource.VISUAL_CV.value,
                            AttributionMode.VISUAL_LOCAL_HUD.value,
                            int(event.timestamp_ms),
                            friendly_name,
                            "OpenCV-LiveVisualFSM",
                        ),
                    )
                    match_id = int(cur.lastrowid)
                else:
                    match_id = int(existing["id"])
                    conn.execute(
                        """
                        UPDATE matches
                        SET chests_opened = chests_opened + 1,
                            processed_at = ?,
                            match_duration_ms = MAX(match_duration_ms, ?)
                        WHERE id = ?;
                        """,
                        (_dt_to_iso(now), int(event.timestamp_ms), match_id),
                    )

                conn.execute(
                    """
                    INSERT INTO chest_events (
                        match_id, timestamp_ms, container_guid, container_class,
                        search_count, opened_by_local_player, confidence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        match_id,
                        int(event.timestamp_ms),
                        event.container_guid,
                        event.container_class,
                        int(event.search_count),
                        1 if event.opened_by_local_player else 0,
                        float(event.confidence),
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM matches WHERE id = ?;",
                    (match_id,),
                ).fetchone()
                conn.execute("COMMIT;")
                return self._row_to_match(row)
            except sqlite3.Error as exc:
                if conn.in_transaction:
                    conn.execute("ROLLBACK;")
                raise DatabaseError(f"Failed to record live visual chest: {exc}") from exc

    def delete_live_session(self, session_id: str) -> bool:
        """Delete a temporary live Visual CV match row (used when authoritative replay reconciles it)."""
        replay_id = f"visual_live:{session_id}"
        with self._connect() as conn:
            conn.execute("BEGIN;")
            try:
                cur = conn.execute("DELETE FROM matches WHERE replay_id = ?;", (replay_id,))
                conn.execute("COMMIT;")
                return cur.rowcount > 0
            except sqlite3.Error as exc:
                if conn.in_transaction:
                    conn.execute("ROLLBACK;")
                raise DatabaseError(f"Failed to delete live visual session: {exc}") from exc

    def attach_replay_metadata_to_live_session(
        self,
        session_id: str,
        replay_id: str,
        file_hash: str,
        file_name: str,
        friendly_name: str = "",
        match_duration_ms: int = 0,
        eliminations: Optional[int] = None,
        placement: Optional[int] = None,
    ) -> Optional[MatchRecord]:
        """Link a finished `.replay` file's identity/metadata to an active live Visual CV session.

        Prevents duplicate counting or false failure logs when Visual CV already counted chests
        live during a match whose `.replay` cannot be binary-decompressed.
        """
        live_replay_id = f"visual_live:{session_id}"
        now = _utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN;")
            try:
                existing = conn.execute(
                    "SELECT * FROM matches WHERE replay_id = ? LIMIT 1;",
                    (live_replay_id,),
                ).fetchone()
                if existing is None:
                    conn.execute("ROLLBACK;")
                    return None

                # Ensure target replay_id / file_hash is not already recorded on another match
                conflict = conn.execute(
                    "SELECT id FROM matches WHERE (replay_id = ? OR file_hash = ?) AND id != ? LIMIT 1;",
                    (replay_id, file_hash, int(existing["id"])),
                ).fetchone()
                if conflict is not None:
                    conn.execute("ROLLBACK;")
                    return None

                match_id = int(existing["id"])
                conn.execute(
                    """
                    UPDATE matches
                    SET replay_id = ?,
                        file_hash = ?,
                        file_name = ?,
                        friendly_name = CASE WHEN ? != '' THEN ? ELSE friendly_name END,
                        match_duration_ms = MAX(match_duration_ms, ?),
                        eliminations = COALESCE(?, eliminations),
                        placement = COALESCE(?, placement),
                        processed_at = ?,
                        engine_version = 'Hybrid-VisualCV+ReplayMeta'
                    WHERE id = ?;
                    """,
                    (
                        replay_id,
                        file_hash,
                        file_name,
                        friendly_name,
                        friendly_name,
                        int(match_duration_ms),
                        eliminations,
                        placement,
                        _dt_to_iso(now),
                        match_id,
                    ),
                )
                conn.execute(
                    "DELETE FROM failed_analyses WHERE file_hash = ? OR replay_id = ?;",
                    (file_hash, replay_id),
                )
                row = conn.execute("SELECT * FROM matches WHERE id = ?;", (match_id,)).fetchone()
                conn.execute("COMMIT;")
                return self._row_to_match(row) if row else None
            except sqlite3.Error as exc:
                if conn.in_transaction:
                    conn.execute("ROLLBACK;")
                raise DatabaseError(f"Failed to attach replay metadata to live session: {exc}") from exc

    def record_failed_analysis(self, result: AnalysisResult) -> FailedAnalysisRecord:
        """Record a failed analysis attempt separately from valid matches."""
        attempted_at = _utc_now()
        file_name = Path(result.file_path).name if result.file_path else result.replay_id
        with self._connect() as conn:
            conn.execute("BEGIN;")
            try:
                conn.execute(
                    """
                    INSERT INTO failed_analyses (
                        replay_id, file_hash, file_name, status, error_message, attempted_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(file_hash) DO UPDATE SET
                        replay_id = excluded.replay_id,
                        file_name = excluded.file_name,
                        status = excluded.status,
                        error_message = excluded.error_message,
                        attempted_at = excluded.attempted_at;
                    """,
                    (
                        result.replay_id,
                        result.file_hash,
                        file_name,
                        result.status.value,
                        result.error_message or result.status.value,
                        _dt_to_iso(attempted_at),
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM failed_analyses WHERE file_hash = ?;",
                    (result.file_hash,),
                ).fetchone()
                conn.execute("COMMIT;")
                return self._row_to_failed(row)
            except sqlite3.Error as exc:
                conn.execute("ROLLBACK;")
                raise DatabaseError(f"Failed to store failed analysis record: {exc}") from exc

    def clear_failed_analyses(self) -> int:
        """Delete failed analysis markers so files can be retried after config updates."""
        with self._connect() as conn:
            conn.execute("BEGIN;")
            try:
                cur = conn.execute("DELETE FROM failed_analyses;")
                conn.execute("COMMIT;")
                return cur.rowcount
            except sqlite3.Error as exc:
                conn.execute("ROLLBACK;")
                raise DatabaseError(f"Failed to clear failed analyses: {exc}") from exc

    def get_failed_analyses(self, limit: int = 50) -> list[FailedAnalysisRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM failed_analyses
                ORDER BY attempted_at DESC
                LIMIT ?;
                """,
                (limit,),
            ).fetchall()
            return [self._row_to_failed(r) for r in rows]

    def get_matches(self, limit: int = 100, offset: int = 0) -> list[MatchRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM matches
                ORDER BY played_at DESC, id DESC
                LIMIT ? OFFSET ?;
                """,
                (limit, offset),
            ).fetchall()
            return [self._row_to_match(r) for r in rows]

    def get_all_matches(self) -> list[MatchRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM matches ORDER BY played_at DESC, id DESC;"
            ).fetchall()
            return [self._row_to_match(r) for r in rows]

    def get_match_events(self, match_id: int) -> list[ChestEventRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM chest_events
                WHERE match_id = ?
                ORDER BY timestamp_ms ASC, id ASC;
                """,
                (match_id,),
            ).fetchall()
            return [
                ChestEventRecord(
                    timestamp_ms=int(r["timestamp_ms"]),
                    container_guid=str(r["container_guid"]),
                    container_class=str(r["container_class"]),
                    search_count=int(r["search_count"]),
                    opened_by_local_player=bool(r["opened_by_local_player"]),
                    confidence=float(r["confidence"]),
                )
                for r in rows
            ]

    def delete_match(self, match_id: int) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN;")
            try:
                cur = conn.execute("DELETE FROM matches WHERE id = ?;", (match_id,))
                conn.execute("COMMIT;")
                return cur.rowcount > 0
            except sqlite3.Error as exc:
                conn.execute("ROLLBACK;")
                raise DatabaseError(f"Failed to delete match {match_id}: {exc}") from exc

    def get_stats_snapshot(self, now: Optional[datetime] = None) -> StatsSnapshot:
        """Compute aggregate statistics for all-time, today, week, month, and last match."""
        ref_now = (now or _utc_now()).astimezone(timezone.utc)
        start_of_day = ref_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_of_week = start_of_day - timedelta(days=start_of_day.weekday())
        start_of_month = start_of_day.replace(day=1)

        with self._connect() as conn:
            totals = conn.execute(
                """
                SELECT
                    COALESCE(SUM(chests_opened), 0) AS total_chests,
                    COUNT(*) AS total_matches,
                    COALESCE(MAX(chests_opened), 0) AS best_match
                FROM matches;
                """
            ).fetchone()

            today_row = conn.execute(
                """
                SELECT COALESCE(SUM(chests_opened), 0) AS chests
                FROM matches
                WHERE played_at >= ?;
                """,
                (_dt_to_iso(start_of_day),),
            ).fetchone()

            week_row = conn.execute(
                """
                SELECT COALESCE(SUM(chests_opened), 0) AS chests
                FROM matches
                WHERE played_at >= ?;
                """,
                (_dt_to_iso(start_of_week),),
            ).fetchone()

            month_row = conn.execute(
                """
                SELECT COALESCE(SUM(chests_opened), 0) AS chests
                FROM matches
                WHERE played_at >= ?;
                """,
                (_dt_to_iso(start_of_month),),
            ).fetchone()

            last_row = conn.execute(
                """
                SELECT chests_opened, played_at
                FROM matches
                ORDER BY played_at DESC, id DESC
                LIMIT 1;
                """
            ).fetchone()

            failed_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM failed_analyses;"
            ).fetchone()

        total_chests = int(totals["total_chests"])
        total_matches = int(totals["total_matches"])
        avg = (total_chests / total_matches) if total_matches > 0 else 0.0

        return StatsSnapshot(
            total_chests=total_chests,
            total_matches=total_matches,
            today_chests=int(today_row["chests"]),
            week_chests=int(week_row["chests"]),
            month_chests=int(month_row["chests"]),
            last_match_chests=int(last_row["chests_opened"]) if last_row else None,
            last_match_at=_iso_to_dt(last_row["played_at"]) if last_row else None,
            average_per_match=round(avg, 2),
            best_match_chests=int(totals["best_match"]),
            failed_replays_count=int(failed_row["cnt"]),
        )

    def load_config(self) -> AppConfig:
        """Load persisted AppConfig from settings table, filling defaults for missing keys."""
        default_cfg = AppConfig()
        data = asdict(default_cfg)
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings;").fetchall()
            for row in rows:
                key = str(row["key"])
                if key in data:
                    try:
                        data[key] = json.loads(row["value"])
                    except json.JSONDecodeError:
                        data[key] = row["value"]
        return AppConfig(**data)

    def save_config(self, config: AppConfig) -> None:
        """Persist AppConfig atomically into settings table."""
        payload = asdict(config)
        with self._connect() as conn:
            conn.execute("BEGIN;")
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?);",
                    [(k, json.dumps(v)) for k, v in payload.items()],
                )
                conn.execute("COMMIT;")
            except sqlite3.Error as exc:
                conn.execute("ROLLBACK;")
                raise DatabaseError(f"Failed to save configuration: {exc}") from exc

    @staticmethod
    def _row_to_match(row: sqlite3.Row) -> MatchRecord:
        return MatchRecord(
            id=int(row["id"]),
            replay_id=str(row["replay_id"]),
            file_hash=str(row["file_hash"]),
            file_name=str(row["file_name"]),
            chests_opened=int(row["chests_opened"]),
            played_at=_iso_to_dt(row["played_at"]),
            processed_at=_iso_to_dt(row["processed_at"]),
            source=DetectionSource(row["source"]),
            attribution_mode=AttributionMode(row["attribution_mode"]),
            match_duration_ms=int(row["match_duration_ms"]),
            friendly_name=str(row["friendly_name"]),
            engine_version=str(row["engine_version"]),
            eliminations=row["eliminations"],
            placement=row["placement"],
        )

    @staticmethod
    def _row_to_failed(row: sqlite3.Row) -> FailedAnalysisRecord:
        return FailedAnalysisRecord(
            id=int(row["id"]),
            replay_id=str(row["replay_id"]),
            file_hash=str(row["file_hash"]),
            file_name=str(row["file_name"]),
            status=AnalysisStatus(row["status"]),
            error_message=str(row["error_message"]),
            attempted_at=_iso_to_dt(row["attempted_at"]),
        )
