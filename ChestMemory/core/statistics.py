"""Statistics calculation and export engine for ChestMemory."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from database.database import DatabaseManager
from database.models import MatchRecord, StatsSnapshot


@dataclass(slots=True)
class DailyBucket:
    """Aggregated chest count and match count for one calendar day (UTC)."""

    day: date
    chests: int
    matches: int


class StatisticsService:
    """Computes aggregated metrics, period breakdowns, and exports from SQLite."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def get_snapshot(self, now: Optional[datetime] = None) -> StatsSnapshot:
        return self._db.get_stats_snapshot(now=now)

    def get_recent_matches(self, limit: int = 100) -> list[MatchRecord]:
        return self._db.get_matches(limit=limit)

    def get_daily_breakdown(
        self,
        days: int = 30,
        now: Optional[datetime] = None,
    ) -> list[DailyBucket]:
        """Return contiguous daily buckets for the last `days` calendar days."""
        ref = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
        start_day = ref - timedelta(days=max(1, days) - 1)
        matches = self._db.get_all_matches()
        return self.aggregate_daily(matches, start_day=start_day, end_day=ref)

    @staticmethod
    def aggregate_daily(
        matches: Iterable[MatchRecord],
        start_day: date,
        end_day: date,
    ) -> list[DailyBucket]:
        chests_by_day: dict[date, int] = defaultdict(int)
        matches_by_day: dict[date, int] = defaultdict(int)

        for match in matches:
            d = match.played_at_utc().date()
            if start_day <= d <= end_day:
                chests_by_day[d] += match.chests_opened
                matches_by_day[d] += 1

        buckets: list[DailyBucket] = []
        cursor = start_day
        while cursor <= end_day:
            buckets.append(
                DailyBucket(
                    day=cursor,
                    chests=chests_by_day[cursor],
                    matches=matches_by_day[cursor],
                )
            )
            cursor += timedelta(days=1)
        return buckets

    def export_csv(self, destination: str | Path) -> Path:
        """Export full match history to a UTF-8 CSV file."""
        dest = Path(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)
        matches = self._db.get_all_matches()
        with dest.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "id",
                    "played_at_utc",
                    "chests_opened",
                    "file_name",
                    "replay_id",
                    "source",
                    "attribution_mode",
                    "match_duration_ms",
                    "eliminations",
                    "placement",
                ]
            )
            for m in matches:
                writer.writerow(
                    [
                        m.id,
                        m.played_at_utc().isoformat(),
                        m.chests_opened,
                        m.file_name,
                        m.replay_id,
                        m.source.value,
                        m.attribution_mode.value,
                        m.match_duration_ms,
                        "" if m.eliminations is None else m.eliminations,
                        "" if m.placement is None else m.placement,
                    ]
                )
        return dest

    def export_json(self, destination: str | Path) -> Path:
        """Export snapshot and full match history as JSON."""
        dest = Path(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)
        snap = self.get_snapshot()
        matches = self._db.get_all_matches()
        payload = {
            "exported_at_utc": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "total_chests": snap.total_chests,
                "total_matches": snap.total_matches,
                "today_chests": snap.today_chests,
                "week_chests": snap.week_chests,
                "month_chests": snap.month_chests,
                "last_match_chests": snap.last_match_chests,
                "average_per_match": snap.average_per_match,
                "best_match_chests": snap.best_match_chests,
            },
            "matches": [
                {
                    "id": m.id,
                    "replay_id": m.replay_id,
                    "file_name": m.file_name,
                    "chests_opened": m.chests_opened,
                    "played_at_utc": m.played_at_utc().isoformat(),
                    "source": m.source.value,
                    "attribution_mode": m.attribution_mode.value,
                    "match_duration_ms": m.match_duration_ms,
                    "eliminations": m.eliminations,
                    "placement": m.placement,
                }
                for m in matches
            ],
        }
        dest.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return dest


def format_chest_number(value: int) -> str:
    """Format integers with thin/space thousands separator, e.g. 12458 -> '12 458'."""
    return f"{int(value):,}".replace(",", " ")
