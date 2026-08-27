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
from release_tracker.resolve import (
    best_estimates,
    commercial_anchor,
    earliest_confirmed_theatrical,
    earliest_premiere,
    outranked_manual,
)


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


def _channel_obs(
    channel: ReleaseChannel, when: date | None, precision: DatePrecision, *, provider: str
) -> ReleaseObservation:
    return ReleaseObservation(
        entity_id="game-x",
        channel=channel,
        region="WW",
        release_date=when,
        precision=precision,
        certainty=Certainty.ESTIMATED,
        source_tier=SourceTier.FIRST_PARTY_STORE,
        provider=provider,
        source_url=f"https://example.com/{provider}",
        fetched_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


def test_dateless_tba_row_suppressed_when_a_dated_estimate_exists() -> None:
    # IGDB "TBD" (no date) alongside a Steam quarter -> only the dated row surfaces
    ests = best_estimates(
        [
            _channel_obs(ReleaseChannel.PRIMARY, None, DatePrecision.TBA, provider="igdb"),
            _channel_obs(
                ReleaseChannel.STEAM, date(2026, 7, 1), DatePrecision.QUARTER, provider="steam"
            ),
        ]
    )
    assert [(e.channel, e.release_date) for e in ests] == [(ReleaseChannel.STEAM, date(2026, 7, 1))]


def test_tba_row_kept_when_no_date_exists_anywhere() -> None:
    # nothing dated -> the TBA is the only signal, so it must stay (don't blank the entity)
    ests = best_estimates(
        [_channel_obs(ReleaseChannel.PRIMARY, None, DatePrecision.TBA, provider="igdb")]
    )
    assert len(ests) == 1 and ests[0].release_date is None


def _platform_obs(platform: str, when: date) -> ReleaseObservation:
    return ReleaseObservation(
        entity_id="game-x",
        channel=ReleaseChannel.PRIMARY,
        region="WW",
        contingencies={"platform": platform},
        release_date=when,
        precision=DatePrecision.EXACT,
        certainty=Certainty.CONFIRMED,
        source_tier=SourceTier.FIRST_PARTY_STORE,
        fetched_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


def test_facet_tagged_rows_stay_distinct_estimates() -> None:
    # a PS5 date and a PC date on one entity are different availabilities the user OR-s
    # over -> they must NOT collapse into one slot.
    ests = best_estimates(
        [_platform_obs("ps5", date(2026, 3, 1)), _platform_obs("pc", date(2026, 9, 1))]
    )
    by_platform = {e.contingencies.get("platform"): e.release_date for e in ests}
    assert by_platform == {"ps5": date(2026, 3, 1), "pc": date(2026, 9, 1)}


def _theatrical_obs(
    when: date,
    channel: ReleaseChannel,
    *,
    region: str = "WW",
    certainty: Certainty = Certainty.CONFIRMED,
) -> ReleaseObservation:
    return ReleaseObservation(
        entity_id="movie-x",
        channel=channel,
        region=region,
        release_date=when,
        precision=DatePrecision.EXACT,
        certainty=certainty,
        source_tier=SourceTier.AGGREGATOR,
        provider="tmdb",
        fetched_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


def test_commercial_anchor_excludes_premiere() -> None:
    # a festival premiere must NOT anchor the home-video clock — only a commercial release does.
    only_premiere = [_theatrical_obs(date(2026, 5, 17), ReleaseChannel.PREMIERE, region="FR")]
    assert commercial_anchor(only_premiere) is None


def test_commercial_anchor_prefers_wide_then_limited() -> None:
    obs = [
        _theatrical_obs(date(2026, 9, 9), ReleaseChannel.THEATRICAL),
        _theatrical_obs(date(2026, 8, 1), ReleaseChannel.THEATRICAL_LIMITED),  # earlier, limited
    ]
    anchor = commercial_anchor(obs)
    # wide beats an earlier limited release
    assert anchor is not None and anchor.channel is ReleaseChannel.THEATRICAL


def test_commercial_anchor_prefers_domestic_region() -> None:
    # the digital-window table is US-calibrated: anchor on the US wide release, not an earlier
    # foreign one, so a staggered rollout doesn't land the digital estimate far too early.
    obs = [
        _theatrical_obs(date(2026, 7, 15), ReleaseChannel.THEATRICAL, region="KR"),  # earlier intl
        _theatrical_obs(date(2026, 9, 9), ReleaseChannel.THEATRICAL, region="US"),
    ]
    anchor = commercial_anchor(obs)
    assert anchor is not None and anchor.region == "US" and anchor.release_date == date(2026, 9, 9)


def test_commercial_anchor_confirmed_only_filters() -> None:
    rumored = Certainty.RUMORED
    obs = [_theatrical_obs(date(2026, 9, 9), ReleaseChannel.THEATRICAL, certainty=rumored)]
    assert commercial_anchor(obs, confirmed_only=True) is None  # rumored excluded on persist path
    assert commercial_anchor(obs) is not None  # default accepts any certainty (the live /rd path)


def test_earliest_premiere_picks_soonest() -> None:
    obs = [
        _theatrical_obs(date(2026, 8, 30), ReleaseChannel.PREMIERE, region="IT"),  # Venice
        _theatrical_obs(date(2026, 5, 17), ReleaseChannel.PREMIERE, region="FR"),  # Cannes, earlier
        _theatrical_obs(date(2026, 9, 9), ReleaseChannel.THEATRICAL, region="US"),  # not a premiere
    ]
    prem = earliest_premiere(obs)
    assert prem is not None and prem.region == "FR" and prem.release_date == date(2026, 5, 17)


def test_earliest_confirmed_theatrical_is_earliest_anywhere() -> None:
    # the VOD-collision floor is the soonest commercial in-cinema date across ALL regions (not the
    # US-preferred anchor), so a legit foreign VOD following an earlier foreign theatrical survives.
    obs = [
        _theatrical_obs(date(2026, 7, 15), ReleaseChannel.THEATRICAL, region="KR"),  # earliest
        _theatrical_obs(date(2026, 9, 9), ReleaseChannel.THEATRICAL, region="US"),
    ]
    assert earliest_confirmed_theatrical(obs) == date(2026, 7, 15)


def test_earliest_confirmed_theatrical_excludes_premiere_and_unconfirmed() -> None:
    obs = [
        _theatrical_obs(date(2026, 5, 17), ReleaseChannel.PREMIERE, region="FR"),  # not commercial
        _theatrical_obs(
            date(2026, 8, 1), ReleaseChannel.THEATRICAL, certainty=Certainty.RUMORED
        ),  # not committed
        _theatrical_obs(date(2026, 9, 9), ReleaseChannel.THEATRICAL_LIMITED),  # earliest confirmed
    ]
    assert earliest_confirmed_theatrical(obs) == date(2026, 9, 9)


def test_earliest_confirmed_theatrical_none_without_commercial_date() -> None:
    only_premiere = [_theatrical_obs(date(2026, 5, 17), ReleaseChannel.PREMIERE, region="FR")]
    assert earliest_confirmed_theatrical(only_premiere) is None


# --- why a hand-authored date isn't the one being shown -----------------------------------
def test_a_hand_authored_date_outranked_on_precision_says_so() -> None:
    """The common case, and the confusing one. Nothing overwrote the typed quarter — a pull
    only clears the providers that answered, and no source is called `manual` — but the
    finer pulled date wins the ranking, and meeting that silently is what the reason is for.
    """
    reasons = outranked_manual(
        [
            _obs(
                date(2026, 10, 1),
                DatePrecision.QUARTER,
                Certainty.CONFIRMED,
                SourceTier.OFFICIAL,
                provider="manual",
            ),
            _obs(
                date(2026, 10, 15),
                DatePrecision.EXACT,
                Certainty.CONFIRMED,
                SourceTier.OFFICIAL,
                provider="tmdb",
            ),
        ],
        "manual",
    )
    assert reasons == {ReleaseChannel.PRIMARY: "more precise"}


def test_a_hand_authored_date_that_wins_needs_no_explanation() -> None:
    reasons = outranked_manual(
        [
            _obs(
                date(2026, 10, 15),
                DatePrecision.EXACT,
                Certainty.CONFIRMED,
                SourceTier.OFFICIAL,
                provider="manual",
            ),
            _obs(
                date(2026, 10, 1),
                DatePrecision.QUARTER,
                Certainty.CONFIRMED,
                SourceTier.OFFICIAL,
                provider="tmdb",
            ),
        ],
        "manual",
    )
    assert reasons == {}


def test_a_channel_with_no_hand_authored_date_is_not_explained() -> None:
    """There is nothing to be confused about when you never typed one."""
    reasons = outranked_manual(
        [
            _obs(
                date(2026, 10, 15),
                DatePrecision.EXACT,
                Certainty.CONFIRMED,
                SourceTier.OFFICIAL,
                provider="tmdb",
            )
        ],
        "manual",
    )
    assert reasons == {}


def test_an_unconfirmed_hand_authored_date_loses_on_certainty_first() -> None:
    """Certainty is the first component of the key, so it is named before precision even
    when both differ — the explanation walks the key in the key's own order."""
    reasons = outranked_manual(
        [
            _obs(
                date(2026, 10, 15),
                DatePrecision.EXACT,
                Certainty.RUMORED,
                SourceTier.OFFICIAL,
                provider="manual",
            ),
            _obs(
                date(2026, 10, 1),
                DatePrecision.QUARTER,
                Certainty.CONFIRMED,
                SourceTier.OFFICIAL,
                provider="tmdb",
            ),
        ],
        "manual",
    )
    assert reasons == {ReleaseChannel.PRIMARY: "theirs is confirmed"}
