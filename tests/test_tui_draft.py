"""The review-before-add door, and the synthetic row that can only go through it.

Two behaviours are worth pinning. A device nothing has heard of still gets somewhere to go —
that is the case a release tracker exists for, and "no matches" is the wrong answer to
"Steam Deck 2". And a synthetic entry can never be added blind: everything about it is
inferred, so selecting it opens the form rather than writing a row.

Nothing here touches the network — the screen reaches it through module-level names.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input, OptionList, Static

from conftest import until, with_keys
from release_tracker import drafts
from release_tracker.config import Settings, get_settings
from release_tracker.db import Database
from release_tracker.drafts import Draft
from release_tracker.models import Entity, MediaKind
from release_tracker.sources.base import Candidate
from release_tracker.sources.wikidata import Lineage
from release_tracker.tech import TechCategory
from release_tracker.titles import split_version
from release_tracker.tui import add as add_module
from release_tracker.tui.add import AddScreen
from release_tracker.tui.app import RdtApp
from release_tracker.tui.draft import DraftScreen

TODAY = date(2026, 8, 27)

DECK = Lineage(
    qid="Q107542665",
    label="Steam Deck",
    released=date(2022, 2, 25),
    instance_of="handheld gaming PC model series",
)


def _synthetic(title: str = "Steam Deck 2", *, predecessor: Lineage | None = DECK) -> Draft:
    return Draft(
        title=title,
        kind=MediaKind.TECH,
        category=TechCategory.CONSOLE,
        version=split_version(title)[1],
        predecessor=predecessor,
    )


@pytest.fixture
def app(tmp_path: Path) -> RdtApp:
    settings: Settings = get_settings()
    return RdtApp(settings=settings, db=Database(tmp_path / "draft.db"), today=TODAY)


@pytest.fixture
def nothing_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every external search comes back empty, with no client and no network."""

    async def _search(*_a: object, **_k: object) -> list[tuple[MediaKind, Candidate]]:
        return []

    async def _no_client(_self: RdtApp) -> None:
        """The fakes ignore the client, so the screen never needs a real one."""

    monkeypatch.setattr(add_module, "capture_candidates", _search)
    monkeypatch.setattr(RdtApp, "http", _no_client)


@pytest.fixture
def offers_draft(monkeypatch: pytest.MonkeyPatch) -> Draft:
    draft = _synthetic()

    async def _infer(*_a: object, **_k: object) -> Draft:
        return draft

    monkeypatch.setattr(drafts, "infer_synthetic", _infer)
    return draft


async def _search_for(pilot: Any, screen: AddScreen, text: str) -> None:
    screen.query_one("#add-query", Input).value = text
    screen.search(text, None)  # skip the debounce; the worker is what we exercise
    await until(
        pilot,
        lambda: not screen.query_one("#candidates", OptionList).loading,
        f"the search for {text!r} to settle",
    )


async def _open_add(app: RdtApp, pilot: Any) -> AddScreen:
    app.open_add("")
    await pilot.pause()
    screen = app.screen
    assert isinstance(screen, AddScreen)
    return screen


def _options(screen: AddScreen) -> list[str]:
    options = screen.query_one("#candidates", OptionList)
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


# --- the synthetic row ------------------------------------------------------------------
async def test_an_unannounced_device_is_offered_as_a_new_entry(
    app: RdtApp, nothing_found: None, offers_draft: Draft
) -> None:
    """Nothing matched, but the name reads like hardware with a generation on it — which is
    precisely the thing a release tracker is for. A row beats a shrug."""
    del nothing_found
    async with app.run_test(size=(150, 40)) as pilot:
        screen = await _open_add(app, pilot)
        await _search_for(pilot, screen, "Steam Deck 2")
        (row,) = _options(screen)
        assert offers_draft.title in row
        assert "track it as new" in row
        assert "follows Steam Deck" in row


