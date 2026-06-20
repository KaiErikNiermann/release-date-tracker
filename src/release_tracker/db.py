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
    ArtistLink,
    Certainty,
    Condition,
    ConsumptionState,
    CreditRole,
    DatePrecision,
    DescriptorKind,
    Edge,
    Entity,
    LinkTier,
    MediaKind,
    Money,
    Node,
    NodeKind,
    RelationKind,
    ReleaseChannel,
    ReleaseObservation,
    SocialPlatform,
    SourceTier,
    WorkRelation,
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
    consumption_state TEXT NOT NULL DEFAULT 'unset',
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
    fetched_at      TEXT NOT NULL,
    contingencies   TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_obs_entity ON observations(entity_id);
CREATE INDEX IF NOT EXISTS idx_obs_lookup ON observations(entity_id, channel, region);

-- the media graph: who/where/what. Additive; existing dbs gain these on open.
CREATE TABLE IF NOT EXISTS nodes (
    id              TEXT PRIMARY KEY,
    node_kind       TEXT NOT NULL,
    name            TEXT NOT NULL,
    descriptor_kind TEXT,
    owned           INTEGER NOT NULL DEFAULT 0,
    followed        INTEGER NOT NULL DEFAULT 0,
    external_ids    TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    id              TEXT PRIMARY KEY,
    src_id          TEXT NOT NULL,
    dst_id          TEXT NOT NULL,
    relation        TEXT NOT NULL,
    role            TEXT,
    ordinal         INTEGER,
    part            INTEGER,
    source_provider TEXT NOT NULL,
    source_url      TEXT,
    source_tier     INTEGER NOT NULL,
    confidence      REAL NOT NULL,
    owned           INTEGER NOT NULL DEFAULT 0,
    fetched_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(node_kind);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_id, relation);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_id, relation);

-- freeform, timestamped per-media notes (a log; distinct from Entity.notes)
CREATE TABLE IF NOT EXISTS media_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id   TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_entity ON media_notes(entity_id);

-- artist-radar: a followed creator's links (free/paid/aux) + latest surfaced content
CREATE TABLE IF NOT EXISTS artist_links (
    node_id        TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    platform       TEXT NOT NULL,
    tier           TEXT NOT NULL,
    url            TEXT NOT NULL,
    handle         TEXT,
    latest_title   TEXT,
    latest_url     TEXT,
    last_post_at   TEXT,
    fetched_at     TEXT,
    PRIMARY KEY (node_id, platform)
);
CREATE INDEX IF NOT EXISTS idx_links_node ON artist_links(node_id);

