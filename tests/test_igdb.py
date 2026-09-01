"""Tests for what one IGDB row becomes: an observation, a stance, or a sentence.

The conversion half pins precision-derived certainty. The tail pins the release-state
signal — which is the one place IGDB says more than a date, and the one place it can be
wrong in a way that invents a date (a cancelled row still carries one).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from release_tracker.models import Certainty, DatePrecision, Entity, MediaKind, Stance
from release_tracker.sources.igdb import GAME_STANCE, row_to_observation, status_notes

_NOW = datetime(2026, 6, 20, tzinfo=UTC)


def _entity() -> Entity:
    return Entity.create("Ontos", MediaKind.GAME)


def _ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


def _row(category: int, when: date | None, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"category": category}
    if when is not None:
        row["date"] = _ts(when)
    return row | extra


def test_exact_date_is_confirmed() -> None:
    obs = row_to_observation(_row(0, date(2026, 3, 15)), _entity(), 381219, _NOW)
    assert obs is not None
    assert obs.precision is DatePrecision.EXACT
    assert obs.certainty is Certainty.CONFIRMED
    assert obs.release_date == date(2026, 3, 15)


def test_year_precision_is_estimated_not_confirmed() -> None:
    # the ONTOS bug: a vague "2026" must not be a confirmed concrete date.
    obs = row_to_observation(_row(2, date(2026, 1, 1)), _entity(), 381219, _NOW)
    assert obs is not None
    assert obs.precision is DatePrecision.YEAR
    assert obs.certainty is Certainty.ESTIMATED


def test_quarter_precision_is_estimated() -> None:
    obs = row_to_observation(_row(5, date(2026, 7, 1)), _entity(), 381219, _NOW)
    assert obs is not None
    assert obs.precision is DatePrecision.QUARTER
    assert obs.certainty is Certainty.ESTIMATED


def test_tbd_with_placeholder_date_becomes_coarse_year_at_period_start() -> None:
    # IGDB "TBD" (category 7) carrying a Dec-31 placeholder -> coarse YEAR @ Jan 1,
    # so it reads as the vague year it is (and is narrowable), not a precise Dec-31.
    obs = row_to_observation(_row(7, date(2026, 12, 31)), _entity(), 381219, _NOW)
    assert obs is not None
    assert obs.precision is DatePrecision.YEAR
    assert obs.release_date == date(2026, 1, 1)
    assert obs.certainty is Certainty.ESTIMATED


def test_tbd_without_date_yields_a_dateless_observation() -> None:
    obs = row_to_observation(_row(7, None), _entity(), 381219, _NOW)
    assert obs is not None
    assert obs.release_date is None
    assert obs.precision is DatePrecision.TBA


def test_non_tba_row_without_a_date_is_dropped() -> None:
    assert row_to_observation(_row(2, None), _entity(), 381219, _NOW) is None


# --- what IGDB says about whether a game is coming at all ---------------------------------
# Every fixture below is the real IGDB record, because the whole point is that the two halves
# of the signal disagree in practice: Prey 2 is Cancelled at the game level *and* on every
# release row, while Silksong carries no game-level status at all.
def _dev(name: str, status: int, change: date | None = None) -> dict[str, Any]:
    company: dict[str, Any] = {"name": name, "status": status}
    if change is not None:
        company["change_date"] = _ts(change)
    return {"developer": True, "company": company}


def _game(
    status: int | None = None, *, developers: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    game: dict[str, Any] = {"name": "Ontos", "slug": "ontos"}
    if status is not None:
        game["game_status"] = status
    if developers is not None:
        game["involved_companies"] = developers
    return game


def test_a_cancelled_release_row_yields_no_observation() -> None:
    """Prey 2's three rows are all "cancelled for this platform" and all dated 2014-10-30 —
    the day it would have landed. They currently mint three observations claiming a 2014
    release for a game that has none."""
    row = _row(7, date(2014, 10, 30), status=5, platform={"name": "PlayStation 3"})
    assert row_to_observation(row, _entity(), 525, _NOW) is None


def test_an_ordinary_row_is_untouched_by_the_cancellation_check() -> None:
    """`status` is absent far more often than it is set; absence must stay a release."""
    obs = row_to_observation(_row(0, date(2026, 3, 15)), _entity(), 381219, _NOW)
    assert obs is not None
    obs = row_to_observation(_row(0, date(2026, 3, 15), status=6), _entity(), 381219, _NOW)
    assert obs is not None  # 6 is "Full Release" on this enum, not cancelled


def test_the_game_level_status_becomes_a_stance() -> None:
    assert GAME_STANCE[6] is Stance.SHELVED  # Cancelled — Scalebound, Prey 2, Star Wars 1313
    assert GAME_STANCE[7] is Stance.UNCERTAIN  # Rumored — Half-Life 3
    assert GAME_STANCE[0] is Stance.RELEASED


def test_a_shipped_game_takes_no_stance_at_all() -> None:
    """The field is an exception flag, not a state machine: Fallout 4, Hollow Knight and New
    Vegas all read None. Absent must not become UNKNOWN, which would be a claim."""
    for shipped in (2, 3, 4, 5, 8):  # alpha, beta, early access, offline, delisted
        assert shipped not in GAME_STANCE


@pytest.mark.parametrize(
    ("status", "fragment"),
    [(4, "Early Access"), (6, "Cancelled"), (7, "Rumored"), (8, "Delisted")],
)
def test_every_status_word_is_quoted_back(status: int, fragment: str) -> None:
    """IGDB's own word, not a paraphrase — including for the ones that take no stance."""
    said = status_notes(status, [], [], date(2026, 9, 1))
    assert any(fragment in note for note in said)


