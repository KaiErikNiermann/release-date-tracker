"""Keep the suite off the developer's real configuration.

`Settings` reads `~/.config/rdt/` — and now that `rdt` *writes* a `config.toml` there,
anyone who sets a path or a key through the settings screen would start failing unrelated
tests on their own machine. Every test is pointed at a config file that does not exist.

Only that file is redirected, not the whole config dir: the `.env` chain is hand-authored
and the app never writes it, and `test_cross_platform` legitimately asserts the *real* XDG
defaults, which moving `XDG_CONFIG_HOME` would defeat.

`get_settings` is `lru_cache`d and nothing in the package clears it, so the cache is also
reset around each test: otherwise the first test to reach it pins that value for the whole
session and anything monkeypatching the environment afterwards is quietly ignored.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from release_tracker.config import CONFIG_FILE_ENV, get_settings


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point config.toml at a path that starts out absent. Yields it, so a test that wants
    to exercise the file can write there."""
    target = tmp_path / "config" / "config.toml"
    monkeypatch.setenv(CONFIG_FILE_ENV, str(target))
    get_settings.cache_clear()
    yield target
    get_settings.cache_clear()


async def until(pilot: Any, predicate: Callable[[], bool], what: str, timeout: float = 5.0) -> None:
    """Poll for a condition instead of sleeping a guessed interval.

    The Windows runners are several times slower than a local run, so any fixed sleep long
    enough to be reliable there is dead time everywhere else — and any shorter one is a
    flake waiting to happen. Shared because three TUI test modules had grown identical
    copies, which is how one of them ends up with a different timeout nobody notices.
    """
    waited = 0.0
    while waited < timeout:
        await pilot.pause()
        if predicate():
            return
        await asyncio.sleep(0.02)
        waited += 0.02
    raise AssertionError(f"timed out waiting for {what}")
