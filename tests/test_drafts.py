"""Tests for the draft an entry passes through before it is written.

The load-bearing claim is that inference prefills and never decides: a synthetic entry
inherits what survives a generation (that it is tech, roughly what sort) and nothing that
doesn't (dates, the predecessor's ids). The rest is about the corrections the review screen
exists to collect actually reaching the database.

Nothing here touches the network — the lineage lookup is one module-level name.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from release_tracker import drafts
from release_tracker.config import Settings, get_settings
from release_tracker.db import Database
from release_tracker.drafts import PREDECESSOR_KEY, Draft
from release_tracker.models import (
    Entity,
    MediaKind,
    Node,
    NodeKind,
    RelationKind,
    WorkRelation,
)
from release_tracker.sources.wikidata import Lineage
from release_tracker.tech import CATEGORY_OVERRIDE_KEY, TechCategory, category_of

DECK = Lineage(
    qid="Q107542665",
    label="Steam Deck",
    released=date(2022, 2, 25),
    brand=None,
    instance_of="handheld gaming PC model series",
)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "drafts.db")


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def lineage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every stem resolves to the Steam Deck, with no client and no network."""

    async def _find(*_a: object, **_k: object) -> Lineage:
        return DECK

    monkeypatch.setattr(drafts, "find_lineage", _find)


