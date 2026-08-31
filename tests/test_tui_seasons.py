"""Tests for season coordinates reaching every add path.

The coordinate itself has existed end to end for a while (`Entity.season`, `rdt add --season`,
TMDB's `/tv/{id}/season/{n}`). What was missing in the TUI was every path *to* it: `season:2`
worked on `enter` but was dropped by `e`, a season typed on the review form for a searched hit
was discarded on commit, and none of it was ever shown, so a working path looked like a missing
feature. These pin the paths, not the coordinate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input, OptionList, Static

from conftest import until, with_keys
from release_tracker import drafts
from release_tracker.config import get_settings
from release_tracker.db import Database
from release_tracker.models import Entity, MediaKind
from release_tracker.sources.base import Candidate
from release_tracker.sources.tmdb import SeasonRef
from release_tracker.tui import add as add_module
from release_tracker.tui.add import AddScreen, resolve
from release_tracker.tui.app import RdtApp
from release_tracker.tui.draft import DraftScreen

TODAY = __import__("datetime").date(2026, 8, 31)


def _show(title: str = "Yellowjackets") -> list[tuple[MediaKind, Candidate]]:
    return [
        (
            MediaKind.TV,
            Candidate(
                source="tmdb",
                id_key="tmdb",
                canonical_id="117488",
                title=title,
                year=2021,
                score=0.98,
            ),
        )
    ]


@pytest.fixture
def app(tmp_path: Path) -> RdtApp:
    # keys present: the subject here is *routing* (does a season reach the capture), and an
    # unconfigured TMDB would correctly report what is missing instead of listing seasons.
    return RdtApp(settings=with_keys(get_settings()), db=Database(tmp_path / "s.db"), today=TODAY)


@pytest.fixture
def tv_hit(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A single TV hit, plus a record of the text the sources were actually asked for."""
    asked: list[str] = []

    async def _search(_client: object, text: str, *_a: object, **_k: object) -> Any:
        asked.append(text)
        return _show()

    async def _no_client(_self: RdtApp) -> None: ...

    monkeypatch.setattr(add_module, "capture_candidates", _search)
    monkeypatch.setattr(RdtApp, "http", _no_client)
    return asked


async def _open(app: RdtApp, pilot: Any, text: str) -> AddScreen:
    app.open_add("")
    await pilot.pause()
    screen = app.screen
    assert isinstance(screen, AddScreen)
    screen.query_one("#add-query", Input).value = text
    screen.search(resolve(text))
    await until(
        pilot,
        lambda: not screen.query_one("#candidates", OptionList).loading,
        f"the search for {text!r} to settle",
    )
    return screen


# --- resolving the bar --------------------------------------------------------------------
def test_an_explicit_season_term_wins_over_the_text() -> None:
    """The user's own word beats anything read out of the prose."""
    typed = resolve("yellowjackets season 3 season:2")
    assert typed.season == 2
    assert not typed.reasons  # nothing was inferred, so there is nothing to justify


def test_a_typed_season_phrase_becomes_a_coordinate_and_leaves_the_search_text() -> None:
    """Both halves matter: the coord, and a search string TMDB can actually match."""
    typed = resolve("yellowjackets season 2")
    assert (typed.season, typed.text) == (2, "yellowjackets")
    assert typed.reasons  # an inference must be attributable


def test_the_memo_key_ignores_the_season_words() -> None:
    """Keying on the raw text would re-search on every keystroke of " season 2"."""
    assert resolve("yellowjackets").key == resolve("yellowjackets season 2").key


def test_part_survives_the_browse_to_add_handoff() -> None:
    """`Query.external` carried kind/year/season but silently dropped `part:`."""
    from release_tracker import query

    assert "part:2" in query.parse("stranger things season:5 part:2 is:upcoming").external


# --- the search itself --------------------------------------------------------------------
async def test_the_stem_is_what_reaches_the_sources(app: RdtApp, tv_hit: list[str]) -> None:
    """ "yellowjackets season 2" went to TMDB's search verbatim, which is a worse query."""
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(app, pilot, "yellowjackets season 2")
    assert tv_hit == ["yellowjackets"]


async def test_an_inferred_season_is_visible_on_the_row_and_in_the_status(
    app: RdtApp, tv_hit: list[str]
) -> None:
    """A working path nobody can see is indistinguishable from a missing one."""
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open(app, pilot, "yellowjackets season 2")
        status = str(screen.query_one("#add-status", Static).content)
        row = str(screen.query_one("#candidates", OptionList).get_option_at_index(0).prompt)
    assert "season:2" in status
    assert "Season 2" in row


