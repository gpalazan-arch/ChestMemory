"""Automatic SQLite backup and verified recovery service for ChestMemory."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass(slots=True)
class BackupInfo:
    """Metadata describing a verified SQLite backup file."""

    path: Path
    created_at: datetime
    size_bytes: int
    sha256: str


class BackupService:
    """Creates consistent SQLite snapshots using the native online backup API."""

    def __init__(
        self,
        db_path: str | Path,
        backup_dir: str | Path,
        retention_count: int = 15,
    ) -> None:
        self.db_path = Path(db_path)
        self.backup_dir = Path(backup_dir)
        self.retention_count = max(1, int(retention_count))
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def compute_sha256(file_path: Path) -> str:
        digest = hashlib.sha256()
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def verify_sqlite_file(file_path: Path) -> tuple[bool, str]:
        """Verify that a SQLite file opens cleanly and passes PRAGMA integrity_check."""
        if not file_path.exists() or file_path.stat().st_size == 0:
            return False, "Backup file does not exist or is empty"
        try:
            conn = sqlite3.connect(f"file:{file_path}?mode=ro", uri=True)
            try:
                rows = conn.execute("PRAGMA integrity_check;").fetchall()
                msgs = [r[0] for r in rows]
                if msgs != ["ok"]:
                    return False, "; ".join(msgs)
                # Ensure core matches table exists
                tbl = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='matches';"
                ).fetchone()
                if tbl is None:
                    return False, "Missing required 'matches' table"
                return True, "ok"
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return False, str(exc)

    def create_backup(self, tag: str = "auto") -> BackupInfo:
        """Create a consistent backup using sqlite3.Connection.backup() + SHA-256 sidecar."""
        if not self.db_path.exists():
            raise FileNotFoundError(f"Database file not found: {self.db_path}")

        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y%m%d_%H%M%S_%f")
        safe_tag = "".join(c for c in tag if c.isalnum() or c in ("-", "_")) or "auto"
        target_path = self.backup_dir / f"chestmemory_{safe_tag}_{stamp}.db"
        temp_path = target_path.with_suffix(".db.tmp")

        src = sqlite3.connect(str(self.db_path), timeout=15.0)
        dst = sqlite3.connect(str(temp_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        ok, msg = self.verify_sqlite_file(temp_path)
        if not ok:
            temp_path.unlink(missing_ok=True)
            raise RuntimeError(f"Created backup failed integrity check: {msg}")

        temp_path.replace(target_path)
        digest = self.compute_sha256(target_path)
        target_path.with_suffix(".db.sha256").write_text(digest, encoding="utf-8")

        self._prune_old_backups()
        return BackupInfo(
            path=target_path,
            created_at=now,
            size_bytes=target_path.stat().st_size,
            sha256=digest,
        )

    def list_backups(self) -> list[BackupInfo]:
        """List available backups sorted newest first."""
        items: list[BackupInfo] = []
        for path in sorted(
            self.backup_dir.glob("chestmemory_*.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            sha_file = path.with_suffix(".db.sha256")
            sha = (
                sha_file.read_text(encoding="utf-8").strip()
                if sha_file.exists()
                else self.compute_sha256(path)
            )
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            items.append(
                BackupInfo(
                    path=path,
                    created_at=mtime,
                    size_bytes=path.stat().st_size,
                    sha256=sha,
                )
            )
        return items

    def restore_backup(self, backup_path: str | Path) -> Path:
        """Restore a verified backup over self.db_path, preserving a pre-restore safety copy."""
        source = Path(backup_path)
        if not source.exists():
            raise FileNotFoundError(f"Backup not found: {source}")

        sha_file = source.with_suffix(".db.sha256")
        if sha_file.exists():
            expected_sha = sha_file.read_text(encoding="utf-8").strip()
            actual_sha = self.compute_sha256(source)
            if actual_sha != expected_sha:
                raise ValueError(
                    f"Backup checksum mismatch ({source.name}): expected {expected_sha[:12]}, "
                    f"got {actual_sha[:12]}"
                )

        ok, msg = self.verify_sqlite_file(source)
        if not ok:
            raise ValueError(f"Backup failed integrity verification: {msg}")

        # Keep a pre-restore snapshot if current DB exists
        if self.db_path.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            pre_restore = self.backup_dir / f"pre_restore_{stamp}.db"
            shutil.copy2(self.db_path, pre_restore)

        # Clean WAL/SHM sidecar files before replacing the main SQLite file
        for suffix in ("-wal", "-shm"):
            wal_file = Path(f"{self.db_path}{suffix}")
            wal_file.unlink(missing_ok=True)

        temp_target = self.db_path.with_suffix(".db.restoring")
        shutil.copy2(source, temp_target)
        temp_target.replace(self.db_path)
        return self.db_path

    def restore_latest_valid_backup(self) -> Optional[Path]:
        """Find and restore the most recent valid backup (useful on DB corruption)."""
        for info in self.list_backups():
            ok, _ = self.verify_sqlite_file(info.path)
            if ok:
                return self.restore_backup(info.path)
        return None

    def _prune_old_backups(self) -> None:
        backups = self.list_backups()
        for stale in backups[self.retention_count :]:
            stale.path.unlink(missing_ok=True)
            stale.path.with_suffix(".db.sha256").unlink(missing_ok=True)
