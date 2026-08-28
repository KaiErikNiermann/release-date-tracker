"""The add palette: keyboard movement, and what happens while a capture is in flight.

Nothing here touches the network. `AddScreen` reaches the outside world through exactly
two module-level names, so the fakes below replace those and the screen is exercised as
the user drives it — keys in, focus and widget state out.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from textual.widgets import Input, OptionList, Static

from conftest import until, with_keys
from release_tracker.config import Settings, get_settings
from release_tracker.db import Database
from release_tracker.models import Entity, MediaKind
from release_tracker.sources.base import Candidate
from release_tracker.tui import add as add_module
from release_tracker.tui.add import AddScreen
from release_tracker.tui.app import RdtApp

TODAY = date(2026, 6, 1)


def _candidates(*titles: str) -> list[tuple[MediaKind, Candidate]]:
    return [
        (
            MediaKind.MOVIE,
            Candidate(
                source="tmdb",
                id_key="tmdb",
                canonical_id=str(i),
                title=t,
                year=2026,
            ),
        )
        for i, t in enumerate(titles)
    ]


@pytest.fixture
def app(tmp_path: Path) -> RdtApp:
    settings: Settings = get_settings()
    return RdtApp(settings=settings, db=Database(tmp_path / "add.db"), today=TODAY)


@pytest.fixture
def offer(monkeypatch: pytest.MonkeyPatch) -> list[tuple[MediaKind, Candidate]]:
    """Make the search return a fixed set instantly, with no client and no network."""
    hits = _candidates("Dune Part Three", "Dune Prophecy", "Dune Messiah")

    async def _search(*_a: object, **_k: object) -> list[tuple[MediaKind, Candidate]]:
        return hits

    async def _no_client(_self: RdtApp) -> None:
        """The fake search ignores the client, so the screen never needs a real one."""

    monkeypatch.setattr(add_module, "capture_candidates", _search)
    monkeypatch.setattr(RdtApp, "http", _no_client)
    return hits


async def _open_add(app: RdtApp, pilot: Any) -> AddScreen:
    app.open_add("")
    await pilot.pause()
    screen = app.screen
    assert isinstance(screen, AddScreen)
    return screen


def _status_text(screen: AddScreen) -> str:
    return str(screen.query_one("#add-status", Static).content)


async def _search_for(pilot: Any, screen: AddScreen, text: str) -> None:
    screen.query_one("#add-query", Input).focus()
    await pilot.press(*text)
    screen.search(text, None)  # skip the debounce timer; the worker is what we exercise
    await until(
        pilot,
        lambda: not screen.query_one("#candidates", OptionList).loading,
        f"the search for {text!r} to settle",
    )


# --- moving between the bar and the candidates --------------------------------------


async def test_down_from_the_bar_lands_on_the_candidates(
    app: RdtApp, offer: list[tuple[MediaKind, Candidate]]
) -> None:
    """The same move as the browse query bar — down out of a search box enters its list."""
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_add(app, pilot)
        await _search_for(pilot, screen, "dune")
        assert isinstance(screen.focused, Input)

        await pilot.press("down")
        await pilot.pause()
        assert isinstance(screen.focused, OptionList)


async def test_enter_on_the_bar_focuses_the_candidates_it_is_already_showing(
    app: RdtApp, offer: list[tuple[MediaKind, Candidate]]
) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_add(app, pilot)
        await _search_for(pilot, screen, "dune")

        screen.query_one("#add-query", Input).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(screen.focused, OptionList)


async def test_down_does_nothing_while_there_is_nothing_to_move_into(app: RdtApp) -> None:
    """An empty list has nowhere to land, so the bar keeps focus rather than going dead."""
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_add(app, pilot)
        screen.query_one("#add-query", Input).focus()
        await pilot.press("down")
        await pilot.pause()
        assert isinstance(screen.focused, Input)


async def test_escape_walks_back_to_the_bar_before_it_closes_the_screen(
    app: RdtApp, offer: list[tuple[MediaKind, Candidate]]
) -> None:
    """Escape out of the list is a step back, not an exit — as it is on the browse screen."""
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_add(app, pilot)
        await _search_for(pilot, screen, "dune")
        await pilot.press("down")
        await pilot.pause()
        assert isinstance(screen.focused, OptionList)

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, AddScreen), "escape from the list must not close it"
        assert isinstance(app.screen.focused, Input)

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, AddScreen), "escape from the bar closes it"


# --- capturing ----------------------------------------------------------------------


@pytest.fixture
def slow_capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A capture that takes long enough for a keystroke to land in the middle of it."""
    state: dict[str, Any] = {"finished": False}

    async def _report(*_a: object, **_k: object) -> object:
        return object()

    async def _capture_work(*_a: object, **_k: object) -> Entity:
        await asyncio.sleep(0.25)
        state["finished"] = True
        return Entity.create("Dune Part Three", MediaKind.MOVIE)

    monkeypatch.setattr(add_module, "report_for_candidate", _report)
    monkeypatch.setattr(add_module, "capture_work", _capture_work)
    return state


