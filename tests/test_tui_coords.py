"""Tests for editing a work's series coordinates from the TUI.

Until now the edit screen composed only title / dates / who / where / what / notes — season,
part and series were CLI-only, so a split discovered *after* capture could not be recorded
where the user was actually looking. All three rows write through `edits.set_coords`, the same
call `rdt edit part` makes, because they are one fact and writing them separately would let
the entity coord and the series edge disagree about it.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from conftest import until
from release_tracker import views
from release_tracker.config import Settings, get_settings
from release_tracker.db import Database
from release_tracker.models import Entity, MediaKind
from release_tracker.tui.app import RdtApp
from release_tracker.tui.edit import CoordRow, EditScreen, Row, SeriesRow

TODAY = date(2026, 8, 31)


@pytest.fixture
def app(tmp_path: Path) -> RdtApp:
    settings: Settings = get_settings()
    return RdtApp(settings=settings, db=Database(tmp_path / "coords.db"), today=TODAY)


def _tracked(app: RdtApp, title: str, kind: MediaKind = MediaKind.TV) -> Entity:
    entity = Entity.create(title, kind)
    app.db.upsert_entity(entity)
    return entity


async def _open_edit(app: RdtApp, pilot: Any, entity: Entity) -> EditScreen:
    app.push_screen(EditScreen(entity, views.work_card(app.db, entity)))
    await pilot.pause()
    screen = app.screen
    assert isinstance(screen, EditScreen)
    return screen


def _row(screen: EditScreen, label: str) -> Row:
    return next(r for r in screen.query(Row) if r.label == label)


async def test_the_coord_rows_are_tv_only(app: RdtApp) -> None:
    """A season on a film is a mistake, not a coordinate — the same gate the add screen has."""
    async with app.run_test(size=(150, 45)) as pilot:
        screen = await _open_edit(app, pilot, _tracked(app, "Dune: Part Three", MediaKind.MOVIE))
        assert not [r for r in screen.query(Row) if isinstance(r, CoordRow | SeriesRow)]


async def test_setting_a_season_writes_the_entity_and_the_edge(app: RdtApp) -> None:
    async with app.run_test(size=(150, 45)) as pilot:
        entity = _tracked(app, "Pluribus: Season 2")
        screen = await _open_edit(app, pilot, entity)
        _row(screen, "series").field.value = "Pluribus"
        screen._commit(_row(screen, "series"))  # pyright: ignore[reportPrivateUsage]
        _row(screen, "season").field.value = "2"
        screen._commit(_row(screen, "season"))  # pyright: ignore[reportPrivateUsage]
        await pilot.pause()

        stored = app.db.get_entity(entity.id)
        assert stored is not None and stored.season == 2


async def test_a_cut_and_its_word_round_trip(app: RdtApp) -> None:
    """The label is what the cut was sold under; blank reads as "Part"."""
    async with app.run_test(size=(150, 45)) as pilot:
        entity = _tracked(app, "Stranger Things: Season 5, Volume 1")
        screen = await _open_edit(app, pilot, entity)
        for label, value in (("series", "Stranger Things"), ("season", "5"), ("part", "1")):
            _row(screen, label).field.value = value
            screen._commit(_row(screen, label))  # pyright: ignore[reportPrivateUsage]
        _row(screen, "part label").field.value = "Volume"
        screen._commit(_row(screen, "part label"))  # pyright: ignore[reportPrivateUsage]
        await pilot.pause()

        stored = app.db.get_entity(entity.id)
        assert stored is not None
        assert (stored.season, stored.part, stored.part_label) == (5, 1, "Volume")


async def test_a_coord_with_no_series_says_so_instead_of_writing(app: RdtApp) -> None:
    """`set_coords` has nothing to attach the season to, and the row reverts rather than
    half-writing it onto the entity alone."""
    async with app.run_test(size=(150, 45)) as pilot:
        entity = _tracked(app, "Orphan: Season 2")
        screen = await _open_edit(app, pilot, entity)
        row = _row(screen, "season")
        row.field.value = "2"
        screen._commit(row)  # pyright: ignore[reportPrivateUsage]
        await until(
            pilot,
            lambda: "series" in str(screen.query_one("#edit-status").render()),
            "the refusal",
        )
        stored = app.db.get_entity(entity.id)
        assert stored is not None and stored.season is None
