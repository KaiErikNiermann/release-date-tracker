"""Tests for observation -> best-estimate collapse (precision-aware ranking)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from release_tracker.models import (
    Certainty,
    DatePrecision,
    ReleaseChannel,
    ReleaseObservation,
    SourceTier,
)
from release_tracker.resolve import best_estimates


def _obs(
    when: date,
    precision: DatePrecision,
    certainty: Certainty,
    tier: SourceTier,
    *,
    provider: str = "src",
    fetched: datetime | None = None,
) -> ReleaseObservation:
    return ReleaseObservation(
        entity_id="game-x",
        channel=ReleaseChannel.PRIMARY,
        region="WW",
        release_date=when,
        precision=precision,
        certainty=certainty,
        source_tier=tier,
        provider=provider,
        source_url=f"https://example.com/{provider}",
        fetched_at=fetched or datetime(2026, 6, 1, tzinfo=UTC),
    )


def test_precise_estimate_beats_higher_tier_coarse() -> None:
    # a curated month ("Oct 2026") must win over an aggregator's vague year ("2026"),
    # even though the year comes from a higher-trust tier — the ONTOS pivot bug.
    month = _obs(date(2026, 10, 6), DatePrecision.MONTH, Certainty.ESTIMATED, SourceTier.RUMOR)
    year = _obs(date(2026, 1, 1), DatePrecision.YEAR, Certainty.ESTIMATED, SourceTier.AGGREGATOR)
    (est,) = best_estimates([year, month])
    assert est.release_date == date(2026, 10, 6)
    assert est.precision is DatePrecision.MONTH


def test_confirmed_beats_a_more_precise_estimate() -> None:
    confirmed = _obs(
        date(2026, 12, 31), DatePrecision.MONTH, Certainty.CONFIRMED, SourceTier.OFFICIAL
    )
    precise = _obs(date(2026, 3, 15), DatePrecision.EXACT, Certainty.ESTIMATED, SourceTier.RUMOR)
    (est,) = best_estimates([precise, confirmed])
    assert est.certainty is Certainty.CONFIRMED
    assert est.release_date == date(2026, 12, 31)


def test_fresher_fetch_breaks_ties_among_equal_standing() -> None:
    # two equally-ranked manual dates (same precision/certainty/tier) -> the freshly
    # re-curated one supersedes the stale one.
    old = _obs(
        date(2026, 9, 22),
        DatePrecision.MONTH,
        Certainty.ESTIMATED,
        SourceTier.RUMOR,
        provider="notion-old",
        fetched=datetime(2026, 6, 1, tzinfo=UTC),
    )
    new = _obs(
        date(2026, 10, 6),
        DatePrecision.MONTH,
        Certainty.ESTIMATED,
        SourceTier.RUMOR,
        provider="notion-new",
        fetched=datetime(2026, 6, 19, tzinfo=UTC),
    )
    (est,) = best_estimates([old, new])
    assert est.release_date == date(2026, 10, 6)