@pytest.fixture
def no_lineage(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _find(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(drafts, "find_lineage", _find)


# --- inference --------------------------------------------------------------------------
async def test_a_name_with_no_generation_marker_yields_no_draft(lineage: None) -> None:
    """Without a marker there is no family to look up, so an empty form would be a lie
    about how much we knew. The caller falls back to plain "no matches"."""
    del lineage
    assert await drafts.infer_synthetic(None, "Steam Frames") is None  # type: ignore[arg-type]


async def test_a_successor_name_is_prefilled_from_its_family(lineage: None) -> None:
    del lineage
    draft = await drafts.infer_synthetic(None, "Steam Deck 2")  # type: ignore[arg-type]
    assert draft is not None
    assert draft.synthetic
    assert draft.title == "Steam Deck 2"
    assert draft.kind is MediaKind.TECH
    assert draft.version is not None and draft.version.ordinal == 2
    assert draft.predecessor == DECK


async def test_a_family_nobody_has_heard_of_still_drafts(no_lineage: None) -> None:
    """A miss costs a prefilled field, not the entry — this is exactly the case the
    tracker exists for, so refusing to draft would be the wrong end of the trade."""
    del no_lineage
    draft = await drafts.infer_synthetic(None, "Steam Frames 2")  # type: ignore[arg-type]
    assert draft is not None
    assert draft.predecessor is None
    assert draft.kind is MediaKind.TECH


# --- committing -------------------------------------------------------------------------
async def test_committing_writes_the_entity_and_its_node(db: Database, settings: Settings) -> None:
    entity = await drafts.commit(
        db,
        settings,
        Draft(title="Steam Deck 2", kind=MediaKind.TECH),
        None,  # type: ignore[arg-type]
    )
    assert entity is not None
    assert db.get_entity(entity.id) is not None
    assert db.get_node(entity.id) is not None


async def test_the_predecessors_ids_are_never_inherited(db: Database, settings: Settings) -> None:
    """The whole point of the split. A successor is a *different* device, so carrying over
    the family's wikidata id would pin a claim that is simply false — and would make the
    entry look resolved when it is the opposite of resolved."""
    entity = await drafts.commit(
        db,
        settings,
        Draft(title="Steam Deck 2", kind=MediaKind.TECH, predecessor=DECK),
        None,  # type: ignore[arg-type]
    )
    assert entity is not None
    assert "wikidata" not in entity.external_ids
    # ...but where it came from is still recorded, under a key of its own.
    assert entity.external_ids[PREDECESSOR_KEY] == DECK.qid


async def test_no_date_is_invented(db: Database, settings: Settings) -> None:
    """Successor cadence is far too noisy to guess from. An empty date field stays empty."""
    entity = await drafts.commit(
        db,
        settings,
        Draft(title="Steam Deck 2", kind=MediaKind.TECH, predecessor=DECK),
        None,  # type: ignore[arg-type]
    )
    assert entity is not None
    assert db.observation_dates(entity.id) == []


async def test_a_date_typed_during_review_is_written(db: Database, settings: Settings) -> None:
    entity = await drafts.commit(
        db,
        settings,
        Draft(title="Steam Deck 2", kind=MediaKind.TECH, edtf="2026-Q4"),
        None,  # type: ignore[arg-type]
    )
    assert entity is not None
    assert db.observation_dates(entity.id) == [date(2026, 10, 1)]


async def test_a_bad_date_does_not_lose_the_entry(db: Database, settings: Settings) -> None:
    """The row is already written by then; dropping it over a typo would be the worse of
    the two failures, and the date is fixable from the card."""
    entity = await drafts.commit(
        db,
        settings,
        Draft(title="Steam Deck 2", kind=MediaKind.TECH, edtf="whenever"),
        None,  # type: ignore[arg-type]
    )
    assert entity is not None
    assert db.get_entity(entity.id) is not None
    assert db.observation_dates(entity.id) == []


# --- the category correction ------------------------------------------------------------
async def test_a_corrected_category_survives_the_write(db: Database, settings: Settings) -> None:
    """The reason the review screen exists. "Steam Deck 2" classifies as a console off its
    name; if the user says it is a laptop, every later read has to agree — the category is
    otherwise recomputed from the title on each one."""
    entity = await drafts.commit(
        db,
        settings,
        Draft(title="Steam Deck 2", kind=MediaKind.TECH, category=TechCategory.LAPTOP),
        None,  # type: ignore[arg-type]
    )
    assert entity is not None
    assert category_of(entity) is TechCategory.LAPTOP


async def test_an_uncorrected_category_is_not_stored(db: Database, settings: Settings) -> None:
    """Only a disagreement is worth a row: storing the derived value would freeze it, so a
    later improvement to the classifier could never reach entries that never needed one."""
    entity = await drafts.commit(
        db,
        settings,
        Draft(title="Steam Deck 2", kind=MediaKind.TECH, category=TechCategory.CONSOLE),
        None,  # type: ignore[arg-type]
    )
    assert entity is not None
    assert CATEGORY_OVERRIDE_KEY not in entity.external_ids
    assert category_of(entity) is TechCategory.CONSOLE


# --- lineage edges ----------------------------------------------------------------------
async def test_the_successor_edge_is_written_when_the_family_is_tracked(
    db: Database, settings: Settings
) -> None:
    prior = Entity.create("Steam Deck", MediaKind.TECH, external_ids={"wikidata": DECK.qid})
    db.upsert_entity(prior)
    db.upsert_node(Node(id=prior.id, node_kind=NodeKind.WORK, name=prior.title, owned=True))

    entity = await drafts.commit(
        db,
        settings,
        Draft(title="Steam Deck 2", kind=MediaKind.TECH, predecessor=DECK),
        None,  # type: ignore[arg-type]
    )
    assert entity is not None
    (edge,) = db.edges_from(entity.id, RelationKind.DERIVED_FROM)
    assert edge.dst_id == prior.id
    assert edge.role is WorkRelation.SUCCESSOR


async def test_no_edge_is_written_when_the_family_is_not_tracked(
    db: Database, settings: Settings
) -> None:
    """Hanging the edge would mean creating a node for a device that already shipped and
    the user never asked to track, purely as an anchor."""
    entity = await drafts.commit(
        db,
        settings,
        Draft(title="Steam Deck 2", kind=MediaKind.TECH, predecessor=DECK),
        None,  # type: ignore[arg-type]
    )
    assert entity is not None
    assert db.edges_from(entity.id, RelationKind.DERIVED_FROM) == []
    assert db.get_entity(Entity.make_id("Steam Deck", MediaKind.TECH)) is None
