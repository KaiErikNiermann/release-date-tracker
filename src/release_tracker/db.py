"""SQLite persistence.

Design goals (per project conventions):
- Incremental, resumable writes: every upsert is idempotent on a natural key, so
  a crashed/interrupted run never duplicates and re-runs only refresh rows.
- Observations are append-mostly and immutable in spirit: an observation's
  identity is the deterministic hash of (entity, channel, region, date, provider,
  source). Re-seeing the same claim updates volatile fields (confidence,
  fetched_at) but never forks a new row.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from release_tracker.models import (
    Certainty,
    DatePrecision,
    Entity,
    MediaKind,
    Money,
    ReleaseChannel,
    ReleaseObservation,
    SourceTier,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    kind            TEXT NOT NULL,
    aliases         TEXT NOT NULL DEFAULT '[]',
    external_ids    TEXT NOT NULL DEFAULT '{}',
    notion_page_id  TEXT,
    notes           TEXT,
    watch           INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    id              TEXT PRIMARY KEY,
    entity_id       TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    channel         TEXT NOT NULL,
    region          TEXT NOT NULL,
    release_date    TEXT,
    date_end        TEXT,
    precision       TEXT NOT NULL,
    price_minor     INTEGER,
    price_currency  TEXT,
    certainty       TEXT NOT NULL,
    source_tier     INTEGER NOT NULL,
    provider        TEXT NOT NULL,
    source_name     TEXT,
    source_url      TEXT,
    source_quote    TEXT,
    published_at    TEXT,
    confidence      REAL NOT NULL,
    fetched_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_obs_entity ON observations(entity_id);
CREATE INDEX IF NOT EXISTS idx_obs_lookup ON observations(entity_id, channel, region);
"""


class Database:
    """Thin typed wrapper over a SQLite connection."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Generator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # -- entities ----------------------------------------------------------
    def upsert_entity(self, entity: Entity) -> None:
        now = datetime.now(UTC).isoformat()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO entities (id, title, kind, aliases, external_ids,
                    notion_page_id, notes, watch, created_at, updated_at)
                VALUES (:id, :title, :kind, :aliases, :external_ids,
                    :notion_page_id, :notes, :watch, :now, :now)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    kind=excluded.kind,
                    aliases=excluded.aliases,
                    -- merge external_ids: keep existing, add new (handled in app layer)
                    external_ids=excluded.external_ids,
                    notion_page_id=COALESCE(excluded.notion_page_id, entities.notion_page_id),
                    notes=COALESCE(excluded.notes, entities.notes),
                    watch=excluded.watch,
                    updated_at=:now
                """,
                {
                    "id": entity.id,
                    "title": entity.title,
                    "kind": entity.kind.value,
                    "aliases": json.dumps(list(entity.aliases)),
                    "external_ids": json.dumps(entity.external_ids),
                    "notion_page_id": entity.notion_page_id,
                    "notes": entity.notes,
                    "watch": int(entity.watch),
                    "now": now,
                },
            )

    def get_entity(self, entity_id: str) -> Entity | None:
        row = self._conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
        return _row_to_entity(row) if row else None

    def iter_entities(self, *, watched_only: bool = False) -> Iterator[Entity]:
        sql = "SELECT * FROM entities"
        if watched_only:
            sql += " WHERE watch = 1"
        sql += " ORDER BY title"
        for row in self._conn.execute(sql):
            yield _row_to_entity(row)

    def find_entities(self, query: str) -> list[Entity]:
        """Resolve an entity reference: exact id, else case-insensitive title substring."""
        exact = self.get_entity(query)
        if exact is not None:
            return [exact]
        rows = self._conn.execute(
            "SELECT * FROM entities WHERE lower(title) LIKE ? ORDER BY title",
            (f"%{query.lower()}%",),
        )
        return [_row_to_entity(r) for r in rows]

    def observation_dates(self, entity_id: str) -> list[date]:
        """All known release dates for an entity (for released/upcoming + year hints)."""
        rows = self._conn.execute(
            "SELECT release_date FROM observations "
            "WHERE entity_id = ? AND release_date IS NOT NULL",
            (entity_id,),
        )
        return [date.fromisoformat(r[0]) for r in rows]

    def merge_external_ids(self, entity_id: str, ids: dict[str, str]) -> None:
        """Non-destructively fold newly discovered external IDs into an entity."""
        if not ids:
            return
        existing = self.get_entity(entity_id)
        if existing is None:
            return
        merged = {**existing.external_ids, **ids}
        with self._tx() as conn:
            conn.execute(
                "UPDATE entities SET external_ids = ?, updated_at = ? WHERE id = ?",
                (json.dumps(merged), datetime.now(UTC).isoformat(), entity_id),
            )

    # -- observations ------------------------------------------------------
    def upsert_observation(self, obs: ReleaseObservation) -> None:
        with self._tx() as conn:
            _insert_observation(conn, obs)

    def delete_observations(self, entity_id: str, providers: tuple[str, ...]) -> int:
        """Drop an entity's rows for the given providers (refresh before re-pull).

        Keeps rows from other providers (e.g. the hand-authored 'notion' seed) so a
        re-pull after re-pinning an id doesn't leave stale wrong-match ghosts.
        """
        if not providers:
            return 0
        placeholders = ",".join("?" for _ in providers)
        with self._tx() as conn:
            cur = conn.execute(
                f"DELETE FROM observations WHERE entity_id = ? AND provider IN ({placeholders})",  # noqa: S608
                (entity_id, *providers),
            )
            return cur.rowcount

    def upsert_observations(self, observations: Iterable[ReleaseObservation]) -> int:
        count = 0
        with self._tx() as conn:
            for obs in observations:
                _insert_observation(conn, obs)
                count += 1
        return count

    def iter_observations(self, entity_id: str) -> Iterator[ReleaseObservation]:
        rows = self._conn.execute(
            "SELECT * FROM observations WHERE entity_id = ? ORDER BY confidence DESC",
            (entity_id,),
        )
        for row in rows:
            yield _row_to_observation(row)