-- external blockers: one row per CONDITION node, its three-valued resolution.
-- A work BLOCKED_BY a condition is unavailable until the condition resolves.
CREATE TABLE IF NOT EXISTS conditions (
    node_id        TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    status         TEXT NOT NULL,            -- 'resolved' | 'pending' | 'never'
    resolve_date   TEXT,                     -- ISO date iff status='resolved'
    precision      TEXT,                     -- DatePrecision for an EDTF-authored date
    note           TEXT,
    updated_at     TEXT NOT NULL
);
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
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Additive column migrations for dbs created before a column existed."""
        edge_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(edges)")}
        if "ordinal" not in edge_cols:
            self._conn.execute("ALTER TABLE edges ADD COLUMN ordinal INTEGER")
        if "part" not in edge_cols:
            self._conn.execute("ALTER TABLE edges ADD COLUMN part INTEGER")
        entity_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(entities)")}
        if "consumption_state" not in entity_cols:
            self._conn.execute(
                "ALTER TABLE entities ADD COLUMN consumption_state TEXT NOT NULL DEFAULT 'unset'"
            )
        node_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(nodes)")}
        if "followed" not in node_cols:
            self._conn.execute("ALTER TABLE nodes ADD COLUMN followed INTEGER NOT NULL DEFAULT 0")
        obs_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(observations)")}
        if "contingencies" not in obs_cols:
            self._conn.execute(
                "ALTER TABLE observations ADD COLUMN contingencies TEXT NOT NULL DEFAULT '{}'"
            )

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
                    notion_page_id, notes, watch, consumption_state, created_at, updated_at)
                VALUES (:id, :title, :kind, :aliases, :external_ids,
                    :notion_page_id, :notes, :watch, :consumption_state, :now, :now)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    kind=excluded.kind,
                    aliases=excluded.aliases,
                    -- merge external_ids: keep existing, add new (handled in app layer)
                    external_ids=excluded.external_ids,
                    notion_page_id=COALESCE(excluded.notion_page_id, entities.notion_page_id),
                    notes=COALESCE(excluded.notes, entities.notes),
                    watch=excluded.watch,
                    -- a stateless pull (unset) must not clobber a known state; a real
                    -- state (Notion seed / CLI) does update.
                    consumption_state=CASE
                        WHEN excluded.consumption_state = 'unset' THEN entities.consumption_state
                        ELSE excluded.consumption_state END,
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
                    "consumption_state": entity.consumption_state.value,
                    "now": now,
                },
            )

    def get_entity(self, entity_id: str) -> Entity | None:
        row = self._conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
        return _row_to_entity(row) if row else None

    def delete_entity(self, entity_id: str) -> None:
        """Remove an entity and everything keyed to it (observations cascade; node+edges too).

        Used when re-keying an unresolved capture once its real kind is detected.
        """
        with self._tx() as conn:
            conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))  # cascades observations
            conn.execute("DELETE FROM nodes WHERE id = ?", (entity_id,))
            conn.execute("DELETE FROM edges WHERE src_id = ? OR dst_id = ?", (entity_id, entity_id))

    def set_consumption_state(self, entity_id: str, state: ConsumptionState) -> bool:
        """Directly set an entity's watch/play state (can also clear to UNSET)."""
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE entities SET consumption_state = ?, updated_at = ? WHERE id = ?",
                (state.value, datetime.now(UTC).isoformat(), entity_id),
            )
            return cur.rowcount > 0

    # -- media notes (freeform, timestamped) -------------------------------
    def add_note(self, entity_id: str, body: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO media_notes (entity_id, body, created_at) VALUES (?, ?, ?)",
                (entity_id, body, datetime.now(UTC).isoformat()),
            )

    def iter_notes(self, entity_id: str) -> list[tuple[date, str]]:
        """All notes for an entity, newest first, as (created_date, body)."""
        rows = self._conn.execute(
            "SELECT created_at, body FROM media_notes WHERE entity_id = ? ORDER BY created_at DESC",
            (entity_id,),
        )
        return [(datetime.fromisoformat(r[0]).date(), r[1]) for r in rows]

    def note_counts(self) -> dict[str, int]:
        """entity_id -> number of notes, for cheaply flagging which works have notes."""
        rows = self._conn.execute("SELECT entity_id, count(*) FROM media_notes GROUP BY entity_id")
        return {r[0]: r[1] for r in rows}

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

    def delete_channel_observations(
        self, entity_id: str, provider: str, channel: ReleaseChannel
    ) -> int:
        """Drop an entity's rows for one (provider, channel) — re-authoring a hand date.

        Scoped to a single channel so a manual theatrical date isn't lost when its
        digital sibling is re-authored.
        """
        with self._tx() as conn:
            cur = conn.execute(
                "DELETE FROM observations WHERE entity_id = ? AND provider = ? AND channel = ?",
                (entity_id, provider, channel.value),
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

    # -- graph: nodes ------------------------------------------------------
    def upsert_node(self, node: Node) -> None:
        """Idempotent on id. ``owned`` is sticky-true: re-resolving never un-owns."""
        now = datetime.now(UTC).isoformat()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO nodes (id, node_kind, name, descriptor_kind, owned, followed,
                    external_ids, created_at, updated_at)
                VALUES (:id, :node_kind, :name, :descriptor_kind, :owned, :followed,
                    :external_ids, :now, :now)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    descriptor_kind=COALESCE(excluded.descriptor_kind, nodes.descriptor_kind),
                    owned=MAX(nodes.owned, excluded.owned),
                    followed=MAX(nodes.followed, excluded.followed),
                    external_ids=excluded.external_ids,
                    updated_at=:now
                """,
                {
                    "id": node.id,
                    "node_kind": node.node_kind.value,
                    "name": node.name,
                    "descriptor_kind": node.descriptor_kind.value if node.descriptor_kind else None,
                    "owned": int(node.owned),
                    "followed": int(node.followed),
                    "external_ids": json.dumps(node.external_ids),
                    "now": now,
                },
            )

    def get_node(self, node_id: str) -> Node | None:
        row = self._conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return _row_to_node(row) if row else None

    def get_nodes(self, node_ids: Iterable[str]) -> dict[str, Node]:
        """Batch-fetch nodes by id (for rendering an edge set without N queries)."""
        ids = list(node_ids)
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            f"SELECT * FROM nodes WHERE id IN ({placeholders})",  # noqa: S608 - ids are placeholders
            ids,
        )
        return {row["id"]: _row_to_node(row) for row in rows}

    def find_nodes(self, query: str, *, node_kind: NodeKind | None = None) -> list[Node]:
        """Resolve a node reference: exact id, else case-insensitive name substring."""
        exact = self.get_node(query)
        if exact is not None:
            return [exact]
        sql = "SELECT * FROM nodes WHERE lower(name) LIKE ?"
        params: list[str] = [f"%{query.lower()}%"]
        if node_kind is not None:
            sql += " AND node_kind = ?"
            params.append(node_kind.value)
        sql += " ORDER BY owned DESC, name"
        return [_row_to_node(r) for r in self._conn.execute(sql, params)]

    # -- artist radar (followed creators + their links) --------------------
    def set_followed(self, node_id: str, followed: bool) -> bool:
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE nodes SET followed = ?, updated_at = ? WHERE id = ?",
                (int(followed), datetime.now(UTC).isoformat(), node_id),
            )
            return cur.rowcount > 0

    def followed_artists(self) -> list[Node]:
        rows = self._conn.execute(
            "SELECT * FROM nodes WHERE followed = 1 ORDER BY name",
        )
        return [_row_to_node(r) for r in rows]

    def upsert_artist_link(self, link: ArtistLink) -> None:
        """Add/update a creator's platform link. Activity fields are merged, not wiped."""
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO artist_links (node_id, platform, tier, url, handle,
                    latest_title, latest_url, last_post_at, fetched_at)
                VALUES (:node_id, :platform, :tier, :url, :handle,
                    :latest_title, :latest_url, :last_post_at, :fetched_at)
                ON CONFLICT(node_id, platform) DO UPDATE SET
                    tier=excluded.tier,
                    url=excluded.url,
                    handle=COALESCE(excluded.handle, artist_links.handle),
                    latest_title=COALESCE(excluded.latest_title, artist_links.latest_title),
                    latest_url=COALESCE(excluded.latest_url, artist_links.latest_url),
                    last_post_at=COALESCE(excluded.last_post_at, artist_links.last_post_at),
                    fetched_at=COALESCE(excluded.fetched_at, artist_links.fetched_at)
                """,
                {
                    "node_id": link.node_id,
                    "platform": link.platform.value,
                    "tier": link.tier.value,
                    "url": link.url,
                    "handle": link.handle,
                    "latest_title": link.latest_title,
                    "latest_url": link.latest_url,
                    "last_post_at": link.last_post_at.isoformat() if link.last_post_at else None,
                    "fetched_at": link.fetched_at.isoformat() if link.fetched_at else None,
                },
            )

    def update_link_activity(
        self,
        node_id: str,
        platform: SocialPlatform,
        *,
        title: str | None,
        url: str | None,
        posted_at: date | None,
        fetched_at: datetime,
    ) -> None:
        """Record the latest fetched content for one link (always stamps fetched_at)."""
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE artist_links SET latest_title = ?, latest_url = ?,
                    last_post_at = ?, fetched_at = ? WHERE node_id = ? AND platform = ?
                """,
                (
                    title,
                    url,
                    posted_at.isoformat() if posted_at else None,
                    fetched_at.isoformat(),
                    node_id,
                    platform.value,
                ),
            )

    def iter_artist_links(self, node_id: str) -> list[ArtistLink]:
        rows = self._conn.execute(
            "SELECT * FROM artist_links WHERE node_id = ? ORDER BY tier, platform",
            (node_id,),
        )
        return [_row_to_link(r) for r in rows]

    def delete_artist_link(self, node_id: str, platform: SocialPlatform) -> bool:
        with self._tx() as conn:
            cur = conn.execute(
                "DELETE FROM artist_links WHERE node_id = ? AND platform = ?",
                (node_id, platform.value),
            )
            return cur.rowcount > 0

    # -- conditions (external blockers backing CONDITION nodes) ------------
    def upsert_condition(self, cond: Condition) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO conditions (node_id, status, resolve_date, precision, note, updated_at)
                VALUES (:node_id, :status, :resolve_date, :precision, :note, :updated_at)
                ON CONFLICT(node_id) DO UPDATE SET
                    status=excluded.status,
                    resolve_date=excluded.resolve_date,
                    precision=excluded.precision,
                    note=COALESCE(excluded.note, conditions.note),
                    updated_at=excluded.updated_at
                """,
                {
                    "node_id": cond.node_id,
                    "status": cond.status,
                    "resolve_date": cond.resolve_date.isoformat() if cond.resolve_date else None,
                    "precision": cond.precision.value,
                    "note": cond.note,
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            )

    def get_conditions(self, node_ids: Iterable[str]) -> dict[str, Condition]:
        ids = list(node_ids)
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        query = f"SELECT * FROM conditions WHERE node_id IN ({placeholders})"  # noqa: S608
        rows = self._conn.execute(query, ids)
        return {r["node_id"]: _row_to_condition(r) for r in rows}

    # -- graph: edges ------------------------------------------------------
    def upsert_edge(self, edge: Edge) -> None:
        with self._tx() as conn:
            _insert_edge(conn, edge)

    def upsert_edges(self, edges: Iterable[Edge]) -> int:
        count = 0
        with self._tx() as conn:
            for edge in edges:
                _insert_edge(conn, edge)
                count += 1
        return count

    def delete_edge(self, edge_id: str) -> bool:
        """Remove one edge by id (e.g. an uncredit / un-relate). True if a row went."""
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM edges WHERE id = ?", (edge_id,))
            return cur.rowcount > 0

    def edges_from(self, src_id: str, relation: RelationKind | None = None) -> list[Edge]:
        return self._edges("src_id", src_id, relation)

    def edges_to(self, dst_id: str, relation: RelationKind | None = None) -> list[Edge]:
        return self._edges("dst_id", dst_id, relation)

    def _edges(self, column: str, value: str, relation: RelationKind | None) -> list[Edge]:
        sql = f"SELECT * FROM edges WHERE {column} = ?"  # noqa: S608 - column is a literal
        params: list[str] = [value]
        if relation is not None:
            sql += " AND relation = ?"
            params.append(relation.value)
        sql += " ORDER BY confidence DESC"
        return [_row_to_edge(r) for r in self._conn.execute(sql, params)]


# --- row mapping ----------------------------------------------------------
def _insert_observation(conn: sqlite3.Connection, obs: ReleaseObservation) -> None:
    conn.execute(
        """
        INSERT INTO observations (id, entity_id, channel, region, release_date,
            date_end, precision, price_minor, price_currency, certainty,
            source_tier, provider, source_name, source_url, source_quote,
            published_at, confidence, fetched_at, contingencies)
        VALUES (:id, :entity_id, :channel, :region, :release_date, :date_end,
            :precision, :price_minor, :price_currency, :certainty, :source_tier,
            :provider, :source_name, :source_url, :source_quote, :published_at,
            :confidence, :fetched_at, :contingencies)
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
            "contingencies": json.dumps(obs.contingencies),
        },
    )