async def test_a_season_is_not_advertised_on_a_film(
    app: RdtApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A season on a movie is a mistake the capture drops — the row must not claim otherwise."""

    async def _search(*_a: object, **_k: object) -> Any:
        return [
            (
                MediaKind.MOVIE,
                Candidate(
                    source="tmdb",
                    id_key="tmdb",
                    canonical_id="1",
                    title="Dune",
                    year=2026,
                    score=0.9,
                ),
            )
        ]

    async def _no_client(_self: RdtApp) -> None: ...

    monkeypatch.setattr(add_module, "capture_candidates", _search)
    monkeypatch.setattr(RdtApp, "http", _no_client)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open(app, pilot, "dune season 2")
        row = str(screen.query_one("#candidates", OptionList).get_option_at_index(0).prompt)
    assert "Season 2" not in row


# --- `e` (review) used to drop what `enter` honoured ---------------------------------------
async def test_review_on_a_hit_carries_the_season(app: RdtApp, tv_hit: list[str]) -> None:
    """The bug: `enter` honoured `season:2`, `e` silently threw it away."""
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open(app, pilot, "yellowjackets season:2")
        screen.query_one("#candidates", OptionList).highlighted = 0
        screen.action_review()
        await pilot.pause()
        review = app.screen
        assert isinstance(review, DraftScreen)
        assert review.draft.season == 2
        assert review.draft.candidate is not None  # a real hit, not the freeform row


async def test_review_on_a_hit_carries_an_inferred_season_too(
    app: RdtApp, tv_hit: list[str]
) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open(app, pilot, "yellowjackets season 2")
        screen.query_one("#candidates", OptionList).highlighted = 0
        screen.action_review()
        await pilot.pause()
        review = app.screen
        assert isinstance(review, DraftScreen)
        assert review.draft.season == 2
        assert review.draft.reasons  # and says why, on the provenance line


def test_for_candidate_kind_gates_the_coords() -> None:
    """A season on a film must not survive into a hidden form field."""
    cand = _show()[0][1]
    film = drafts.for_candidate("Dune", MediaKind.MOVIE, cand, season=2, part=1)
    assert (film.season, film.part) == (None, None)
    show = drafts.for_candidate("Yellowjackets", MediaKind.TV, cand, season=2, part=1)
    assert (show.season, show.part) == (2, 1)


# --- commit used to drop the coords on the candidate branch --------------------------------
async def test_committing_a_reviewed_candidate_passes_its_coords_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The form gathered season/part for a searched hit and `commit` ignored both."""
    seen: dict[str, object] = {}

    async def _report(*_a: object, **kw: object) -> object:
        seen["report_season"] = kw.get("season")
        return object()

    async def _capture(*_a: object, **kw: object) -> Entity:
        seen["capture_season"] = kw.get("season")
        seen["capture_part"] = kw.get("part")
        return Entity.create("Yellowjackets: Season 2", MediaKind.TV, season=2)

    monkeypatch.setattr(drafts, "report_for_candidate", _report)
    monkeypatch.setattr(drafts, "capture_work", _capture)

    draft = drafts.for_candidate("Yellowjackets", MediaKind.TV, _show()[0][1], season=2, part=1)
    db = Database(tmp_path / "c.db")
    await drafts.commit(db, get_settings(), draft, client=None)  # type: ignore[arg-type]
    db.close()

    assert seen == {"report_season": 2, "capture_season": 2, "capture_part": 1}


# --- the season picker ---------------------------------------------------------------------
def _seasons(*numbers: int, specials: bool = True) -> tuple[SeasonRef, ...]:
    import datetime as _dt

    rows = [SeasonRef(n, f"Season {n}", _dt.date(2020 + n, 1, 1), 10) for n in numbers]
    if specials:
        rows.append(SeasonRef(0, "Specials", _dt.date(2022, 7, 19), 3))
    return tuple(rows)


@pytest.fixture
def listed(monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[SeasonRef, ...]]:
    """Answer `tv_seasons` from a table keyed by tmdb id, with no network."""
    table: dict[str, tuple[SeasonRef, ...]] = {}

    async def _tv_seasons(_self: object, _c: object, _k: str, tmdb_id: str) -> Any:
        return table.get(tmdb_id, ())

    monkeypatch.setattr(add_module.TmdbSource, "tv_seasons", _tv_seasons)
    return table


async def _press_s(app: RdtApp, pilot: Any, screen: AddScreen) -> None:
    screen.query_one("#candidates", OptionList).highlighted = 0
    screen.action_seasons()
    await until(pilot, lambda: not screen.query_one("#candidates", OptionList).loading, "seasons")
    await pilot.pause()


def _rows(screen: AddScreen) -> list[str]:
    options = screen.query_one("#candidates", OptionList)
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


async def test_s_lists_the_seasons_under_a_whole_show_row(
    app: RdtApp, tv_hit: list[str], listed: dict[str, tuple[SeasonRef, ...]]
) -> None:
    """The show stays first and selectable — picking a season is opt-in, never forced."""
    listed["117488"] = _seasons(1, 2, 3, 4)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open(app, pilot, "yellowjackets")
        await _press_s(app, pilot, screen)
        rows = _rows(screen)
    assert "whole show" in rows[0]
    assert [r for r in rows if "Season 2" in r]
    assert not [r for r in rows if "Specials" in r]  # season 0 is reachable via `season:0`


async def test_a_limited_series_is_not_pushed_into_a_season(
    app: RdtApp, tv_hit: list[str], listed: dict[str, tuple[SeasonRef, ...]]
) -> None:
    """One season means no choice to make; a one-row picker reads as a broken keybinding."""
    listed["117488"] = _seasons(1, specials=False)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open(app, pilot, "yellowjackets")
        await _press_s(app, pilot, screen)
        status = str(screen.query_one("#add-status", Static).content)
        assert screen._seasons is None  # pyright: ignore[reportPrivateUsage]
    assert "one season" in status


async def test_s_declines_on_a_film(app: RdtApp, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _search(*_a: object, **_k: object) -> Any:
        return [
            (
                MediaKind.MOVIE,
                Candidate(
                    source="tmdb",
                    id_key="tmdb",
                    canonical_id="1",
                    title="Dune",
                    year=2026,
                    score=0.9,
                ),
            )
        ]

    async def _no_client(_self: RdtApp) -> None: ...

    monkeypatch.setattr(add_module, "capture_candidates", _search)
    monkeypatch.setattr(RdtApp, "http", _no_client)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open(app, pilot, "dune")
        screen.query_one("#candidates", OptionList).highlighted = 0
        screen.action_seasons()
        await pilot.pause()
        assert screen._seasons is None  # pyright: ignore[reportPrivateUsage]
        assert "not a series" in str(screen.query_one("#add-status", Static).content)


async def test_picking_a_season_row_captures_that_season(
    app: RdtApp,
    tv_hit: list[str],
    listed: dict[str, tuple[SeasonRef, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the picker: row N adds season N, row 0 adds the show."""
    listed["117488"] = _seasons(1, 2, 3, 4)
    seen: list[int | None] = []

    async def _report(*_a: object, **kw: object) -> object:
        return object()

    async def _capture(*_a: object, **kw: object) -> Entity:
        seen.append(kw.get("season"))  # type: ignore[arg-type]
        return Entity.create("Yellowjackets: Season 2", MediaKind.TV, season=2)

    monkeypatch.setattr(add_module, "report_for_candidate", _report)
    monkeypatch.setattr(add_module, "capture_work", _capture)

    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open(app, pilot, "yellowjackets")
        await _press_s(app, pilot, screen)
        screen.query_one("#candidates", OptionList).highlighted = 2  # row 0 = show, 1 = S1
        await pilot.press("enter")
        await until(pilot, lambda: bool(seen), "the capture to land")
    assert seen == [2]


async def test_escape_leaves_the_seasons_before_it_leaves_the_screen(
    app: RdtApp, tv_hit: list[str], listed: dict[str, tuple[SeasonRef, ...]]
) -> None:
    """Escape walks out one level at a time, as it already did for the bar."""
    listed["117488"] = _seasons(1, 2, 3)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open(app, pilot, "yellowjackets")
        await _press_s(app, pilot, screen)
        screen.action_back()
        await pilot.pause()
        assert screen._seasons is None  # pyright: ignore[reportPrivateUsage]
        assert isinstance(app.screen, AddScreen)  # back to the hits, not out of the palette
