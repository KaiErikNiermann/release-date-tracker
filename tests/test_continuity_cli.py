"""Tests for the CLI face of "what carries season N".

The add screen has offered this since the Dexter work; the point here is that `rdt rd
--continuity` answers the *same* question the *same* way. Both go through
``TmdbSource.continuations``, and a parity feature whose two surfaces rank a franchise
differently is worse than one surface, so the ordering is asserted on both sides of the seam.

Nothing here touches the network: the search, the show shape and the casts are three
module-level names.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import cast

import pytest

from conftest import with_keys
from release_tracker import lookup as lookup_module
from release_tracker.config import Settings, get_settings
from release_tracker.db import Database
from release_tracker.lookup import ContinuityAnswer, continuity
from release_tracker.models import MediaKind, RelationKind, WorkRelation
from release_tracker.seasons import SeasonRef, ShowShape
from release_tracker.sources.base import Candidate
from release_tracker.sources.tmdb import TmdbSource

TODAY = date(2026, 9, 1)

_BASE = ("Dexter", "1405")
_HEIR = ("Dexter: New Blood", "131927")
_STRANGER = ("Dexter's Laboratory", "2609")

_CASTS = {
    _BASE[1]: frozenset({"a", "b", "c", "d", "e"}),
    _HEIR[1]: frozenset({"a", "b", "c", "d", "z"}),  # the measured four
    _STRANGER[1]: frozenset({"q"}),
}


def _cand(title: str, tmdb: str, year: int = 2006, score: float = 0.9) -> Candidate:
    return Candidate(
        source="tmdb", id_key="tmdb", canonical_id=tmdb, title=title, year=year, score=score
    )


@pytest.fixture
def settings() -> Settings:
    return with_keys(get_settings())


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ended show, an heir that shares its cast, and a stranger that shares nobody."""

    async def _search(*_a: object, **_k: object) -> list[Candidate]:
        return [_cand(*_BASE), _cand(*_HEIR, year=2021), _cand(*_STRANGER, year=1996)]

    async def _shape(_self: object, _c: object, _k: str, _id: str) -> ShowShape:
        seasons = tuple(SeasonRef(n, f"Season {n}", date(2005 + n, 1, 1), 12) for n in range(1, 9))
        return ShowShape("Dexter", "Ended", seasons, 8)

    async def _cast(_self: object, _c: object, _k: str, tmdb_id: str) -> frozenset[str]:
        return _CASTS.get(tmdb_id, frozenset())

    class _NoClient:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_a: object) -> None:
            return None

    monkeypatch.setattr(lookup_module, "_search_kind", _search)
    monkeypatch.setattr(lookup_module, "make_client", _NoClient)
    monkeypatch.setattr(TmdbSource, "tv_shape", _shape)
    monkeypatch.setattr(TmdbSource, "tv_cast", _cast)
    monkeypatch.setattr(lookup_module.utc_today, "__call__", lambda: TODAY, raising=False)


async def _ask(settings: Settings, season: int) -> ContinuityAnswer:
    return await continuity("dexter", settings, season)


# --- the answer -----------------------------------------------------------------------------
async def test_the_show_that_shares_the_most_cast_leads(world: None, settings: Settings) -> None:
    """The regression the ranking exists for: debut date and vote count both put a zero-vote
    stranger above New Blood, and cast overlap is the one signal that does not."""
    del world
    answer = await _ask(settings, 9)
    assert answer.mean is not None
    assert [s.title for s in answer.mean.offer] == [_HEIR[0]]
    assert answer.mean.offer[0].shared_cast == 4


async def test_a_stranger_is_dropped_and_said_rather_than_omitted(
    world: None, settings: Settings
) -> None:
    """The reader asked a question and deserves to know the pool was narrowed."""
    del world
    answer = await _ask(settings, 9)
    assert answer.mean is not None
    said = " ".join(answer.mean.reasons)
    assert _STRANGER[0] in said
    assert "share no cast" in said


async def test_the_asked_for_season_is_renumbered_onto_the_successor(
    world: None, settings: Settings
) -> None:
    """Season 9 of Dexter is season 1 of New Blood — the whole reason the offer is useful."""
    del world
    answer = await _ask(settings, 9)
    assert answer.mean is not None
    assert answer.mean.after == 8
    assert answer.mean.native(answer.mean.offer[0]) == 1