async def test_a_film_that_matched_nothing_still_says_so(app: RdtApp, nothing_found: None) -> None:
    """An empty search for a film usually *does* mean the title was wrong, and the freeform row
    must not bury that — it is offered underneath the warning, not instead of it.

    (This replaces a test that asserted no row at all. Offering one is the deliberate change;
    keeping the warning is the part of that test's concern which still holds.)
    """
    del nothing_found
    app.settings = with_keys(app.settings)  # else the screen reports the missing keys instead
    async with app.run_test(size=(150, 40)) as pilot:
        screen = await _open_add(app, pilot)
        await _search_for(pilot, screen, "Dune 2")
        assert "no matches" in str(screen.query_one("#add-status", Static).content)
        (row,) = _options(screen)
        assert "Dune 2" in row
        # nothing said it was a device or anything else, so it stays deliberately unclassified
        assert "as other" in row


async def test_a_synthetic_row_opens_review_rather_than_adding(
    app: RdtApp, nothing_found: None, offers_draft: Draft
) -> None:
    """It can never be added blind: every field on it is inferred."""
    del nothing_found, offers_draft
    async with app.run_test(size=(150, 40)) as pilot:
        screen = await _open_add(app, pilot)
        await _search_for(pilot, screen, "Steam Deck 2")
        await pilot.press("down")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, DraftScreen), "the review screen")
        assert app.db.get_entity(Entity.make_id("Steam Deck 2", MediaKind.TECH)) is None


# --- the review form --------------------------------------------------------------------
async def test_the_form_shows_what_the_prefill_was_read_off(app: RdtApp) -> None:
    async with app.run_test(size=(150, 40)) as pilot:
        app.push_screen(DraftScreen(_synthetic()))
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DraftScreen)
        lineage = str(screen.query_one("#draft-lineage", Static).content)
        assert "Steam Deck" in lineage
        assert "2022-02-25" in lineage


async def test_a_draft_with_no_lineage_says_so(app: RdtApp) -> None:
    """Silence would read as confidence. If nothing was found, the fields are guesses and
    the form has to say which."""
    async with app.run_test(size=(150, 40)) as pilot:
        app.push_screen(DraftScreen(_synthetic(predecessor=None)))
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DraftScreen)
        assert "No lineage" in str(screen.query_one("#draft-lineage", Static).content)


async def test_category_is_hidden_for_a_kind_that_has_no_categories(app: RdtApp) -> None:
    async with app.run_test(size=(150, 40)) as pilot:
        app.push_screen(DraftScreen(_synthetic()))
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DraftScreen)
        assert screen.picker("category").display
        kind = screen.picker("kind")
        while kind.cycle.value != MediaKind.MOVIE.value:
            kind.cycle.action_step(1)
        await pilot.pause()
        assert not screen.picker("category").display


