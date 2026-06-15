"""Tests for the speculative-date estimators (pure functions)."""

from __future__ import annotations

from datetime import date

from release_tracker.deltas import (
    DEFAULT_DIGITAL_DELTA_DAYS,
    digital_delta_days,
    estimate_digital,
    match_studio,
    precise_from_coarse,
    predicted_platform,
    studio_platform,
)
from release_tracker.models import DatePrecision


def test_digital_delta_matches_studio_substring() -> None:
    days, key = digital_delta_days("Warner Bros. Pictures")
    assert (days, key) == (45, "warner bros")
    assert digital_delta_days("Universal Pictures") == (31, "universal")


def test_digital_delta_falls_back_to_default() -> None:
    days, key = digital_delta_days("Some Indie Co")
    assert days == DEFAULT_DIGITAL_DELTA_DAYS
    assert key is None
    assert digital_delta_days(None) == (DEFAULT_DIGITAL_DELTA_DAYS, None)


def test_match_studio_prefers_known_over_lead() -> None:
    # TMDB lists the lead production co first; we want the known distributor.
    assert match_studio(["6th & Idaho", "DC Studios", "Dylan Clark"]) == "DC Studios"
    # nothing known -> fall back to the first listed company
    assert match_studio(["Indie A", "Indie B"]) == "Indie A"
    assert match_studio([]) is None


def test_estimate_digital_known_studio_is_more_confident() -> None:
    theatrical = date(2027, 9, 30)
    known = estimate_digital(theatrical, "Warner Bros.")
    unknown = estimate_digital(theatrical, "Tiny Studio")
    assert known.when == date(2027, 11, 14)  # +45d
    assert known.confidence > unknown.confidence
    assert known.margin_days < unknown.margin_days


def test_estimate_digital_guessed_theatrical_is_penalised() -> None:
    base = estimate_digital(date(2027, 1, 1), "A24")
    guessed = estimate_digital(date(2027, 1, 1), "A24", theatrical_confirmed=False)
    assert guessed.confidence < base.confidence
    assert guessed.margin_days > base.margin_days
    assert guessed.basis.startswith("guessed")


def test_studio_platform_maps_distributor_to_service() -> None:
    assert studio_platform("Walt Disney Pictures") == "Disney+"
    assert studio_platform("Warner Bros. Pictures") == "HBO Max"
    assert studio_platform("Universal Pictures") == "Peacock"
    assert studio_platform("Some Indie Co") is None
    assert studio_platform(None) is None


def test_predicted_platform_scans_all_companies() -> None:
    # lead production co is unknown; a later one (DC Studios) carries the mapping.
    assert predicted_platform(["6th & Idaho", "DC Studios"]) == "HBO Max"
    assert predicted_platform(["Indie A", "Indie B"]) is None
    assert predicted_platform([]) is None


def test_precise_from_coarse_collapses_to_midpoint() -> None:
    assert precise_from_coarse(date(2026, 1, 1), DatePrecision.EXACT).margin_days == 0
    q = precise_from_coarse(date(2026, 7, 1), DatePrecision.QUARTER)  # Q3 start
    assert q.when == date(2026, 8, 15) and q.margin_days == 45
    y = precise_from_coarse(date(2026, 1, 1), DatePrecision.YEAR)
    assert y.when.year == 2026 and y.margin_days == 181
    assert precise_from_coarse(date(2026, 1, 1), DatePrecision.TBA).confidence < 0.3
