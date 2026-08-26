"""Tests for runtime configuration (display colors are user-overridable)."""

from __future__ import annotations

from pathlib import Path

import pytest

from release_tracker import config
from release_tracker.config import Settings


def test_default_colors_are_colorblind_safe_not_green_yellow() -> None:
    # the default ramp is cyan/orange/red (cool-vs-warm), not the green/yellow that
    # red-green color blindness can't separate.
    s = Settings()
    assert (s.confirmed_color, s.speculative_color) == ("cyan", "orange1")
    assert (s.fresh_color, s.aging_color, s.stale_color) == ("cyan", "orange1", "red")
    assert "green" not in {s.confirmed_color, s.speculative_color, s.fresh_color, s.aging_color}


def test_colors_are_env_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RDT_CONFIRMED_COLOR", "blue")
    monkeypatch.setenv("RDT_SPECULATIVE_COLOR", "magenta")
    monkeypatch.setenv("RDT_STALE_COLOR", "bright_red")
    s = Settings()
    assert s.confirmed_color == "blue"
    assert s.speculative_color == "magenta"
    assert s.stale_color == "bright_red"
    assert s.fresh_color == "cyan"  # untouched default


# --- path portability ---------------------------------------------------------------
# An installed `rdt` is invoked from wherever the user is standing. If the defaults were
# CWD-relative it would open a different (empty) tracker per directory, so these pin the
# XDG resolution and the one escape hatch that keeps an existing checkout working.


def test_paths_do_not_follow_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for d in ("a", "b"):
        (tmp_path / d).mkdir()

    monkeypatch.chdir(tmp_path / "a")
    from_a = config.Settings()
    monkeypatch.chdir(tmp_path / "b")
    from_b = config.Settings()

    assert from_a.db_path == from_b.db_path == tmp_path / "share" / "rdt" / "releases.db"
    assert from_a.trend_cache_path == tmp_path / "cache" / "rdt" / "trends_cache.db"
    assert from_a.seeds_path == tmp_path / "config" / "rdt" / "seeds.json"
    assert from_a.platform_db_path == tmp_path / "share" / "rdt" / "platforms.db"


def test_an_existing_project_database_is_not_stranded_by_the_xdg_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkout that has been tracking titles keeps its data/ — and its local/ seeds."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    (tmp_path / "checkout" / "data").mkdir(parents=True)
    (tmp_path / "checkout" / "data" / "releases.db").write_bytes(b"")
    monkeypatch.chdir(tmp_path / "checkout")

    s = config.Settings()
    assert s.db_path == Path("data/releases.db")
    assert s.seeds_path == Path("local/seeds.json")  # the old split, not data/seeds.json


def test_every_path_stays_env_overridable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RDT_DB_PATH", "/srv/rdt/releases.db")
    monkeypatch.setenv("RDT_SEEDS_PATH", "/srv/rdt/seeds.json")
    monkeypatch.chdir(tmp_path)
    s = config.Settings()
    assert s.db_path == Path("/srv/rdt/releases.db")
    assert s.seeds_path == Path("/srv/rdt/seeds.json")
