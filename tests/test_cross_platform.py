"""Behaviour that differs by operating system, exercised on every OS in the CI matrix.

The tracker is a local tool: it resolves its own paths, opens a SQLite file, and writes
text the user typed. Each of those is somewhere Linux, macOS and Windows disagree — path
anchors, path separators and filename rules, and the default text encoding — and none of
it is covered by testing the logic on one OS. These pin the behaviour rather than the
spelling, so a real difference fails and a cosmetic one does not.
"""

from __future__ import annotations

import os
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest
from platformdirs import PlatformDirs

from release_tracker.config import Settings
from release_tracker.db import Database
from release_tracker.models import Entity, MediaKind

# A name that is legal on every target OS but exercises the parts that differ: spaces,
# non-ASCII beyond latin-1, and a character Windows stores as UTF-16 internally.
AWKWARD = "Amélie · 君の名は。 (2026)"


# --- path resolution ----------------------------------------------------------------


def test_the_default_paths_are_absolute_and_under_the_user_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wherever the OS puts them, they must be user-owned and not relative to the CWD.

    This is the portability claim in one assertion: an absolute path anchored in the
    user's home is the same path from any working directory, on any of the three OSes.
    """
    monkeypatch.chdir(tmp_path)
    s = Settings()

    home = Path.home().resolve()
    for path in (s.db_path, s.seeds_path, s.trend_cache_path, s.platform_db_path):
        assert path.is_absolute(), f"{path} is relative — it would follow the CWD"
        assert home in path.resolve().parents, f"{path} is not under {home}"


def test_the_defaults_match_this_platform_s_own_convention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """~/.local/share on Linux, ~/Library/… on macOS, %LOCALAPPDATA% on Windows."""
    monkeypatch.chdir(tmp_path)
    dirs = PlatformDirs(appname="rdt", appauthor=False)
    s = Settings()

    assert s.db_path == Path(dirs.user_data_dir) / "releases.db"
    assert s.platform_db_path == Path(dirs.user_data_dir) / "platforms.db"
    assert s.trend_cache_path == Path(dirs.user_cache_dir) / "trends_cache.db"
    assert s.seeds_path == Path(dirs.user_config_dir) / "seeds.json"


def test_an_env_override_accepts_this_platform_s_path_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Windows user types C:\\..., a Unix user types /... — both arrive as one Path."""
    native = tmp_path / "custom dir" / "releases.db"
    monkeypatch.setenv("RDT_DB_PATH", str(native))
    monkeypatch.chdir(tmp_path)

    assert Settings().db_path == native


# --- the database on this filesystem ------------------------------------------------


def test_the_database_opens_under_a_path_with_spaces_and_non_ascii(tmp_path: Path) -> None:
    """Everything below the tracker's paths is user-controlled, and homes have spaces."""
    path = tmp_path / "My Tracker" / "données" / "releases.db"
    db = Database(path)  # creates its own parents
    try:
        assert path.is_file()
    finally:
        db.close()


def test_text_survives_the_round_trip_regardless_of_the_locale_encoding(
    tmp_path: Path,
) -> None:
    """Windows still defaults `open()` to cp1252, which mangles anything beyond latin-1.

    Titles and notes are whatever the user typed, so a non-UTF-8 default anywhere in the
    write path is a silent corruption rather than an error.
    """
    entity = Entity.create(AWKWARD, MediaKind.MOVIE)
    db = Database(tmp_path / "releases.db")
    try:
        db.upsert_entity(entity)
        reread = db.get_entity(entity.id)
        assert reread is not None
        assert reread.title == AWKWARD
    finally:
        db.close()


def test_a_database_can_be_reopened_after_close(tmp_path: Path) -> None:
    """Windows refuses to delete or reopen a file another handle still holds.

    A tracker that leaks its connection works fine until the user's next command, so the
    close has to actually release the file.
    """
    path = tmp_path / "releases.db"
    first = Database(path)
    first.upsert_entity(Entity.create(AWKWARD, MediaKind.MOVIE))
    first.close()

    second = Database(path)
    try:
        assert [e.title for e in second.iter_entities()] == [AWKWARD]
    finally:
        second.close()


# --- the installed command ----------------------------------------------------------


def test_the_cli_entry_point_runs_on_this_platform(tmp_path: Path) -> None:
    """`python -m` stands in for the console script: same import path, no PATH assumptions.

    Run from a temp directory so anything that quietly depended on the checkout — a
    relative data path, a package-relative asset — fails here rather than on a user's
    machine.

    PYTHONIOENCODING is set because a pipe on Windows inherits the console code page, not
    UTF-8, and rich's box drawing and em dashes are not in cp1252. Without it the child's
    output is undecodable here and the assertion below fails on an encoding artefact
    instead of on behaviour; `errors="replace"` keeps that true even if a runner ignores
    the hint.
    """
    result = subprocess.run(
        [sys.executable, "-m", "release_tracker.cli", "--help"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "upcoming" in result.stdout


@pytest.mark.parametrize("hostile", ["cp1252", "ascii"])
def test_the_tables_print_on_a_stdout_that_cannot_encode_their_glyphs(
    tmp_path: Path, hostile: str
) -> None:
    """The Windows crash, reproduced everywhere: a pipe inheriting a non-UTF-8 code page.

    `rdt upcoming` draws ⟳, ● and ⛔, none of which exist in cp1252 — the code page a
    Windows pipe or legacy console inherits — so the command used to die with
    UnicodeEncodeError *partway through the table*, after emitting half of it. Forcing
    the encoding here means the regression is caught on Linux and macOS too, rather than
    only when the Windows job happens to run.
    """
    for command in ("upcoming", "show"):
        # S603: the argv is this interpreter and two literals from the loop above.
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "release_tracker.cli", command],
            cwd=tmp_path,
            env={**os.environ, "PYTHONIOENCODING": hostile, "RDT_DB_PATH": str(tmp_path / "r.db")},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, f"rdt {command} under {hostile}:\n{result.stderr}"
        assert "UnicodeEncodeError" not in result.stderr


def test_version_reports_the_installed_distribution(tmp_path: Path) -> None:
    """`--version` is what a bug report quotes, so it must not be a second hardcoded copy."""
    result = subprocess.run(
        [sys.executable, "-m", "release_tracker.cli", "--version"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == version("release-date-tracker")
