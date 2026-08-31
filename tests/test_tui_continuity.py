"""Tests for the offer made when a finished show does not carry the season you asked for.

Dexter's ninth season is New Blood's first; Marvel's Daredevil's fourth is Born Again's first.
The season exists — on another id — so the add screen offers the shows that might carry it,
ranked by shared cast, and taking one records *why* so the same question never needs guessing
at again.

Nothing is ever forced: the first row always adds the season exactly as typed.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import OptionList

from conftest import until, with_keys
from release_tracker import views
from release_tracker.config import get_settings
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
from release_tracker.seasons import SeasonRef, ShowShape
from release_tracker.sources.base import Candidate
from release_tracker.tui import add as add_module
from release_tracker.tui.add import AddScreen, AnywayRow, SuccessorRow, resolve
from release_tracker.tui.app import RdtApp

TODAY = date(2026, 8, 31)

_BASE = ("Marvel's Daredevil", "61889")
_HEIR = ("Daredevil: Born Again", "202555")
_STRANGER = ("Kick Buttowski: Suburban Daredevil", "17572")


@pytest.fixture
def app(tmp_path: Path) -> RdtApp:
    return RdtApp(settings=with_keys(get_settings()), db=Database(tmp_path / "c.db"), today=TODAY)


def _cand(title: str, tmdb: str, year: int = 2015) -> Candidate:
    return Candidate(
        source="tmdb", id_key="tmdb", canonical_id=tmdb, title=title, year=year, score=0.9
    )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, frozenset[str]]:
    """A finished base show, an heir that shares its cast, and a stranger that shares nobody."""
    casts = {
        _BASE[1]: frozenset({"a", "b", "c", "d", "e"}),
        _HEIR[1]: frozenset({"a", "b", "c", "d", "z"}),  # 4 shared, the measured Dexter shape
        _STRANGER[1]: frozenset({"q"}),
    }

    async def _search(*_a: object, **_k: object) -> Any:
        return [
            (MediaKind.TV, _cand(*_BASE)),
            (MediaKind.TV, _cand(*_HEIR, year=2025)),
            (MediaKind.TV, _cand(*_STRANGER, year=2010)),
        ]

    async def _shape(_self: object, _c: object, _k: str, tmdb_id: str) -> Any:
        seasons = tuple(SeasonRef(n, f"Season {n}", date(2015 + n, 1, 1), 13) for n in (1, 2, 3))
        return ShowShape("Marvel's Daredevil", "Ended", seasons, 3)

    async def _cast(_self: object, _c: object, _k: str, tmdb_id: str) -> frozenset[str]:
        return casts.get(tmdb_id, frozenset())

    async def _no_client(_self: RdtApp) -> None: ...

    def _quiet(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(add_module, "capture_candidates", _search)
    monkeypatch.setattr(add_module.TmdbSource, "tv_shape", _shape)
    monkeypatch.setattr(add_module.TmdbSource, "tv_cast", _cast)
    monkeypatch.setattr(RdtApp, "http", _no_client)
    monkeypatch.setattr(add_module.log, "info", _quiet)
    monkeypatch.setattr(add_module.log, "warning", _quiet)
    return casts


async def _ask(app: RdtApp, pilot: Any, text: str) -> AddScreen:
    app.open_add("")
    await pilot.pause()
    screen = app.screen
    assert isinstance(screen, AddScreen)
    screen.query_one("#add-query", add_module.Input).value = text
    screen.search(resolve(text))
    await until(pilot, lambda: not screen.query_one("#candidates", OptionList).loading, "search")
    return screen


async def _open_offer(app: RdtApp, pilot: Any) -> AddScreen:
    screen = await _ask(app, pilot, "daredevil season:4")
    options = screen.query_one("#candidates", OptionList)
    options.highlighted = 0  # the base show
    options.focus()
    await pilot.press("enter")
    await until(
        pilot,
        lambda: screen._mean is not None,  # pyright: ignore[reportPrivateUsage]
        "the offer",
    )
    return screen


# --- the offer -------------------------------------------------------------------------------
async def test_a_finished_show_offers_what_carries_the_season(
    app: RdtApp, world: dict[str, frozenset[str]]
) -> None:
    """Marvel's Daredevil has three seasons and is Ended, so the fourth is somewhere else."""
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_offer(app, pilot)
        picker = screen._mean  # pyright: ignore[reportPrivateUsage]
        assert picker is not None
        rows = picker.rows
        assert isinstance(rows[0], AnywayRow)  # never removed
        heirs = [r for r in rows if isinstance(r, SuccessorRow)]
        assert [h.successor.title for h in heirs] == [_HEIR[0]]  # the stranger shares nobody
        assert heirs[0].native == 1  # season 4 of the continuity is its first
        assert app.db.get_entity("x") is None  # and nothing has been written


