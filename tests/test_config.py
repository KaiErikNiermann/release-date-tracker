"""Tests for runtime configuration (display colors are user-overridable)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

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
# resolution and the one escape hatch that keeps an existing checkout working. The
# platform-specific spellings are asserted in test_cross_platform.py.


def test_paths_do_not_follow_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for d in ("a", "b"):
        (tmp_path / d).mkdir()

    monkeypatch.chdir(tmp_path / "a")
    from_a = Settings()
    monkeypatch.chdir(tmp_path / "b")
    from_b = Settings()

    assert from_a.db_path == from_b.db_path
    assert from_a.seeds_path == from_b.seeds_path
    assert from_a.trend_cache_path == from_b.trend_cache_path
    assert from_a.platform_db_path == from_b.platform_db_path


@pytest.mark.skipif(sys.platform != "linux", reason="XDG_* is only consulted on Linux")
def test_linux_honours_the_xdg_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)

    s = Settings()
    assert s.db_path == tmp_path / "share" / "rdt" / "releases.db"
    assert s.platform_db_path == tmp_path / "share" / "rdt" / "platforms.db"
    assert s.trend_cache_path == tmp_path / "cache" / "rdt" / "trends_cache.db"
    assert s.seeds_path == tmp_path / "config" / "rdt" / "seeds.json"


def test_an_existing_project_database_is_not_stranded_by_the_xdg_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkout that has been tracking titles keeps its data/ — and its local/ seeds."""
    (tmp_path / "checkout" / "data").mkdir(parents=True)
    (tmp_path / "checkout" / "data" / "releases.db").write_bytes(b"")
    monkeypatch.chdir(tmp_path / "checkout")

    s = Settings()
    assert s.db_path == Path("data/releases.db")
    assert s.seeds_path == Path("local/seeds.json")  # the old split, not data/seeds.json
    assert s.trend_cache_path == Path("data/trends_cache.db")


def test_the_legacy_layout_needs_an_actual_database_not_just_a_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Someone else's `data/` in the CWD must not capture an installed rdt."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "unrelated.csv").write_text("x", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert Settings().db_path.is_absolute()


def test_every_path_stays_env_overridable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "elsewhere"
    monkeypatch.setenv("RDT_DB_PATH", str(target / "releases.db"))
    monkeypatch.setenv("RDT_SEEDS_PATH", str(target / "seeds.json"))
    monkeypatch.chdir(tmp_path)

    s = Settings()
    assert s.db_path == target / "releases.db"
    assert s.seeds_path == target / "seeds.json"
