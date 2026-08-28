"""The writable config layer: what wins, what survives a rewrite, and what refuses.

The file is app-owned, so the risks are not the usual parsing ones. They are: writing a
spelling the loader silently ignores, clobbering something the user put there, leaving a
half-written file behind, and a secret sitting world-readable for a moment.
"""

from __future__ import annotations

import stat
import sys
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from release_tracker import config, config_file
from release_tracker.config import (
    ConfigFileError,
    Settings,
    config_file_path,
    field_name_for,
    secret,
)
from release_tracker.config_file import (
    FIELD_DOCS,
    known_aliases,
    mask,
    migrate_env,
    origins,
    read_config,
    set_values,
)


# --- precedence -------------------------------------------------------------------------
def test_the_file_is_read(isolated_config: Path) -> None:
    set_values({"RDT_FRESH_DAYS": "3", "TMDB_API_KEY": "from-toml"}, path=isolated_config)
    settings = Settings()
    assert settings.fresh_days == 3
    assert secret(settings.tmdb_api_key) == "from-toml"


def test_the_environment_still_wins(isolated_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What makes a one-off `TMDB_API_KEY=… rdt …` work, and what keeps a CI runner immune
    to whatever a developer once typed into the settings screen."""
    set_values({"RDT_FRESH_DAYS": "3"}, path=isolated_config)
    monkeypatch.setenv("RDT_FRESH_DAYS", "99")
    assert Settings().fresh_days == 99


def test_an_absent_file_changes_nothing(isolated_config: Path) -> None:
    assert not isolated_config.exists()
    assert Settings().fresh_days == Settings.model_fields["fresh_days"].default


# --- the spelling that actually works ----------------------------------------------------
def test_field_name_spelling_is_not_silently_accepted(isolated_config: Path) -> None:
    """`TomlConfigSettingsSource` matches on the *alias*, so `fresh_days = 3` parses fine and
    is then dropped by `extra="ignore"`. The writer must never produce that, which is why it
    writes alias spellings and why `set_values` rejects anything else outright."""
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text("fresh_days = 3\n")
    assert Settings().fresh_days != 3, "if this ever passes, the schema can be field-keyed"

    with pytest.raises(KeyError):
        set_values({"fresh_days": "3"}, path=isolated_config)


def test_every_setting_has_somewhere_to_live() -> None:
    """The guard on "any Settings field should be expressible" — a new setting cannot be
    added without deciding how the file describes it."""
    assert {doc.alias for doc in FIELD_DOCS} == known_aliases()


# --- writing ------------------------------------------------------------------------------
def test_only_what_was_set_is_written(isolated_config: Path) -> None:
    """Sparse on purpose. Freezing the default paths into the file would pin the XDG
    resolution and the legacy `data/` layout to whatever the machine looked like that day."""
    set_values({"RDT_FRESH_DAYS": "3"}, path=isolated_config)
    assert set(read_config(isolated_config)) == {"RDT_FRESH_DAYS"}


def test_values_are_typed_not_stringly(isolated_config: Path) -> None:
    set_values(
        {"RDT_FRESH_DAYS": "7", "RDT_JUSTWATCH": "false", "RDT_REGIONS": "DE,US"},
        path=isolated_config,
    )
    stored = read_config(isolated_config)
    assert stored == {"RDT_FRESH_DAYS": 7, "RDT_JUSTWATCH": False, "RDT_REGIONS": "DE,US"}


def test_a_second_write_keeps_the_first(isolated_config: Path) -> None:
    set_values({"TMDB_API_KEY": "a"}, path=isolated_config)
    set_values({"RDT_FRESH_DAYS": "5"}, path=isolated_config)
    assert read_config(isolated_config) == {"TMDB_API_KEY": "a", "RDT_FRESH_DAYS": 5}


def test_unsetting_removes_the_key(isolated_config: Path) -> None:
    set_values({"TMDB_API_KEY": "a", "RDT_FRESH_DAYS": "5"}, path=isolated_config)
    set_values({"TMDB_API_KEY": None}, path=isolated_config)
    assert read_config(isolated_config) == {"RDT_FRESH_DAYS": 5}


def test_a_key_from_a_newer_rdt_survives_a_rewrite(isolated_config: Path) -> None:
    """More likely someone else's new setting than a typo, and dropping it on a round-trip
    through an older build would be a nasty way to lose one."""
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text('RDT_FROM_THE_FUTURE = "keep me"\n')
    set_values({"RDT_FRESH_DAYS": "5"}, path=isolated_config)
    assert read_config(isolated_config)["RDT_FROM_THE_FUTURE"] == "keep me"


@pytest.mark.parametrize(
    "value",
    [
        r"C:\Users\kai\rdt\releases.db",  # backslashes — where a hand-rolled quoter dies
        'has "quotes" in it',
        "a\nnewline",
        "ünïcode ✓",
    ],
)
def test_awkward_values_round_trip(isolated_config: Path, value: str) -> None:
    set_values({"RDT_REGIONS": value}, path=isolated_config)
    with isolated_config.open("rb") as handle:
        assert tomllib.load(handle)["RDT_REGIONS"] == value


def test_a_bad_value_is_refused_before_anything_is_written(isolated_config: Path) -> None:
    with pytest.raises(ValidationError):
        set_values({"RDT_AVAILABILITY_CHANNEL": "telepathy"}, path=isolated_config)
    assert not isolated_config.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_the_file_is_not_world_readable(isolated_config: Path) -> None:
    """It holds API keys. The mode is set when the file is created, not after, so there is
    no window in which a secret is readable by anyone else."""
    set_values({"TMDB_API_KEY": "secret"}, path=isolated_config)
    assert stat.S_IMODE(isolated_config.stat().st_mode) == 0o600


def test_a_malformed_file_names_itself(isolated_config: Path) -> None:
    """Unwrapped this raises out of every `get_settings()` call — including the command you
    would use to fix it."""
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text('TMDB_API_KEY = "unterminated\n')
    with pytest.raises(ConfigFileError) as caught:
        read_config(isolated_config)
    assert str(isolated_config) in str(caught.value)


# --- migration ----------------------------------------------------------------------------
def test_migration_lifts_recognised_keys_once(
    isolated_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("TMDB_API_KEY=from-env\nNOT_A_SETTING=ignored\nRDT_FRESH_DAYS=9\n")
    monkeypatch.setattr(config_file, "env_file_paths", lambda: (env, env))

    report = migrate_env(path=isolated_config)
    assert report is not None
    assert set(report.aliases) == {"TMDB_API_KEY", "RDT_FRESH_DAYS"}
    assert read_config(isolated_config) == {"TMDB_API_KEY": "from-env", "RDT_FRESH_DAYS": 9}

    assert migrate_env(path=isolated_config) is None, "runs once, never again"


def test_migration_does_nothing_without_an_env(
    isolated_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    absent = tmp_path / "nothing-here" / ".env"
    monkeypatch.setattr(config_file, "env_file_paths", lambda: (absent, absent))
    assert migrate_env(path=isolated_config) is None
    assert not isolated_config.exists()


# --- presentation -------------------------------------------------------------------------
def test_a_secret_is_masked_but_still_recognisable() -> None:
    assert mask("TMDB_API_KEY", "abcdef0123456789") == "abcd…89"
    assert mask("TMDB_API_KEY", "short") == "…"
    assert mask("RDT_REGIONS", "US,DE") == "US,DE", "only secrets are masked"


def test_origin_says_where_a_value_came_from(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The answer to "why didn't my key take effect", which is almost always a leftover
    environment variable winning over the one just typed in."""
    set_values({"TMDB_API_KEY": "from-toml", "RDT_FRESH_DAYS": "3"}, path=isolated_config)
    monkeypatch.setenv("TMDB_API_KEY", "from-env")
    found = origins(isolated_config)
    assert found["TMDB_API_KEY"] == "environment"
    assert found["RDT_FRESH_DAYS"] == "config.toml"


def test_the_config_path_is_redirectable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """So a checkout, a test or someone with opinions about directories can move it."""
    monkeypatch.setenv("RDT_CONFIG_FILE", str(tmp_path / "elsewhere.toml"))
    assert config_file_path() == tmp_path / "elsewhere.toml"


# --- the suite's own independence ----------------------------------------------------------
def test_the_suite_does_not_see_the_developers_credentials() -> None:
    """The guard for a whole class of "passes here, fails in CI".

    Two TUI tests asserted on the add screen's "no matches" line, which is only what it says
    when the sources are *configured* — so they passed on a machine with a populated
    `~/.config/rdt/.env` and failed the moment they ran anywhere else. A test that depends on
    the machine it runs on is not a test, so the baseline is no credentials at all and a test
    that needs one says so (`conftest.with_keys`).
    """
    settings = Settings()
    present = sorted(
        alias
        for alias in config_file.SECRET_ALIASES
        if getattr(settings, field_name_for(alias) or "", None)
    )
    assert present == [], f"the suite is reading real credentials: {present}"


def test_the_env_chain_is_detached_from_the_machine() -> None:
    """...and the reason it cannot: neither `.env` in the chain exists under test."""
    assert not any(path.exists() for path in config.env_file_paths())
