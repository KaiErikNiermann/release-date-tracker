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
    ConsumptionState,
    Entity,
    MediaKind,
    Node,
    NodeKind,
    RelationKind,
    WorkRelation,
)
from release_tracker.sources.base import Candidate
from release_tracker.sources.wikidata import Lineage
from release_tracker.tech import CATEGORY_OVERRIDE_KEY, TechCategory, category_of
from release_tracker.titles import slice_title

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


# --- the freeform ladder ----------------------------------------------------------------
def _hit(title: str, kind: MediaKind, score: float) -> tuple[MediaKind, Candidate]:
    return kind, Candidate(
        source="tmdb", id_key="tmdb", canonical_id=title, title=title, score=score
    )


def test_an_explicit_kind_outranks_everything_read_off_the_results() -> None:
    """The user's own word is not inference and never loses to it."""
    draft = drafts.prefill(
        "Doomsday",
        kind_hint=MediaKind.GAME,
        hits=[_hit("Doomsday", MediaKind.MOVIE, 0.9)],
    )
    assert draft.kind is MediaKind.GAME
    assert any("kind:game" in reason for reason in draft.reasons)


def test_credible_matches_that_agree_set_the_kind() -> None:
    """The franchise case: the rest of the series is evidence about what you are adding."""
    draft = drafts.prefill(
        "Avengers Doomsday",
        hits=[
            _hit("Avengers Endgame", MediaKind.MOVIE, 0.72),
            _hit("Avengers Infinity War", MediaKind.MOVIE, 0.66),
        ],
    )
    assert draft.kind is MediaKind.MOVIE
    assert any("2 matches above" in reason for reason in draft.reasons)


def test_matches_below_the_floor_say_nothing() -> None:
    """Weak hits are noise. Reading a kind off them is exactly the wrong-prefill case, and
    they are also what makes the row render as "nothing matched"."""
    draft = drafts.prefill(
        "Some Unheard Of Album",
        hits=[_hit("Some Other Film", MediaKind.MOVIE, 0.2)],
    )
    assert draft.kind is MediaKind.OTHER
    assert draft.reasons == ()


def test_a_split_verdict_declines_to_guess() -> None:
    """A film and a game of the same name cancel out — better unclassified than confidently
    wrong, because the kind is baked into the entity id."""
    draft = drafts.prefill(
        "Tron",
        hits=[_hit("Tron", MediaKind.MOVIE, 0.9), _hit("Tron", MediaKind.GAME, 0.9)],
    )
    assert draft.kind is MediaKind.OTHER


def test_a_device_name_falls_to_tech_when_nothing_else_fired() -> None:
    draft = drafts.prefill("RTX 5090")
    assert draft.kind is MediaKind.TECH
    assert draft.category is not TechCategory.OTHER  # classified from the name


def test_a_device_name_still_loses_to_credible_matches() -> None:
    """The lexical guess is the weakest rung: real results outrank a regex over the name."""
    draft = drafts.prefill(
        "Steam Deck",
        hits=[_hit("Steam Deck: The Documentary", MediaKind.MOVIE, 0.8)],
    )
    assert draft.kind is MediaKind.MOVIE


def test_a_year_annotation_fills_the_date_and_nothing_else_does() -> None:
    """Dates are never inferred — a franchise says nothing about when a new entry ships."""
    assert drafts.prefill("Some Film", year_hint=2027).edtf == "2027"
    assert drafts.prefill("Some Film", hits=[_hit("Some Film 1", MediaKind.MOVIE, 0.9)]).edtf == ""


def test_a_season_annotation_implies_a_series_and_carries_the_coord() -> None:
    draft = drafts.prefill("Pluribus", season_hint=2)
    assert draft.kind is MediaKind.TV
    assert draft.season == 2


def test_a_season_coord_is_dropped_when_the_kind_is_not_tv() -> None:
    """Coords are a TV idea; carrying one onto a film would write a meaningless column."""
    assert drafts.prefill("Some Film", kind_hint=MediaKind.MOVIE, season_hint=2).season is None


async def test_the_freeform_ladder_folds_in_a_device_lineage(lineage: None) -> None:
    """The unannounced-device case is the top rung of the one ladder, not a feature beside
    it — so it comes back as a single draft carrying both."""
    del lineage
    draft = await drafts.infer_freeform(None, "Steam Deck 2")  # type: ignore[arg-type]
    assert draft.kind is MediaKind.TECH
    assert draft.predecessor == DECK
    assert draft.version is not None and draft.version.ordinal == 2


async def test_the_freeform_ladder_drafts_what_has_no_lineage_at_all(lineage: None) -> None:
    """Where the old path returned None and the screen shrugged, there is now still a row."""
    del lineage
    draft = await drafts.infer_freeform(None, "Some Unheard Of Album")  # type: ignore[arg-type]
    assert draft.kind is MediaKind.OTHER
    assert draft.version is None


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


async def test_a_hand_added_film_is_written_despite_having_no_canonical_id(
    db: Database, settings: Settings
) -> None:
    """`STRICT_CAPTURE_KINDS` refuses an unpinned film on the *automated* path, where a bad
    auto-match would mint junk stubs. Adding one deliberately is a different act, and the CLI
    has always allowed it — so the freeform door must not be narrower than `rdt add`."""
    entity = await drafts.commit(
        db,
        settings,
        Draft(title="Some Unannounced Film", kind=MediaKind.MOVIE),
        None,  # type: ignore[arg-type]
    )
    assert entity is not None
    assert db.get_entity(entity.id) is not None
    assert entity.kind is MediaKind.MOVIE
    assert entity.external_ids.get("tmdb") is None


async def test_a_hand_added_entry_starts_wanted(db: Database, settings: Settings) -> None:
    """Adding something by hand states an intent. Left unset it could never become
    `available` — `bucket_of` gates that on an active state — and the search path through
    `capture_work` already lands on exactly this."""
    entity = await drafts.commit(
        db,
        settings,
        Draft(title="Some Album", kind=MediaKind.MUSIC),
        None,  # type: ignore[arg-type]
    )
    assert entity is not None
    assert entity.consumption_state is ConsumptionState.WANT


async def test_a_season_draft_is_titled_and_coordinated_like_rdt_add(
    db: Database, settings: Settings
) -> None:
    """A season added here and one added from the CLI must land on the same row, not fork a
    near-duplicate — so it takes the same canonical title and the same structured coords.

    Asserted against `slice_title` rather than a literal, because that is the actual
    invariant: whatever the canonical format is, both paths must generate it. The part has
    to reach the title, or two cuts of one season collapse onto the same id.
    """
    entity = await drafts.commit(
        db,
        settings,
        Draft(title="Pluribus", kind=MediaKind.TV, season=2, part=1),
        None,  # type: ignore[arg-type]
    )
    assert entity is not None
    assert entity.title == slice_title("Pluribus", 2, 1)
    assert entity.title == "Pluribus: Season 2, Part 1"
    assert (entity.season, entity.part) == (2, 1)
    assert db.get_entity(entity.id) is not None


async def test_two_cuts_of_one_season_are_two_rows(db: Database, settings: Settings) -> None:
    """The collision: both used to title as "Show: Season 5" and the second overwrote the first."""
    made = [
        await drafts.commit(
            db,
            settings,
            Draft(title="Stranger Things", kind=MediaKind.TV, season=5, part=n),
            None,  # type: ignore[arg-type]
        )
        for n in (1, 2)
    ]
    assert all(e is not None for e in made)
    assert made[0].id != made[1].id  # type: ignore[union-attr]
    assert len([e for e in db.iter_entities() if e.season == 5]) == 2


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
