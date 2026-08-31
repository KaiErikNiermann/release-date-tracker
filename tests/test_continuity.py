"""Tests for franchise position across a numbering reset.

TMDB models most reboots as a separate show that restarts at season 1 — Marvel's Daredevil
(3 seasons) and Daredevil: Born Again are different ids, as are Dexter's four and Doctor Who's
three. The tracker has to follow that numbering, because `/tv/{id}/season/{n}` is what resolves
a season's date. So continuity is necessarily a *second* ordering, derived by walking the
`continues` links rather than stored anywhere.

Every failure mode yields no number plus a reason. A number that might be wrong is worse than
none, because the native number beside it is right and the reader cannot tell which to believe.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from release_tracker import views
from release_tracker.db import Database
from release_tracker.models import (
    Edge,
    Entity,
    MediaKind,
    Node,
    NodeKind,
    RelationKind,
    SourceTier,
    WorkRelation,
)

_NOW = datetime.fromisoformat("2026-08-31T00:00:00+00:00")


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    store = Database(tmp_path / "c.db")
    yield store
    store.close()


def _season(db: Database, show: str, n: int) -> Entity:
    """A tracked season, linked to its own show's series node."""
    entity = Entity.create(f"{show}: Season {n}", MediaKind.TV, season=n)
    db.upsert_entity(entity)
    node = Node.create(NodeKind.SERIES, show, owned=True)
    db.upsert_node(node)
    db.upsert_edge(
        Edge(
            src_id=entity.id,
            dst_id=node.id,
            relation=RelationKind.PART_OF_SERIES,
            ordinal=n,
            source_provider="user",
            source_tier=SourceTier.OFFICIAL,
            fetched_at=_NOW,
        )
    )
    return entity


def _continues(db: Database, show: str, prior: str, after: int | None) -> None:
    for name in (show, prior):
        db.upsert_node(Node.create(NodeKind.SERIES, name, owned=True))
    db.upsert_edge(
        Edge(
            src_id=Node.create(NodeKind.SERIES, show).id,
            dst_id=Node.create(NodeKind.SERIES, prior).id,
            relation=RelationKind.DERIVED_FROM,
            role=WorkRelation.CONTINUES,
            ordinal=after,
            source_provider="user",
            source_tier=SourceTier.OFFICIAL,
            fetched_at=_NOW,
        )
    )


def _position(db: Database, entity: Entity) -> views.FranchisePosition | None:
    return views.franchise_position(db, entity, entity.season)


# --- the working case ------------------------------------------------------------------------
def test_a_reset_season_carries_its_continuity_number(db: Database) -> None:
    """Born Again S1 is also Daredevil's 4th season."""
    born = _season(db, "Daredevil: Born Again", 1)
    _season(db, "Marvel's Daredevil", 3)
    _continues(db, "Daredevil: Born Again", "Marvel's Daredevil", 3)

    position = _position(db, born)
    assert position is not None
    assert position.season == 4
    assert position.root is not None and position.root.name == "Marvel's Daredevil"


def test_one_link_covers_every_season_of_the_continuation(db: Database) -> None:
    """The edge is between *series*, not works — otherwise the offset would have to be
    restated for each season, and every season but one would fail to find the chain."""
    seasons = [_season(db, "Daredevil: Born Again", n) for n in (1, 2)]
    _continues(db, "Daredevil: Born Again", "Marvel's Daredevil", 3)
    assert [_position(db, s).season for s in seasons] == [4, 5]  # type: ignore[union-attr]


def test_chains_compose(db: Database) -> None:
    """Doctor Who's shape: 2024 continues 2005 continues 1963. The offsets are per hop, so
    the design never has to adjudicate which continuity the reader means."""
    modern = _season(db, "Doctor Who (2024)", 1)
    _continues(db, "Doctor Who (2024)", "Doctor Who (2005)", 13)
    _continues(db, "Doctor Who (2005)", "Doctor Who (1963)", 26)
    position = _position(db, modern)
    assert position is not None
    assert position.season == 40
    assert position.root is not None and position.root.name == "Doctor Who (1963)"


# --- no chain is the common, correct answer ---------------------------------------------------
def test_a_show_that_never_reset_has_no_second_number(db: Database) -> None:
    """TMDB folds most revivals into the original show — Twin Peaks: The Return is S3 of one
    id, Samurai Jack's is S5 — and a second line there would invent a distinction the source
    does not make."""
    assert _position(db, _season(db, "Twin Peaks", 3)) is None


# --- every failure yields a reason, never a number ---------------------------------------------
def test_a_link_with_no_stated_offset_refuses_to_guess(db: Database) -> None:
    """Absence is not zero. Zero is the claim "nothing ran before", and reading a missing
    offset as zero would silently renumber a franchise from its own reboot."""
    born = _season(db, "Daredevil: Born Again", 1)
    _continues(db, "Daredevil: Born Again", "Marvel's Daredevil", None)

    position = _position(db, born)
    assert position is not None
    assert position.season is None
    assert "no season count" in position.reasons[0]


def test_a_loop_stops_rather_than_spinning(db: Database) -> None:
    show = _season(db, "A", 1)
    _continues(db, "A", "B", 1)
    _continues(db, "B", "A", 1)

    position = _position(db, show)
    assert position is not None
    assert position.season is None
    assert "loops" in position.reasons[0]


def test_a_fork_refuses_because_a_renumbering_has_one_predecessor(db: Database) -> None:
    show = _season(db, "A", 1)
    _continues(db, "A", "B", 1)
    _continues(db, "A", "C", 1)

    position = _position(db, show)
    assert position is not None
    assert position.season is None
    assert "one predecessor" in position.reasons[0]


# --- the ordinal's two meanings must not blur ---------------------------------------------------
def test_an_offset_on_any_other_relation_is_refused() -> None:
    """`ordinal` is a season number on PART_OF_SERIES and a season *count* on a continuation.
    Anywhere else it would leave the walk reading a number that was never about seasons."""
    with pytest.raises(ValueError, match="ordinal"):
        Edge(
            src_id="a",
            dst_id="b",
            relation=RelationKind.DERIVED_FROM,
            role=WorkRelation.SPINOFF,
            ordinal=3,
        )


def test_seasons_before_reads_only_a_continuation() -> None:
    spinoff = Edge(
        src_id="a", dst_id="b", relation=RelationKind.DERIVED_FROM, role=WorkRelation.SPINOFF
    )
    assert spinoff.seasons_before is None
