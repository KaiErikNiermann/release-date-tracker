"""Tests for runtime configuration (display colors are user-overridable)."""

from __future__ import annotations

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
