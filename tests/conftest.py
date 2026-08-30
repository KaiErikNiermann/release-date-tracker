"""Keep the suite off the developer's real configuration.

`Settings` reads `~/.config/rdt/` — and now that `rdt` *writes* a `config.toml` there,
anyone who sets a path or a key through the settings screen would start failing unrelated
tests on their own machine. Every test is pointed at a config file that does not exist.

The `.env` chain and the credential environment variables are neutralised too. Without
that, a developer with keys and a CI runner without them are running different suites: the
add screen says "no matches" on one and "TMDB_API_KEY is not set" on the other, and two
tests passed locally for months of nothing while failing the moment they left the machine.
The baseline is now "nothing configured" everywhere, and a test that needs a key says so.

`XDG_CONFIG_HOME` itself is left alone: `test_cross_platform` legitimately asserts the real
XDG defaults, which moving it would defeat.

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
from pydantic import SecretStr

from release_tracker import config, config_file
from release_tracker.config import CONFIG_FILE_ENV, Settings, get_settings
from release_tracker.sources.igdb import forget_tokens


@pytest.fixture(autouse=True)
def reset_process_caches() -> Iterator[None]:
    """Clear the caches that deliberately outlive a single call, so tests stay order-independent.

    The IGDB app token is shared process-wide to keep an interactive add fast, which would
    otherwise let one test's state decide another's outcome.
    """
    forget_tokens()
    yield
    forget_tokens()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point config.toml at a path that starts out absent. Yields it, so a test that wants
    to exercise the file can write there."""
    target = tmp_path / "config" / "config.toml"
    monkeypatch.setenv(CONFIG_FILE_ENV, str(target))
    # `Settings` resolves the chain through this function at load time, so patching the
    # module attribute is what actually detaches it from the developer's ~/.config and from
    # a .env beside the checkout.
    absent = tmp_path / "no-env" / ".env"
    monkeypatch.setattr(config, "env_file_paths", lambda: (absent, absent))
    monkeypatch.setattr(config_file, "env_file_paths", lambda: (absent, absent))
    for alias in config_file.SECRET_ALIASES:
        monkeypatch.delenv(alias, raising=False)
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


def with_keys(settings: Settings) -> Settings:
    """The same settings with every credential present.

    For a test whose subject is *routing* rather than configuration — "does an unhinted
    search retry as tech", "does a film query get a synthetic row" — the sources have to be
    configured, or the screen correctly reports what is missing instead of what was asked.
    """
    return settings.model_copy(
        update={
            "tmdb_api_key": SecretStr("test-tmdb-key"),
            "twitch_client_id": SecretStr("test-twitch-id"),
            "twitch_client_secret": SecretStr("test-twitch-secret"),
        }
    )
