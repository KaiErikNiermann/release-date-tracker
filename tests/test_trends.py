"""Tests for studio release-timing trend mining + staged coarse narrowing (pure)."""

from __future__ import annotations

from datetime import date

from release_tracker.models import DatePrecision, MediaKind
from release_tracker.trends import (
    MIN_SAMPLES,
    Season,
    StudioTrend,
    compute_trend,
    narrow_coarse,
)


def _trend(months: tuple[int, ...], name: str = "Acme") -> StudioTrend:
    return compute_trend("igdb:1", name, MediaKind.GAME, months)


def test_compute_trend_empty() -> None:
    t = _trend(())
    assert t.n == 0
    assert t.month_mode is None
    assert t.month_concentration == 0.0
    assert t.season_mode is None


def test_compute_trend_tight_month_high_concentration() -> None:
    t = _trend((10, 10, 10, 11))  # almost always October
    assert t.month_mode == 10
    assert t.month_concentration > 0.95
    assert t.season_mode is Season.FALL


def test_compute_trend_is_circular_dec_jan() -> None:
    # December/January studio: a plain mean would say ~June; circular says ~Dec/Jan.
    t = _trend((12, 12, 1, 1))
    assert t.month_mode in (12, 1)
    assert t.month_concentration > 0.95
    assert t.season_mode is Season.WINTER


def test_compute_trend_season_without_tight_month() -> None:
    t = _trend((9, 10, 11, 9, 10, 11))  # spread evenly across all of fall
    assert t.season_mode is Season.FALL
    assert t.season_concentration == 1.0
    # evenly across three months -> not tight enough for the month stage
    assert t.month_concentration < 0.93


def test_narrow_needs_min_samples() -> None:
    t = _trend((10,) * (MIN_SAMPLES - 1))
    assert narrow_coarse(date(2027, 1, 1), DatePrecision.YEAR, t) is None


def test_narrow_year_to_month_when_tight() -> None:
    t = _trend((10, 10, 10, 10, 11))  # tight October
    est = narrow_coarse(date(2027, 1, 1), DatePrecision.YEAR, t)
    assert est is not None
    assert est.when == date(2027, 10, 15)
    assert est.margin_days == 14
    assert est.confidence == 0.55


def test_narrow_year_to_season_when_only_seasonal() -> None:
    t = _trend((9, 10, 11, 9, 10, 11))  # all over fall, no single month
    est = narrow_coarse(date(2027, 1, 1), DatePrecision.YEAR, t)
    assert est is not None
    assert est.when.month in (9, 10, 11)
    assert est.margin_days == 45  # season-width, wider than the month stage
    assert est.confidence == 0.45


def test_narrow_skips_when_pattern_conflicts_with_quarter() -> None:
    # studio always ships in October, but the stated quarter is Q1 (Jan-Mar):
    # the harder data wins, no narrowing.
    t = _trend((10, 10, 10, 10))
    assert narrow_coarse(date(2027, 1, 1), DatePrecision.QUARTER, t) is None


def test_narrow_quarter_to_month_inside_bound() -> None:
    t = _trend((11, 11, 11, 12))  # November-ish, Q4
    est = narrow_coarse(date(2027, 10, 1), DatePrecision.QUARTER, t)
    assert est is not None
    assert est.when == date(2027, 11, 15)


def test_narrow_returns_none_for_already_precise() -> None:
    t = _trend((10, 10, 10, 10))
    assert narrow_coarse(date(2027, 10, 1), DatePrecision.MONTH, t) is None
    assert narrow_coarse(date(2027, 10, 5), DatePrecision.EXACT, t) is None