def _insert_edge(conn: sqlite3.Connection, edge: Edge) -> None:
    conn.execute(
        """
        INSERT INTO edges (id, src_id, dst_id, relation, role, ordinal, part, source_provider,
            source_url, source_tier, confidence, owned, fetched_at)
        VALUES (:id, :src_id, :dst_id, :relation, :role, :ordinal, :part, :source_provider,
            :source_url, :source_tier, :confidence, :owned, :fetched_at)
        ON CONFLICT(id) DO UPDATE SET
            ordinal=COALESCE(excluded.ordinal, edges.ordinal),
            part=COALESCE(excluded.part, edges.part),
            source_url=COALESCE(excluded.source_url, edges.source_url),
            source_tier=excluded.source_tier,
            confidence=excluded.confidence,
            owned=MAX(edges.owned, excluded.owned),
            fetched_at=excluded.fetched_at
        """,
        {
            "id": edge.id,
            "src_id": edge.src_id,
            "dst_id": edge.dst_id,
            "relation": edge.relation.value,
            "role": edge.role.value if edge.role else None,
            "ordinal": edge.ordinal,
            "part": edge.part,
            "source_provider": edge.source_provider,
            "source_url": edge.source_url,
            "source_tier": int(edge.source_tier),
            "confidence": edge.confidence,
            "owned": int(edge.owned),
            "fetched_at": (edge.fetched_at or datetime.now(UTC)).isoformat(),
        },
    )