async def test_the_stranger_is_named_not_just_dropped(
    app: RdtApp, world: dict[str, frozenset[str]]
) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_offer(app, pilot)
        status = str(screen.query_one("#add-status", add_module.Static).content)
    assert "Kick Buttowski" in status
    assert "share no cast" in status


async def test_adding_anyway_still_writes_the_season_as_typed(
    app: RdtApp, world: dict[str, frozenset[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tracker knows what TMDB lists, not what is true — the reader may be right."""
    seen: list[int | None] = []

    async def _report(*_a: object, **_k: object) -> object:
        return object()

    async def _capture(*_a: object, **kw: object) -> Entity:
        seen.append(kw.get("season"))  # type: ignore[arg-type]
        return Entity.create("Daredevil: Season 4", MediaKind.TV, season=4)

    monkeypatch.setattr(add_module, "report_for_candidate", _report)
    monkeypatch.setattr(add_module, "capture_work", _capture)

    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_offer(app, pilot)
        options = screen.query_one("#candidates", OptionList)
        options.highlighted = 0
        await pilot.press("enter")
        await until(pilot, lambda: bool(seen), "the capture")
    assert seen == [4]


async def test_taking_the_successor_captures_it_and_records_the_continuation(
    app: RdtApp, world: dict[str, frozenset[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: the guess happens once, then it is a fact in the graph."""
    made: list[Entity] = []

    async def _report(*_a: object, **_k: object) -> object:
        return object()

    async def _capture(db: Database, *_a: object, **kw: object) -> Entity:
        entity = Entity.create("Daredevil: Born Again: Season 1", MediaKind.TV, season=1)
        db.upsert_entity(entity)
        # enrichment would have made this; the continuation hangs off it
        node = Node.create(NodeKind.SERIES, _HEIR[0], source="tmdb", source_id=_HEIR[1])
        db.upsert_node(node)
        db.upsert_edge(
            Edge(
                src_id=entity.id,
                dst_id=node.id,
                relation=RelationKind.PART_OF_SERIES,
                ordinal=1,
                source_provider="tmdb",
                source_tier=SourceTier.AGGREGATOR,
            )
        )
        made.append(entity)
        return entity

    monkeypatch.setattr(add_module, "report_for_candidate", _report)
    monkeypatch.setattr(add_module, "capture_work", _capture)
    monkeypatch.setattr(add_module.edits.log, "info", lambda *_a, **_k: None)  # type: ignore[misc]

    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_offer(app, pilot)
        options = screen.query_one("#candidates", OptionList)
        options.highlighted = 1  # the successor
        await pilot.press("enter")
        await until(pilot, lambda: bool(made), "the capture")
        await pilot.pause()

        entity = made[0]
        series = app.db.edges_from(entity.id, RelationKind.PART_OF_SERIES)
        (hop,) = [
            e
            for e in app.db.edges_from(series[0].dst_id, RelationKind.DERIVED_FROM)
            if e.role is WorkRelation.CONTINUES
        ]
        assert hop.ordinal == 3  # the base show's three seasons
        # the predecessor is keyed the way enrichment keys it, or the chain would orphan
        assert hop.dst_id == f"series:tmdb:{_BASE[1]}"
        # and the derived answer now needs no inference at all
        position = views.franchise_position(app.db, entity, 1)
        assert position is not None and position.season == 4


async def test_escape_walks_back_to_the_candidates(
    app: RdtApp, world: dict[str, frozenset[str]]
) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_offer(app, pilot)
        screen.action_back()
        await pilot.pause()
        assert screen._mean is None  # pyright: ignore[reportPrivateUsage]
        assert isinstance(app.screen, AddScreen)
