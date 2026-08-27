"""The settings screen: what it shows, what it writes, and what it refuses to pretend.

The screen exists because configuring `rdt` previously meant hand-authoring a file nothing
creates. The things worth pinning are the ones that would quietly mislead: a masked value
that leaks, an untouched field that overwrites itself, and a write that appears to work
while an environment variable outranks it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr
from textual.widgets import Static

from release_tracker import config, config_file
from release_tracker.config import Settings, get_settings
from release_tracker.config_file import set_values
from release_tracker.db import Database
from release_tracker.tui.app import RdtApp
from release_tracker.tui.settings import SettingsScreen

TODAY = __import__("datetime").date(2026, 8, 28)


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RdtApp:
    absent = tmp_path / "nowhere" / ".env"
    monkeypatch.setattr(config, "env_file_paths", lambda: (absent, absent))
    monkeypatch.setattr(config_file, "env_file_paths", lambda: (absent, absent))
    for alias in config_file.SECRET_ALIASES:
        monkeypatch.delenv(alias, raising=False)
    get_settings.cache_clear()
    return RdtApp(settings=get_settings(), db=Database(tmp_path / "s.db"), today=TODAY)


def _status(screen: SettingsScreen) -> str:
    return str(screen.query_one("#settings-status", Static).content)


async def _open(app: RdtApp, pilot: object) -> SettingsScreen:
    app.push_screen(SettingsScreen())
    await pilot.pause()  # pyright: ignore[reportAttributeAccessIssue]
    await pilot.pause()  # pyright: ignore[reportAttributeAccessIssue]
    screen = app.screen
    assert isinstance(screen, SettingsScreen)
    return screen


# --- showing ------------------------------------------------------------------------------
async def test_a_stored_credential_is_shown_masked(app: RdtApp, isolated_config: Path) -> None:
    """Enough to tell which key it is, never enough to use — the screen is the thing most
    likely to be on screen while sharing it."""
    set_values({"TMDB_API_KEY": "abcdef0123456789"}, path=isolated_config)
    get_settings.cache_clear()
    async with app.run_test(size=(110, 40)) as pilot:
        screen = await _open(app, pilot)
        placeholder = screen.row("TMDB_API_KEY").field.placeholder
        assert "abcdef0123456789" not in placeholder
        assert placeholder.startswith("abcd")


async def test_a_value_the_environment_supplies_says_it_wins(
    app: RdtApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise typing here looks like it did nothing, which is the most confusing
    outcome available."""
    monkeypatch.setenv("TMDB_API_KEY", "from-env")
    get_settings.cache_clear()
    async with app.run_test(size=(110, 40)) as pilot:
        screen = await _open(app, pilot)
        assert "environment" in screen.row("TMDB_API_KEY").note()


async def test_the_tracker_path_says_it_needs_a_restart(app: RdtApp) -> None:
    """Applying it live would close a connection async workers hold across awaits."""
    async with app.run_test(size=(110, 40)) as pilot:
        screen = await _open(app, pilot)
        assert "next launch" in screen.row("RDT_DB_PATH").note()


# --- writing ------------------------------------------------------------------------------
async def test_typing_a_value_writes_it_and_applies_it(app: RdtApp, isolated_config: Path) -> None:
    async with app.run_test(size=(110, 40)) as pilot:
        screen = await _open(app, pilot)
        screen.row("RDT_FRESH_DAYS").field.value = "3"
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert config_file.read_config(isolated_config) == {"RDT_FRESH_DAYS": 3}
        assert app.settings.fresh_days == 3, "and it takes effect without a restart"


async def test_an_untouched_field_is_not_rewritten(app: RdtApp, isolated_config: Path) -> None:
    """Current values are shown as placeholders precisely so that leaving a field alone
    means leaving it alone — writing them back would freeze today's defaults into the file."""
    async with app.run_test(size=(110, 40)) as pilot:
        screen = await _open(app, pilot)
        screen.row("RDT_FRESH_DAYS").field.value = "3"
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert set(config_file.read_config(isolated_config)) == {"RDT_FRESH_DAYS"}


async def test_a_rejected_value_keeps_the_screen_and_the_typing(
    app: RdtApp, isolated_config: Path
) -> None:
    """A validation message contains square brackets, which Rich parses as a style tag —
    interpolating it raised `MissingStyle` out of the render and took the screen down."""
    async with app.run_test(size=(110, 40)) as pilot:
        screen = await _open(app, pilot)
        screen.row("RDT_AVAILABILITY_CHANNEL").field.value = "telepathy"
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen), "the screen survived"
        assert "Rejected" in _status(screen)
        assert screen.row("RDT_AVAILABILITY_CHANNEL").field.value == "telepathy"
        assert not isolated_config.exists()


async def test_saving_nothing_says_so(app: RdtApp) -> None:
    async with app.run_test(size=(110, 40)) as pilot:
        screen = await _open(app, pilot)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert "Nothing to save" in _status(screen)


async def test_a_write_shadowed_by_the_environment_says_so(
    app: RdtApp, isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TMDB_API_KEY", "from-env")
    get_settings.cache_clear()
    async with app.run_test(size=(110, 40)) as pilot:
        screen = await _open(app, pilot)
        screen.row("TMDB_API_KEY").field.value = "from-file"
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert "which wins" in _status(screen)


async def test_the_screen_reaches_the_file_the_cli_reads(
    app: RdtApp, isolated_config: Path
) -> None:
    """One file, two front ends — a key set in the TUI has to be the key `rdt` then uses."""
    async with app.run_test(size=(110, 40)) as pilot:
        screen = await _open(app, pilot)
        screen.row("TMDB_API_KEY").field.value = "typed-in-the-tui"
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
    get_settings.cache_clear()
    assert Settings().tmdb_api_key == SecretStr("typed-in-the-tui")
