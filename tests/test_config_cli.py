"""`rdt doctor` and `rdt config` — the headless half of key management.

Doctor exists to be pasted into a bug report, so the thing worth pinning is that it says
what is missing and what that costs without ever printing a usable credential.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from release_tracker import cli, config, config_file
from release_tracker.config import get_settings

runner = CliRunner()


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


@pytest.fixture
def no_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No environment and no .env, so only the config file is in play."""
    absent = tmp_path / "nowhere" / ".env"
    # `Settings` resolves the chain through `config.env_file_paths` at load time, so that
    # is the one that has to be patched — the re-exports elsewhere only affect reporting.
    monkeypatch.setattr(config, "env_file_paths", lambda: (absent, absent))
    monkeypatch.setattr(config_file, "env_file_paths", lambda: (absent, absent))
    monkeypatch.setattr(cli, "env_file_paths", lambda: (absent, absent))
    for alias in config_file.SECRET_ALIASES:
        monkeypatch.delenv(alias, raising=False)
    get_settings.cache_clear()


def test_doctor_names_what_is_missing_and_what_it_costs(no_env: None) -> None:
    del no_env
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 0
    out = _plain(result.output)
    assert "TMDB_API_KEY" in out
    assert "no movie or TV dates" in out, "a missing key has to say what it costs"


def test_doctor_never_prints_a_usable_credential(
    isolated_config: Path, no_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is meant to be pasted into an issue."""
    del no_env
    monkeypatch.setenv("TMDB_API_KEY", "abcdef0123456789")
    out = _plain(runner.invoke(cli.app, ["doctor"]).output)
    assert "abcdef0123456789" not in out
    assert "abcd" in out, "...but enough to recognise which key it is"


def test_set_then_show_round_trips(isolated_config: Path, no_env: None) -> None:
    del no_env
    assert runner.invoke(cli.app, ["config", "set", "RDT_FRESH_DAYS=9"]).exit_code == 0
    out = _plain(runner.invoke(cli.app, ["config", "show"]).output)
    assert "RDT_FRESH_DAYS" in out
    assert "config.toml" in out


def test_set_warns_when_the_environment_will_win(
    isolated_config: Path, no_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The most confusing outcome available: a correct write that appears to do nothing
    because a leftover variable outranks it."""
    del no_env
    monkeypatch.setenv("TMDB_API_KEY", "from-env")
    out = _plain(runner.invoke(cli.app, ["config", "set", "TMDB_API_KEY=from-file"]).output)
    assert "which wins" in out


def test_an_unknown_setting_is_refused(isolated_config: Path, no_env: None) -> None:
    del no_env
    result = runner.invoke(cli.app, ["config", "set", "NOT_A_SETTING=x"])
    assert result.exit_code == 1
    assert "not a setting" in _plain(result.output)


def test_a_bad_value_is_refused_before_the_file_is_touched(
    isolated_config: Path, no_env: None
) -> None:
    del no_env
    result = runner.invoke(cli.app, ["config", "set", "RDT_AVAILABILITY_CHANNEL=telepathy"])
    assert result.exit_code == 1
    assert not isolated_config.exists()


def test_unset_removes_only_that_key(isolated_config: Path, no_env: None) -> None:
    del no_env
    runner.invoke(cli.app, ["config", "set", "TMDB_API_KEY=abc", "RDT_FRESH_DAYS=9"])
    assert runner.invoke(cli.app, ["config", "unset", "TMDB_API_KEY"]).exit_code == 0
    assert set(config_file.read_config(isolated_config)) == {"RDT_FRESH_DAYS"}


def test_config_path_works_even_when_the_file_is_broken(isolated_config: Path) -> None:
    """The command you would reach for to find the file you need to fix must not itself
    need that file to parse."""
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text('TMDB_API_KEY = "unterminated\n')
    result = runner.invoke(cli.app, ["config", "path"])
    assert result.exit_code == 0
    assert str(isolated_config) in _plain(result.output)
