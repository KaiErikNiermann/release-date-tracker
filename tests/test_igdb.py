"""Tests for the IGDB row -> observation conversion (precision-derived certainty)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from release_tracker.models import Certainty, DatePrecision, Entity, MediaKind
from release_tracker.sources.igdb import row_to_observation

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