# --- row mapping ----------------------------------------------------------
def _insert_observation(conn: sqlite3.Connection, obs: ReleaseObservation) -> None:
    conn.execute(
        """
        INSERT INTO observations (id, entity_id, channel, region, release_date,
            date_end, precision, price_minor, price_currency, certainty,
            source_tier, provider, source_name, source_url, source_quote,
            published_at, confidence, fetched_at)
        VALUES (:id, :entity_id, :channel, :region, :release_date, :date_end,
            :precision, :price_minor, :price_currency, :certainty, :source_tier,
            :provider, :source_name, :source_url, :source_quote, :published_at,
            :confidence, :fetched_at)
        ON CONFLICT(id) DO UPDATE SET
            precision=excluded.precision,
            price_minor=excluded.price_minor,
            price_currency=excluded.price_currency,
            certainty=excluded.certainty,
            confidence=excluded.confidence,
            source_quote=COALESCE(excluded.source_quote, observations.source_quote),
            fetched_at=excluded.fetched_at
        """,
        {
            "id": obs.id,
            "entity_id": obs.entity_id,
            "channel": obs.channel.value,
            "region": obs.region,
            "release_date": obs.release_date.isoformat() if obs.release_date else None,
            "date_end": obs.date_end.isoformat() if obs.date_end else None,
            "precision": obs.precision.value,
            "price_minor": obs.price.amount_minor if obs.price else None,
            "price_currency": obs.price.currency if obs.price else None,
            "certainty": obs.certainty.value,
            "source_tier": int(obs.source_tier),
            "provider": obs.provider,
            "source_name": obs.source_name,
            "source_url": obs.source_url,
            "source_quote": obs.source_quote,
            "published_at": obs.published_at.isoformat() if obs.published_at else None,
            "confidence": obs.confidence,
            "fetched_at": (obs.fetched_at or datetime.now(UTC)).isoformat(),
        },
    )


def _row_to_entity(row: sqlite3.Row) -> Entity:
    return Entity(
        id=row["id"],
        title=row["title"],
        kind=MediaKind(row["kind"]),
        aliases=tuple(json.loads(row["aliases"])),
        external_ids=json.loads(row["external_ids"]),
        notion_page_id=row["notion_page_id"],
        notes=row["notes"],
        watch=bool(row["watch"]),
    )


def _row_to_observation(row: sqlite3.Row) -> ReleaseObservation:
    price = (
        Money(amount_minor=row["price_minor"], currency=row["price_currency"])
        if row["price_minor"] is not None and row["price_currency"]
        else None
    )
    return ReleaseObservation(
        entity_id=row["entity_id"],
        channel=ReleaseChannel(row["channel"]),
        region=row["region"],
        release_date=_parse_date(row["release_date"]),
        date_end=_parse_date(row["date_end"]),
        precision=DatePrecision(row["precision"]),
        price=price,
        certainty=Certainty(row["certainty"]),
        source_tier=SourceTier(row["source_tier"]),
        provider=row["provider"],
        source_name=row["source_name"],
        source_url=row["source_url"],
        source_quote=row["source_quote"],
        published_at=_parse_date(row["published_at"]),
        confidence=row["confidence"],
        fetched_at=datetime.fromisoformat(row["fetched_at"]),
    )


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None
