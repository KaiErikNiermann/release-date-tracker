"""Persistent cache for mined studio trends.

A studio's release-timing pattern (:class:`release_tracker.trends.StudioTrend`) is
expensive to mine — an extra API round-trip — but changes slowly, so we memoize it
on disk keyed by ``(studio_key, kind)`` with a TTL. This is a *derived, disposable*
cache: deleting the file only forces a re-mine. It is deliberately a separate
SQLite file from the entity DB so the stateless ``rdt rd`` lookup never has to open
the tracked-entity database.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from release_tracker.models import MediaKind
from release_tracker.trends import Season, StudioTrend

# Output deals / studio habits drift slowly; a quarter is a safe refresh window.
TREND_TTL_DAYS: Final[int] = 90

_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS studio_trends (
    studio_key       TEXT NOT NULL,
    kind             TEXT NOT NULL,
    studio_name      TEXT NOT NULL,
    n_samples        INTEGER NOT NULL,
    months_json      TEXT NOT NULL,
    month_mode       INTEGER,
    month_conc       REAL NOT NULL,
    season_mode      TEXT,
    season_conc      REAL NOT NULL,
    computed_at      TEXT NOT NULL,
    PRIMARY KEY (studio_key, kind)
);
"""


class TrendCache:
    """Thin TTL'd SQLite store for :class:`StudioTrend` records."""

    def __init__(self, path: Path, *, ttl_days: int = TREND_TTL_DAYS) -> None:
        self.path = path
        self.ttl = timedelta(days=ttl_days)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> TrendCache:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get(self, studio_key: str, kind: MediaKind) -> StudioTrend | None:
        """Return the cached trend if present and still fresh, else ``None``."""
        row = self._conn.execute(
            "SELECT * FROM studio_trends WHERE studio_key = ? AND kind = ?",
            (studio_key, kind.value),
        ).fetchone()
        if row is None:
            return None
        computed_at = datetime.fromisoformat(row["computed_at"])
        if datetime.now(UTC) - computed_at > self.ttl:
            return None
        return _row_to_trend(row)

    def put(self, trend: StudioTrend) -> None:
        """Upsert a mined trend, stamping it with the current time."""
        self._conn.execute(
            """
            INSERT INTO studio_trends (studio_key, kind, studio_name, n_samples,
                months_json, month_mode, month_conc, season_mode, season_conc, computed_at)
            VALUES (:studio_key, :kind, :studio_name, :n, :months, :month_mode,
                :month_conc, :season_mode, :season_conc, :now)
            ON CONFLICT(studio_key, kind) DO UPDATE SET
                studio_name=excluded.studio_name,
                n_samples=excluded.n_samples,
                months_json=excluded.months_json,
                month_mode=excluded.month_mode,
                month_conc=excluded.month_conc,
                season_mode=excluded.season_mode,
                season_conc=excluded.season_conc,
                computed_at=excluded.computed_at
            """,
            {
                "studio_key": trend.studio_key,
                "kind": trend.kind.value,
                "studio_name": trend.studio_name,
                "n": trend.n,
                "months": json.dumps(list(trend.months)),
                "month_mode": trend.month_mode,
                "month_conc": trend.month_concentration,
                "season_mode": trend.season_mode.value if trend.season_mode else None,
                "season_conc": trend.season_concentration,
                "now": datetime.now(UTC).isoformat(),
            },
        )
        self._conn.commit()


def _row_to_trend(row: sqlite3.Row) -> StudioTrend:
    return StudioTrend(
        studio_key=row["studio_key"],
        studio_name=row["studio_name"],
        kind=MediaKind(row["kind"]),
        n=row["n_samples"],
        months=tuple(json.loads(row["months_json"])),
        month_mode=row["month_mode"],
        month_concentration=row["month_conc"],
        season_mode=Season(row["season_mode"]) if row["season_mode"] else None,
        season_concentration=row["season_conc"],
    )
