"""Tests for the Steam release-date block -> observation (precision-derived certainty)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from release_tracker.models import Certainty, DatePrecision, Entity, MediaKind
from release_tracker.sources.steam import date_observation

_NOW = datetime(2026, 6, 20, tzinfo=UTC)
_URL = "https://store.steampowered.com/app/1"


def _entity() -> Entity:
    return Entity.create("Ontos", MediaKind.GAME)


def _block(text: str, *, coming_soon: bool) -> dict[str, Any]:
    return {"date": text, "coming_soon": coming_soon}


def test_an_announced_day_is_confirmed_even_though_it_has_not_shipped() -> None:
    """Regression: `coming_soon` is set on everything unreleased, so reading it as "the
    date is a guess" filed every announced day for an upcoming game as speculation — and
    a confirmed date scored as one (0.49 rather than 0.74)."""
    obs = date_observation(_block("Sep 24, 2026", coming_soon=True), _entity(), _URL, _NOW)
    assert obs is not None
    assert obs.release_date == date(2026, 9, 24)
    assert obs.precision is DatePrecision.EXACT
    assert obs.certainty is Certainty.CONFIRMED


@pytest.mark.parametrize(
    ("text", "when", "precision"),
    [
        ("Q3 2026", date(2026, 7, 1), DatePrecision.QUARTER),
        ("September 2026", date(2026, 9, 1), DatePrecision.MONTH),
        ("2026", date(2026, 1, 1), DatePrecision.YEAR),
    ],
)
def test_a_window_is_only_an_estimate(text: str, when: date, precision: DatePrecision) -> None:
    obs = date_observation(_block(text, coming_soon=True), _entity(), _URL, _NOW)
    assert obs is not None
    assert (obs.release_date, obs.precision) == (when, precision)
    assert obs.certainty is Certainty.ESTIMATED


def test_a_shipped_game_is_confirmed_whatever_the_precision() -> None:
    obs = date_observation(_block("2019", coming_soon=False), _entity(), _URL, _NOW)
    assert obs is not None
    assert obs.certainty is Certainty.CONFIRMED


@pytest.mark.parametrize("text", ["Coming soon", "TBA", "To be announced", ""])
def test_a_dateless_block_yields_nothing(text: str) -> None:
    assert date_observation(_block(text, coming_soon=True), _entity(), _URL, _NOW) is None


def test_the_quote_keeps_what_steam_actually_said() -> None:
    obs = date_observation(_block("Sep 24, 2026", coming_soon=True), _entity(), _URL, _NOW)
    assert obs is not None and obs.source_quote == "Sep 24, 2026"