def _row_to_node(row: sqlite3.Row) -> Node:
    return Node(
        id=row["id"],
        node_kind=NodeKind(row["node_kind"]),
        name=row["name"],
        descriptor_kind=DescriptorKind(row["descriptor_kind"]) if row["descriptor_kind"] else None,
        owned=bool(row["owned"]),
        followed=bool(row["followed"]),
        external_ids=json.loads(row["external_ids"]),
    )


def _row_to_link(row: sqlite3.Row) -> ArtistLink:
    return ArtistLink(
        node_id=row["node_id"],
        platform=SocialPlatform(row["platform"]),
        tier=LinkTier(row["tier"]),
        url=row["url"],
        handle=row["handle"],
        latest_title=row["latest_title"],
        latest_url=row["latest_url"],
        last_post_at=date.fromisoformat(row["last_post_at"]) if row["last_post_at"] else None,
        fetched_at=datetime.fromisoformat(row["fetched_at"]) if row["fetched_at"] else None,
    )


def _parse_role(relation: RelationKind, raw: str | None) -> CreditRole | WorkRelation | None:
    """The role column means CreditRole for credits, WorkRelation for derived-from."""
    if raw is None:
        return None
    if relation is RelationKind.DERIVED_FROM:
        return WorkRelation(raw)
    if relation is RelationKind.CREDITED_ON:
        return CreditRole(raw)
    return None


def _row_to_edge(row: sqlite3.Row) -> Edge:
    relation = RelationKind(row["relation"])
    return Edge(
        src_id=row["src_id"],
        dst_id=row["dst_id"],
        relation=relation,
        role=_parse_role(relation, row["role"]),
        ordinal=row["ordinal"],
        part=row["part"],
        source_provider=row["source_provider"],
        source_url=row["source_url"],
        source_tier=SourceTier(row["source_tier"]),
        confidence=row["confidence"],
        owned=bool(row["owned"]),
        fetched_at=datetime.fromisoformat(row["fetched_at"]),
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
        consumption_state=ConsumptionState(row["consumption_state"]),
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
        contingencies=json.loads(row["contingencies"]) if row["contingencies"] else {},
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


def _row_to_condition(row: sqlite3.Row) -> Condition:
    return Condition(
        node_id=row["node_id"],
        status=row["status"],
        resolve_date=_parse_date(row["resolve_date"]),
        precision=DatePrecision(row["precision"]) if row["precision"] else DatePrecision.TBA,
        note=row["note"],
    )


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None