async def test_a_keystroke_mid_capture_does_not_cancel_the_write(
    app: RdtApp,
    offer: list[tuple[MediaKind, Candidate]],
    slow_capture: dict[str, Any],
) -> None:
    """`search` and `capture` shared the default worker group, and both are exclusive.

    A search starting while a capture was in flight therefore cancelled it — after the
    report had been fetched and somewhere around the writes, leaving a half-enriched
    work behind and no error to explain it.
    """
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_add(app, pilot)
        await _search_for(pilot, screen, "dune")

        kind, cand = screen._hits[0]  # pyright: ignore[reportPrivateUsage]
        screen.capture(kind, cand)
        await pilot.pause()

        screen.search("dune part", None)  # what a keystroke's debounce would fire
        await pilot.pause()

        await until(
            pilot,
            lambda: slow_capture["finished"],
            "the capture to finish — a search cancelled it",
        )


async def test_a_running_capture_is_visible_and_the_bar_is_dead(
    app: RdtApp,
    offer: list[tuple[MediaKind, Candidate]],
    slow_capture: dict[str, Any],
) -> None:
    """Several seconds of network used to look exactly like the idle screen."""
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_add(app, pilot)
        await _search_for(pilot, screen, "dune")
        options = screen.query_one("#candidates", OptionList)
        bar = screen.query_one("#add-query", Input)
        assert not options.loading and not bar.disabled

        kind, cand = screen._hits[0]  # pyright: ignore[reportPrivateUsage]
        screen.capture(kind, cand)
        await until(pilot, lambda: options.loading, "the capture to show as running")

        assert bar.disabled, "the bar still takes keys that will not be read"
        assert "adding" in _status_text(screen)