def test_a_cancelled_release_is_said_and_says_where() -> None:
    rows = [
        _row(7, None, status=5, platform={"name": "PlayStation 3"}, game=_game(6)),
        _row(7, None, status=5, platform={"name": "Xbox 360"}, game=_game(6)),
    ]
    said = " ".join(status_notes(6, rows, [], date(2026, 9, 1)))
    assert "every release it lists" in said
    assert "PlayStation 3" in said and "Xbox 360" in said


def test_a_partial_cancellation_does_not_claim_to_be_total() -> None:
    """Cancelled on one platform and shipped on another is an ordinary thing to be."""
    rows = [
        _row(7, None, status=5, platform={"name": "Wii U"}, game=_game()),
        _row(0, date(2026, 3, 1), platform={"name": "PC"}, game=_game()),
    ]
    said = " ".join(status_notes(None, rows, [], date(2026, 9, 1)))
    assert "cancelled" in said
    assert "every release" not in said


# --- the defunct-studio proxy: context for a wait, never a verdict -------------------------
def test_a_defunct_developer_is_named_while_the_game_is_still_awaited() -> None:
    """Prey 2's Human Head Studios reads defunct as of 2019-11-13 — which is why nothing has
    moved since. It explains the wait; it does not end it."""
    dev = _dev("Human Head Studios", 1, date(2019, 11, 13))
    rows = [_row(7, None, status=5, game=_game(6, developers=[dev]))]
    said = " ".join(status_notes(6, rows, [], date(2026, 9, 1)))
    assert "Human Head Studios" in said
    assert "2019-11-13" in said


def test_a_defunct_developer_is_not_mentioned_once_the_game_shipped() -> None:
    """Telltale shut down and The Walking Dead's final season still came out. For a game you
    can buy, the studio's death is trivia, and printing it implies a doubt that is not there."""
    dev = _dev("Telltale Games", 1, date(2018, 10, 11))
    rows = [_row(0, date(2019, 3, 26), game=_game(developers=[dev]))]
    shipped = row_to_observation(rows[0], _entity(), 525, _NOW)
    assert shipped is not None
    assert status_notes(None, rows, [shipped], date(2026, 9, 1)) == ()


def test_an_active_developer_is_never_mentioned() -> None:
    rows = [_row(7, None, game=_game(7, developers=[_dev("Valve", 0)]))]
    said = " ".join(status_notes(7, rows, [], date(2026, 9, 1)))
    assert "Valve" not in said
    assert "Rumored" in said  # the game-level word still stands