async def test_a_season_the_show_carries_offers_nothing(world: None, settings: Settings) -> None:
    """Offering to renumber season 3 onto another show would be actively wrong."""
    del world
    answer = await _ask(settings, 3)
    assert answer.mean is None
    assert answer.base is not None
    assert "lists season 3" in " ".join(answer.reasons)


async def test_the_verdicts_own_words_survive_to_the_surface(
    world: None, settings: Settings
) -> None:
    """`SeasonVerdict.reasons` is printed verbatim by every surface; this is one of them."""
    del world
    answer = await _ask(settings, 9)
    assert answer.mean is not None
    assert any("8 seasons" in r for r in answer.mean.verdict.reasons)


async def test_no_match_answers_without_a_base(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    async def _none(*_a: object, **_k: object) -> list[Candidate]:
        return []

    class _NoClient:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_a: object) -> None:
            return None

    monkeypatch.setattr(lookup_module, "_search_kind", _none)
    monkeypatch.setattr(lookup_module, "make_client", _NoClient)
    answer = await _ask(settings, 9)
    assert answer.base is None
    assert answer.mean is None
    assert "No confident TV match" in " ".join(answer.reasons)


async def test_a_weak_match_is_not_a_match(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """`MATCH_FLOOR` is the same line the capture path uses — below it we have not found it."""

    async def _weak(*_a: object, **_k: object) -> list[Candidate]:
        return [_cand(*_BASE, score=0.2)]

    class _NoClient:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_a: object) -> None:
            return None

    monkeypatch.setattr(lookup_module, "_search_kind", _weak)
    monkeypatch.setattr(lookup_module, "make_client", _NoClient)
    assert (await _ask(settings, 9)).base is None


async def test_the_json_carries_the_id_the_pick_needs(world: None, settings: Settings) -> None:
    """`--continuity --track --id tmdb=…` reads its argument off this; without the id the
    machine-readable answer cannot be acted on."""
    del world
    out = (await _ask(settings, 9)).to_dict()
    offer = out["offer"]
    assert isinstance(offer, list)
    (row,) = cast("list[dict[str, object]]", offer)
    assert row["id"] == _HEIR[1]
    assert row["lands_on_season"] == 1
    assert out["after"] == 8


# --- taking one -----------------------------------------------------------------------------
async def test_taking_a_successor_writes_the_work_and_the_edge(
    world: None, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the whole feature: the guess happens once and becomes a fact, so
    `franchise_position` answers the same question later with no inference in it."""
    del world
    from release_tracker import capture as capture_module
    from release_tracker.capture import take_continuation
    from release_tracker.models import Entity, Node, NodeKind
    from release_tracker.views import franchise_position

    db = Database(tmp_path / "c.db")

    async def _report(*_a: object, **_k: object) -> object:
        return object()

    async def _capture(inner: Database, *_a: object, **_k: object) -> Entity:
        entity = Entity.create("Dexter: New Blood: Season 1", MediaKind.TV, season=1)
        inner.upsert_entity(entity)
        # what enrichment would have written; the continuation hangs off the series node
        node = Node.create(NodeKind.SERIES, _HEIR[0], source="tmdb", source_id=_HEIR[1])
        inner.upsert_node(node)
        from release_tracker.edits import add_series

        add_series(inner, entity, _HEIR[0], source="tmdb", source_id=_HEIR[1], ordinal=1)
        return entity

    monkeypatch.setattr(capture_module, "report_for_candidate", _report)
    monkeypatch.setattr(capture_module, "capture_work", _capture)

    answer = await _ask(settings, 9)
    assert answer.mean is not None and answer.base is not None
    successor = answer.mean.offer[0]
    entity = await take_continuation(
        db,
        settings,
        kind=MediaKind.TV,
        base=answer.base,
        successor=successor,
        after=answer.mean.after,
        native=answer.mean.native(successor),
        client=object(),  # type: ignore[arg-type]
    )
    assert entity is not None

    (series,) = db.edges_from(entity.id, RelationKind.PART_OF_SERIES)
    (hop,) = [
        e
        for e in db.edges_from(series.dst_id, RelationKind.DERIVED_FROM)
        if e.role is WorkRelation.CONTINUES
    ]
    assert hop.ordinal == 8  # the base show's eight seasons
    # keyed the way enrichment keys it, or the chain orphans on the day Dexter is tracked
    assert hop.dst_id == f"series:tmdb:{_BASE[1]}"
    position = franchise_position(db, entity, 1)
    assert position is not None and position.season == 9
    db.close()