async def test_edits_made_in_the_form_are_what_gets_committed(
    app: RdtApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole reason the screen exists — a correction that never reached the write
    would make the review theatre."""
    seen: list[Draft] = []

    async def _commit(_db: object, _s: object, draft: Draft, _c: object) -> Entity:
        seen.append(draft)
        return Entity.create(draft.title, draft.kind)

    monkeypatch.setattr(drafts, "commit", _commit)

    async def _no_client(_self: RdtApp) -> None: ...

    monkeypatch.setattr(RdtApp, "http", _no_client)

    async with app.run_test(size=(150, 40)) as pilot:
        app.push_screen(DraftScreen(_synthetic()))
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DraftScreen)
        category = screen.picker("category")
        while category.cycle.value != TechCategory.LAPTOP.value:
            category.cycle.action_step(1)
        await pilot.pause()
        await pilot.press("ctrl+s")
        await until(pilot, lambda: bool(seen), "the commit")

    (committed,) = seen
    assert committed.category is TechCategory.LAPTOP
    assert committed.title == "Steam Deck 2"


async def test_the_form_says_why_each_field_was_prefilled(app: RdtApp) -> None:
    """The whole licence for guessing eagerly. A prefill costs one ←→ to correct *if* you can
    see it is a guess and what it was read off; unattributed it saves nothing."""
    async with app.run_test(size=(150, 40)) as pilot:
        app.push_screen(
            DraftScreen(
                Draft(
                    title="Avengers Doomsday",
                    kind=MediaKind.MOVIE,
                    reasons=("kind read off the 2 matches above (all movie)",),
                )
            )
        )
        await pilot.pause()
        assert "2 matches above" in str(app.screen.query_one("#draft-lineage", Static).content)


async def test_the_coord_rows_belong_to_tv_alone(app: RdtApp) -> None:
    """Season and part are TV coordinates, shown on the same terms the category row is shown
    for tech — so a film never offers a field that would mean nothing on it."""
    async with app.run_test(size=(150, 40)) as pilot:
        app.push_screen(DraftScreen(Draft(title="Pluribus", kind=MediaKind.TV, season=2)))
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DraftScreen)
        assert screen._row("season").display  # pyright: ignore[reportPrivateUsage]
        kind = screen.picker("kind").cycle
        while kind.value != MediaKind.MOVIE.value:
            kind.action_step(1)
        await pilot.pause()
        assert not screen._row("season").display  # pyright: ignore[reportPrivateUsage]


async def test_a_coord_left_behind_by_a_kind_change_is_not_written(
    app: RdtApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hidden-field trap. A `season:2` prefill must not survive being re-kinded to a film
    off screen, where nobody could see it, and land as a coordinate on a movie."""
    committed: list[Draft] = []

    async def _commit(*_a: object, **_k: object) -> Entity | None:
        committed.append(_a[2])  # type: ignore[arg-type]
        return None

    monkeypatch.setattr(drafts, "commit", _commit)

    async with app.run_test(size=(150, 40)) as pilot:
        app.push_screen(DraftScreen(Draft(title="Pluribus", kind=MediaKind.TV, season=2, part=1)))
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DraftScreen)
        kind = screen.picker("kind").cycle
        while kind.value != MediaKind.MOVIE.value:
            kind.action_step(1)
        await pilot.press("ctrl+s")
        await until(pilot, lambda: bool(committed), "the commit")
        assert committed[0].season is None
        assert committed[0].part is None


async def test_escape_drops_the_draft_without_writing(app: RdtApp) -> None:
    async with app.run_test(size=(150, 40)) as pilot:
        app.push_screen(DraftScreen(_synthetic()))
        await pilot.pause()
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, DraftScreen), "the screen to close")
        assert app.db.get_entity(Entity.make_id("Steam Deck 2", MediaKind.TECH)) is None


async def test_e_reviews_a_real_candidate_instead_of_adding_it(
    app: RdtApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second door applies to search hits too, not just invented ones — the whole
    point is being able to look before anything is written."""
    hit = Candidate(source="tmdb", id_key="tmdb", canonical_id="1", title="Dune Part Three")

    async def _search(*_a: object, **_k: object) -> list[tuple[MediaKind, Candidate]]:
        return [(MediaKind.MOVIE, hit)]

    async def _no_client(_self: RdtApp) -> None: ...

    monkeypatch.setattr(add_module, "capture_candidates", _search)
    monkeypatch.setattr(RdtApp, "http", _no_client)

    async with app.run_test(size=(150, 40)) as pilot:
        screen = await _open_add(app, pilot)
        await _search_for(pilot, screen, "Dune Part Three")
        await pilot.press("down")
        await pilot.press("e")
        await until(pilot, lambda: isinstance(app.screen, DraftScreen), "the review screen")
        review = app.screen
        assert isinstance(review, DraftScreen)
        # It carries the candidate, so committing still goes through the normal capture
        # rather than writing a bare row.
        assert not review.draft.synthetic
        assert review.draft.candidate is hit
        assert review.draft.kind is MediaKind.MOVIE