async def test_a_failed_capture_gives_the_screen_back(
    app: RdtApp, offer: list[tuple[MediaKind, Candidate]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead provider must leave a usable palette, not a permanent spinner."""

    async def _boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("tmdb is down")

    monkeypatch.setattr(add_module, "report_for_candidate", _boom)

    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_add(app, pilot)
        await _search_for(pilot, screen, "dune")

        kind, cand = screen._hits[0]  # pyright: ignore[reportPrivateUsage]
        screen.capture(kind, cand)
        await until(
            pilot,
            lambda: "tmdb is down" in _status_text(screen),
            "the failure to reach the status line",
        )

        assert not screen.query_one("#candidates", OptionList).loading
        assert not screen.query_one("#add-query", Input).disabled


async def test_a_search_that_fails_clears_its_own_spinner(
    app: RdtApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(*_a: object, **_k: object) -> list[tuple[MediaKind, Candidate]]:
        raise RuntimeError("no provider")

    monkeypatch.setattr(add_module, "capture_candidates", _boom)

    async def _no_client(_self: RdtApp) -> None:
        """The failing search never gets as far as using a client."""

    monkeypatch.setattr(RdtApp, "http", _no_client)

    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_add(app, pilot)
        await _search_for(pilot, screen, "dune")

        assert not screen.query_one("#candidates", OptionList).loading
        assert "no provider" in _status_text(screen)


async def test_a_device_name_retries_as_tech_when_the_media_dbs_are_empty(
    app: RdtApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tech is deliberately absent from the unhinted sweep — Wikidata label-matches
    everything at score 1.0, so "Dune" would return a sand dune alongside the film. Once the
    media DBs come back empty there is nothing left to drown, so a name that plainly reads as
    a device gets a second pass. Without this, typing "Xiaomi Mix 4" silently finds nothing.
    """
    asked: list[MediaKind | None] = []
    device = _candidates("Xiaomi Mi MIX 4")

    async def _search(
        _client: object, _text: str, _settings: object, *, kind_hint: MediaKind | None, **_k: object
    ) -> list[tuple[MediaKind, Candidate]]:
        asked.append(kind_hint)
        return device if kind_hint is MediaKind.TECH else []

    async def _no_client(_self: RdtApp) -> None: ...

    monkeypatch.setattr(add_module, "capture_candidates", _search)
    monkeypatch.setattr(RdtApp, "http", _no_client)

    async with app.run_test(size=(120, 30)) as pilot:
        screen = await _open_add(app, pilot)
        screen.query_one("#add-query", Input).value = "Xiaomi Mix 4"
        await pilot.press("enter")
        await until(pilot, lambda: len(asked) >= 2, "the tech retry")
        assert asked == [None, MediaKind.TECH]
        await until(
            pilot,
            lambda: screen.query_one("#candidates", OptionList).option_count == 1,
            "the device to appear",
        )


async def test_a_film_that_finds_nothing_does_not_retry_as_tech(
    app: RdtApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard that keeps the retry from becoming a second sweep for everything."""
    asked: list[MediaKind | None] = []

    async def _search(
        _client: object, _text: str, _settings: object, *, kind_hint: MediaKind | None, **_k: object
    ) -> list[tuple[MediaKind, Candidate]]:
        asked.append(kind_hint)
        return []

    async def _no_client(_self: RdtApp) -> None: ...

    monkeypatch.setattr(add_module, "capture_candidates", _search)
    monkeypatch.setattr(RdtApp, "http", _no_client)
    # This is about routing, not configuration: with no keys the screen would correctly
    # report the missing ones instead of the miss this test is looking at.
    app.settings = with_keys(app.settings)

    async with app.run_test(size=(120, 30)) as pilot:
        screen = await _open_add(app, pilot)
        screen.query_one("#add-query", Input).value = "Some Unknown Film"
        await pilot.press("enter")
        await until(pilot, lambda: "no matches" in _status_text(screen), "the empty result")
        assert asked == [None]
        # and the miss says how to reach a device, which is the one thing a user can't guess
        assert "kind:tech" in _status_text(screen)


async def test_an_unconfigured_source_is_named_instead_of_looking_like_a_miss(
    app: RdtApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A keyless TMDB returns an empty list exactly like a genuine miss, so "no matches" was
    the answer to both "this film doesn't exist" and "you never gave me a key". The second
    one now says so."""

    async def _nothing(*_a: object, **_k: object) -> list[tuple[MediaKind, Candidate]]:
        return []

    async def _no_client(_self: RdtApp) -> None: ...

    monkeypatch.setattr(add_module, "capture_candidates", _nothing)
    monkeypatch.setattr(RdtApp, "http", _no_client)
    app.settings = app.settings.model_copy(update={"tmdb_api_key": None, "twitch_client_id": None})

    async with app.run_test(size=(150, 40)) as pilot:
        screen = await _open_add(app, pilot)
        await _search_for(pilot, screen, "dune")
        status = _status_text(screen)
        assert "TMDB_API_KEY" in status, status
        assert "no matches" not in status, "the misleading phrasing must not survive"


async def test_a_configured_search_that_finds_nothing_still_says_no_matches(
    app: RdtApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: with keys in place an empty result really does mean the title is
    wrong, and blaming configuration would send the user off fixing the wrong thing."""

    async def _nothing(*_a: object, **_k: object) -> list[tuple[MediaKind, Candidate]]:
        return []

    async def _no_client(_self: RdtApp) -> None: ...

    monkeypatch.setattr(add_module, "capture_candidates", _nothing)
    monkeypatch.setattr(RdtApp, "http", _no_client)
    app.settings = app.settings.model_copy(
        update={
            "tmdb_api_key": SecretStr("k"),
            "twitch_client_id": SecretStr("i"),
            "twitch_client_secret": SecretStr("s"),
        }
    )

    async with app.run_test(size=(150, 40)) as pilot:
        screen = await _open_add(app, pilot)
        await _search_for(pilot, screen, "zzzzzz")
        assert "no matches" in _status_text(screen)
