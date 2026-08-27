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

from collections.abc import Iterator
from pathlib import Path

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
