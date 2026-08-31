"""Tests for region-scoped where-edges.

The load-bearing guarantee is negative: adding ``region`` to ``Edge`` must not move the id of
any edge already in a database. Region joins the dedup key for ``AVAILABLE_ON`` alone, where
"on Netflix in the US" and "on Netflix in Japan" are two facts; folding it in everywhere would
rehash every credit, genre and series edge and duplicate the lot on the next enrich.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from release_tracker.db import Database
from release_tracker.models import (
    CreditRole,
    Edge,
    Entity,
    MediaKind,
    Node,
    NodeKind,
    RelationKind,
    SourceTier,
)

_NOW = datetime(2026, 8, 31, tzinfo=UTC)


def _edge(relation: RelationKind, **kw: object) -> Edge:
    return Edge(src_id="work-1", dst_id="node-1", relation=relation, **kw)  # type: ignore[arg-type]


# --- the dedup key ------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("relation", "role"),
    [
        (RelationKind.CREDITED_ON, CreditRole.DIRECTOR),
        (RelationKind.EXHIBITS, None),
        (RelationKind.PART_OF_SERIES, None),
    ],
)
def test_region_never_touches_a_non_availability_key(
    relation: RelationKind, role: CreditRole | None
) -> None:
    """A stray region on any other relation must not fork the edge — the migration hazard."""
    assert _edge(relation, role=role).id == _edge(relation, role=role, region="US").id


def test_two_markets_are_two_availability_edges() -> None:
    """The whole point: one platform in two countries is two facts, not one."""
    us = _edge(RelationKind.AVAILABLE_ON, region="US")
    jp = _edge(RelationKind.AVAILABLE_ON, region="JP")
    assert us.id != jp.id


def test_an_unscoped_availability_keeps_its_pre_region_id() -> None:
    """A region-less AVAILABLE_ON hashes as it always did, so nothing pre-existing moves."""
    assert _edge(RelationKind.AVAILABLE_ON).id == _edge(RelationKind.AVAILABLE_ON, region=None).id


# --- storage round-trip + migration -------------------------------------------------------
def _seed(db: Database) -> Entity:
    entity = Entity.create("Yellowjackets", MediaKind.TV)
    db.upsert_entity(entity)
    for name in ("Netflix", "Showtime"):
        db.upsert_node(Node.create(NodeKind.PLATFORM, name))
    return entity


def test_region_round_trips(tmp_path: Path) -> None:
    db = Database(tmp_path / "r.db")
    entity = _seed(db)
    node = Node.create(NodeKind.PLATFORM, "Netflix")
    for region in ("US", "JP"):
        db.upsert_edge(
            Edge(
                src_id=entity.id,
                dst_id=node.id,
                relation=RelationKind.AVAILABLE_ON,
                region=region,
                source_provider="tmdb",
                source_tier=SourceTier.AGGREGATOR,
                fetched_at=_NOW,
            )
        )
    edges = db.edges_from(entity.id, RelationKind.AVAILABLE_ON)
    assert sorted(e.region or "" for e in edges) == ["JP", "US"]
    db.close()


def test_migration_adds_the_column_to_a_pre_region_db(tmp_path: Path) -> None:
    """Opening an older db must add `region` and leave every existing row in place."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE edges (
            id TEXT PRIMARY KEY, src_id TEXT NOT NULL, dst_id TEXT NOT NULL,
            relation TEXT NOT NULL, role TEXT, source_provider TEXT NOT NULL,
            source_url TEXT, source_tier INTEGER NOT NULL, confidence REAL NOT NULL,
            owned INTEGER NOT NULL DEFAULT 0, fetched_at TEXT NOT NULL
        );
        INSERT INTO edges VALUES ('e1','w','n','available_on',NULL,'tmdb',NULL,3,0.85,0,
                                  '2026-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    db = Database(path)
    cols = {r[1] for r in db._conn.execute("PRAGMA table_info(edges)")}  # pyright: ignore[reportPrivateUsage]
    assert {"region", "ordinal", "part"} <= cols
    survivor = db.edges_from("w", RelationKind.AVAILABLE_ON)
    assert len(survivor) == 1
    assert survivor[0].region is None  # unscoped, not "WW" — we do not know where
    db.close()


# --- delete_edges -------------------------------------------------------------------------
def test_delete_edges_clears_the_named_providers_and_spares_owned(tmp_path: Path) -> None:
    """A refetch clears what it is about to re-answer; a user's own assertion is not that."""
    db = Database(tmp_path / "d.db")
    entity = _seed(db)
    netflix = Node.create(NodeKind.PLATFORM, "Netflix")
    showtime = Node.create(NodeKind.PLATFORM, "Showtime")
    common = {
        "src_id": entity.id,
        "relation": RelationKind.AVAILABLE_ON,
        "source_tier": SourceTier.AGGREGATOR,
        "fetched_at": _NOW,
    }
    db.upsert_edge(Edge(dst_id=netflix.id, region="US", source_provider="tmdb", **common))  # type: ignore[arg-type]
    db.upsert_edge(Edge(dst_id=netflix.id, region="JP", source_provider="tmdb", **common))  # type: ignore[arg-type]
    db.upsert_edge(Edge(dst_id=showtime.id, source_provider="user", owned=True, **common))  # type: ignore[arg-type]

    assert db.delete_edges(entity.id, RelationKind.AVAILABLE_ON, ("tmdb", "model")) == 2
    left = db.edges_from(entity.id, RelationKind.AVAILABLE_ON)
    assert [e.source_provider for e in left] == ["user"]
    db.close()


def test_delete_edges_with_no_providers_is_a_no_op(tmp_path: Path) -> None:
    """An unconfigured source names nobody — it must not empty the where-graph."""
    db = Database(tmp_path / "n.db")
    entity = _seed(db)
    db.upsert_edge(
        Edge(
            src_id=entity.id,
            dst_id=Node.create(NodeKind.PLATFORM, "Netflix").id,
            relation=RelationKind.AVAILABLE_ON,
            region="US",
            source_provider="tmdb",
            source_tier=SourceTier.AGGREGATOR,
            fetched_at=_NOW,
        )
    )
    assert db.delete_edges(entity.id, RelationKind.AVAILABLE_ON, ()) == 0
    assert len(db.edges_from(entity.id, RelationKind.AVAILABLE_ON)) == 1
    db.close()


# --- the view: one line per platform, markets unioned -------------------------------------
def _availability(
    db: Database, entity: Entity, node: Node, *, region: str | None, provider: str, tier: SourceTier
) -> None:
    db.upsert_node(node)
    db.upsert_edge(
        Edge(
            src_id=entity.id,
            dst_id=node.id,
            relation=RelationKind.AVAILABLE_ON,
            region=region,
            source_provider=provider,
            source_tier=tier,
            fetched_at=_NOW,
        )
    )


def test_platform_lines_fold_markets_onto_one_line(tmp_path: Path) -> None:
    """Netflix in three countries is one place to watch, not three rows in the column."""
    from release_tracker import views

    db = Database(tmp_path / "v.db")
    entity = Entity.create("Yellowjackets", MediaKind.TV)
    db.upsert_entity(entity)
    netflix = Node.create(NodeKind.PLATFORM, "Netflix")
    for region in ("US", "CA", "JP"):
        _availability(
            db, entity, netflix, region=region, provider="tmdb", tier=SourceTier.AGGREGATOR
        )

    (line,) = views.work_card(db, entity).platforms
    assert line.name == "Netflix"
    assert line.regions == ("CA", "JP", "US")
    assert not line.predicted
    db.close()


def test_one_sourced_edge_settles_a_platform_that_was_also_predicted(tmp_path: Path) -> None:
    """`predicted` means every edge behind it is a guess — a real answer outranks the guess."""
    from release_tracker import views

    db = Database(tmp_path / "p.db")
    entity = Entity.create("Some Show", MediaKind.TV)
    db.upsert_entity(entity)
    hulu = Node.create(NodeKind.PLATFORM, "Hulu")
    _availability(db, entity, hulu, region=None, provider="model", tier=SourceTier.MODEL)
    _availability(db, entity, hulu, region="US", provider="tmdb", tier=SourceTier.AGGREGATOR)

    (line,) = views.work_card(db, entity).platforms
    assert not line.predicted
    assert line.providers == ("model", "tmdb")
    db.close()


def test_an_unscoped_platform_is_reachable_from_anywhere() -> None:
    """ "We don't know where" must not render as "nowhere you can get to"."""
    from release_tracker.views import PlatformLine

    network = PlatformLine("Showtime", predicted=False)
    assert network.live_in({"DE"})
    assert PlatformLine("Hulu", predicted=False, regions=("US",)).live_in({"DE"}) is False


# --- display order ------------------------------------------------------------------------
def test_a_reachable_offer_outranks_an_unscoped_network() -> None:
    """The column answers "where can I watch this" — an offer beats an attribution."""
    from release_tracker.views import PlatformLine

    network = PlatformLine("Showtime", predicted=False)
    offer = PlatformLine("Netflix", predicted=False, regions=("US",))
    assert sorted([network, offer], key=lambda p: p.rank({"US"}))[0].name == "Netflix"


def test_a_platform_you_cannot_reach_sorts_behind_an_unscoped_one() -> None:
    """Live only in markets the reader has no access to is the least useful thing to show."""
    from release_tracker.views import PlatformLine

    network = PlatformLine("Showtime", predicted=False)
    elsewhere = PlatformLine("U-NEXT", predicted=False, regions=("JP",))
    ordered = sorted([elsewhere, network], key=lambda p: p.rank({"DE"}))
    assert [p.name for p in ordered] == ["Showtime", "U-NEXT"]


def test_a_prediction_never_displaces_a_fact() -> None:
    """The column truncates, so a guess sorting above a sourced answer would hide it."""
    from release_tracker.views import PlatformLine

    guess = PlatformLine("Hulu", predicted=True, regions=("US",))
    fact = PlatformLine("U-NEXT", predicted=False, regions=("JP",))
    assert sorted([guess, fact], key=lambda p: p.rank({"US"}))[0].name == "U-NEXT"
