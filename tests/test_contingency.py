"""Tests for the pure contingency-resolution layer (matcher + three-valued combine)."""

from __future__ import annotations

from datetime import date

from release_tracker.contingency import (
    ProfileMatcher,
    Resolution,
    ResolutionStatus,
    combine,
    estimate_resolution,
)
from release_tracker.models import BestEstimate, Certainty, DatePrecision, ReleaseChannel


def _matcher(**dims: set[str]) -> ProfileMatcher:
    return ProfileMatcher({k: frozenset(v) for k, v in dims.items()})


def test_matcher_absent_dim_is_a_wildcard() -> None:
    # I constrain platform; an observation with no platform facet still matches (wildcard)
    m = _matcher(platform={"pc"})
    assert m.matches({"region": "US"}) is True
    assert m.matches({"region": "US", "platform": "pc"}) is True
    assert m.matches({"region": "US", "platform": "ps5"}) is False  # constrained + mismatch


def test_matcher_or_within_dim_and_across_dims() -> None:
    m = _matcher(platform={"pc", "ps5"}, region={"US", "WW"})
    assert m.matches({"platform": "ps5", "region": "US"}) is True  # OR within platform
    assert m.matches({"platform": "switch", "region": "US"}) is False  # not an accepted platform
    assert m.matches({"platform": "pc", "region": "JP"}) is False  # region fails the AND


def test_empty_matcher_accepts_everything() -> None:
    assert ProfileMatcher({}).matches({"platform": "ps5", "region": "JP"}) is True


def _est(when: date | None, certainty: Certainty) -> BestEstimate:
    return BestEstimate(
        entity_id="x",
        channel=ReleaseChannel.DIGITAL,
        region="US",
        release_date=when,
        precision=DatePrecision.EXACT if when else DatePrecision.TBA,
        certainty=certainty,
    )


def test_estimate_resolution_three_valued() -> None:
    assert estimate_resolution(None).status is ResolutionStatus.PENDING  # nothing for my profile
    confirmed = estimate_resolution(_est(date(2026, 9, 1), Certainty.CONFIRMED))
    assert confirmed.status is ResolutionStatus.RESOLVED and confirmed.when == date(2026, 9, 1)
    spec = estimate_resolution(_est(date(2026, 9, 1), Certainty.ESTIMATED))
    assert spec.status is ResolutionStatus.PENDING  # speculative date doesn't gate availability


def test_combine_never_dominates_then_pending_then_max() -> None:
    resolved, pending, never = (
        ResolutionStatus.RESOLVED,
        ResolutionStatus.PENDING,
        ResolutionStatus.NEVER,
    )
    early = Resolution(resolved, date(2026, 3, 1))
    late = Resolution(resolved, date(2027, 1, 1))
    # all resolved -> the LAST contingency to clear (max) governs
    assert combine([early, late]) == Resolution(resolved, date(2027, 1, 1))
    # a pending drags the whole thing to pending (you're still waiting on something)
    assert combine([early, Resolution(pending, blocker="PC port TBD")]).status is pending
    # a never dominates everything (the "if, not when" case)
    blocked = Resolution(never, blocker="needs Windows")
    assert combine([early, late, Resolution(pending), blocked]) == blocked
