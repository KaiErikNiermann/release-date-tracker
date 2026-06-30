"""Tests for the JustWatch offer source (pure parsing/derivation; fetch is best-effort HTTP)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from release_tracker.lookup import justwatch_predates_theatrical, justwatch_year_mismatch
from release_tracker.models import (
    Certainty,
    DatePrecision,
    ReleaseChannel,
    ReleaseObservation,
    SourceTier,
)
from release_tracker.sources.justwatch import (
    JustWatchAvailability,
    Offer,
    dedupe,
    earliest_vod,
    parse_from_time,
    parse_offers,
    parse_price,
    pick_node,
    title_match_score,
)

# one JustWatch title node, as the GraphQL hands it back (prices are localized strings).
_NODE: dict[str, Any] = {
    "objectId": 307535,
    "objectType": "MOVIE",
    "content": {"title": "Nosferatu", "originalReleaseYear": 2024},
    "offers": [
        {
            "monetizationType": "RENT",
            "presentationType": "_4K",
            "retailPrice": "$3.99",
            "currency": "USD",
            "availableFromTime": "2024-12-25T08:00:00Z",
            "package": {"clearName": "Apple TV Store"},
        },
        {  # a buy with no surfaced price/date — still a real availability
            "monetizationType": "BUY",
            "presentationType": "HD",
            "retailPrice": None,
            "currency": "USD",
            "availableFromTime": None,
            "package": {"clearName": "Amazon Video"},
        },
        {  # subscription home — flatrate, not a VOD buy/rent
            "monetizationType": "FLATRATE",
            "presentationType": "HD",
            "retailPrice": None,
            "currency": "USD",
            "availableFromTime": "2026-04-21T10:00:00Z",
            "package": {"clearName": "Peacock Premium"},
        },
        {"monetizationType": "BUY", "package": {}},  # no platform -> dropped
    ],
}


def test_parse_price_handles_symbol_and_locales() -> None:
    assert parse_price("$3.99") == 3.99
    assert parse_price("3,99 €") == 3.99  # comma-decimal locale
    assert parse_price("R$ 39,90") == 39.90
    assert parse_price("1.299,00 kr") == 1299.00  # dot thousands + comma decimal
    assert parse_price(4.99) == 4.99
    assert parse_price(None) is None
    assert parse_price("") is None
    assert parse_price("free") is None


def test_parse_from_time_decodes_iso_z() -> None:
    assert parse_from_time("2024-12-25T08:00:00Z") == date(2024, 12, 25)
    assert parse_from_time(None) is None
    assert parse_from_time("not-a-date") is None


def test_parse_offers_flattens_and_normalizes() -> None:
    offers = parse_offers(_NODE, "US")
    assert len(offers) == 3  # the platform-less offer is dropped
    rent = next(o for o in offers if o.monetization == "rent")
    assert rent.platform == "Apple TV Store"
    assert rent.presentation == "4k"  # leading underscore stripped, lowercased
    assert rent.price == 3.99
    assert rent.available_from == date(2024, 12, 25)
    # monetization is lowercased across the board
    assert {o.monetization for o in offers} == {"rent", "buy", "flatrate"}


def test_earliest_vod_ignores_flatrate_and_undated() -> None:
    offers = (
        Offer("US", "rent", "Apple TV Store", "hd", 3.99, "USD", date(2025, 1, 10)),
        Offer("AU", "buy", "Apple TV Store", "hd", 14.99, "AUD", date(2024, 12, 25)),
        Offer("US", "flatrate", "Peacock", "hd", None, "USD", date(2024, 1, 1)),  # earlier, not VOD
        Offer("GB", "rent", "Amazon", "hd", 3.49, "GBP", None),  # undated -> ignored
    )
    when, country, platform = earliest_vod(offers)
    assert when == date(2024, 12, 25)
    assert country == "AU"
    assert platform == "Apple TV Store"


def test_earliest_vod_empty_when_no_dated_vod() -> None:
    offers = (Offer("US", "flatrate", "Peacock", "hd", None, "USD", date(2024, 1, 1)),)
    assert earliest_vod(offers) == (None, None, None)


def test_dedupe_keeps_dated_then_cheapest() -> None:
    offers = [
        Offer("US", "buy", "Apple TV Store", "sd", 14.99, "USD", None),
        Offer("US", "buy", "Apple TV Store", "4k", 19.99, "USD", date(2024, 12, 25)),  # dated
        Offer("US", "buy", "Apple TV Store", "hd", 12.99, "USD", date(2024, 12, 25)),  # cheaper
    ]
    out = dedupe(offers)
    assert len(out) == 1
    assert out[0].price == 12.99 and out[0].available_from == date(2024, 12, 25)


def test_pick_node_filters_by_year_and_prefers_exact_title() -> None:
    edges = [
        {"node": {"objectId": 1, "content": {"title": "Nosferatu", "originalReleaseYear": 1922}}},
        {"node": {"objectId": 2, "content": {"title": "Nosferatu", "originalReleaseYear": 2024}}},
    ]
    picked = pick_node(edges, "Nosferatu", 2024)
    assert picked is not None
    node, _content = picked
    assert node["objectId"] == 2  # the year-matching one, not the 1922 silent film


def test_pick_node_none_when_year_off() -> None:
    edges = [{"node": {"objectId": 1, "content": {"title": "Wicked", "originalReleaseYear": 2024}}}]
    assert pick_node(edges, "Wicked", 2099) is None


def test_title_match_score_prefix_subtitle_vs_trailing_word() -> None:
    assert title_match_score("Nosferatu", "Nosferatu") == 2
    # a prefix abbreviation / subtitle is a partial match
    assert title_match_score("Wicked", "Wicked: Part I") == 1
    assert title_match_score("Nosferatu", "Nosferatu the Vampyre") == 1
    # a shared *trailing* word is NOT a match (the "Ghosts" collision)
    assert title_match_score("Anything but Ghosts", "Ghosts") == 0
    # word-boundary: a partial word never matches
    assert title_match_score("Wick", "Wicked") == 0
    # wholly unrelated
    assert title_match_score("Anything but Ghosts", "Absolutely Anything") == 0


def test_pick_node_rejects_zero_title_overlap_even_when_year_unknown() -> None:
    # the "Absolutely Anything" collision: fuzzy search returns an unrelated popular title; with the
    # film's year unknown the year filter can't reject it, so the title floor must.
    content = {"title": "Absolutely Anything", "originalReleaseYear": 2015}
    edges = [{"node": {"objectId": 9, "content": content}}]
    assert pick_node(edges, "Anything but Ghosts", None) is None


def test_availability_to_dict_omits_nothing_and_derives_streaming() -> None:
    av = JustWatchAvailability(
        object_id=1,
        title="X",
        year=2024,
        offers=tuple(parse_offers(_NODE, "US")),
        earliest_vod=date(2024, 12, 25),
        earliest_vod_country="US",
        earliest_vod_platform="Apple TV Store",
    )
    assert av.streaming_platforms == ("Peacock Premium",)
    assert av.countries == ("US",)
    d = av.to_dict()
    assert d["earliest_vod"] == "2024-12-25"
    assert d["streaming_platforms"] == ["Peacock Premium"]
    assert len(d["offers"]) == 3  # pyright: ignore[reportArgumentType]


def _theatrical(
    when: date, channel: ReleaseChannel = ReleaseChannel.THEATRICAL
) -> ReleaseObservation:
    return ReleaseObservation(
        entity_id="movie-x",
        channel=channel,
        region="US",
        release_date=when,
        precision=DatePrecision.EXACT,
        certainty=Certainty.CONFIRMED,
        source_tier=SourceTier.AGGREGATOR,
        provider="tmdb",
        fetched_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


def _avail(vod: date | None, year: int | None = 2026) -> JustWatchAvailability:
    return JustWatchAvailability(
        object_id=1,
        title="X",
        year=year,
        offers=(),
        earliest_vod=vod,
        earliest_vod_country="AU",
        earliest_vod_platform="Apple TV Store",
    )


def test_year_mismatch_flags_far_matched_year() -> None:
    # matched JustWatch title is 2015, the film is 2026 -> collision.
    assert justwatch_year_mismatch(_avail(None, year=2015), 2026) is not None


def test_year_mismatch_flags_ancient_vod_even_without_film_year() -> None:
    # the "Absolutely Anything" tell: a 2001 VOD on a match whose own year is 2015 (film undated).
    assert justwatch_year_mismatch(_avail(date(2001, 10, 26), year=2015), None) is not None


def test_year_mismatch_passes_a_sane_match() -> None:
    assert justwatch_year_mismatch(_avail(date(2026, 12, 2), year=2026), 2026) is None
    # VOD in the release year itself is fine (day-and-date / early-window)
    assert justwatch_year_mismatch(_avail(date(2026, 1, 1), year=2026), 2026) is None


def test_guard_flags_vod_before_theatrical() -> None:
    # the Tim-Dillon-special collision: a "digital" date months before the film's own theatrical.
    obs = [_theatrical(date(2026, 9, 25))]
    assert justwatch_predates_theatrical(_avail(date(2026, 4, 24)), obs) is True


def test_guard_allows_vod_on_or_after_theatrical() -> None:
    obs = [_theatrical(date(2026, 9, 25))]
    assert justwatch_predates_theatrical(_avail(date(2026, 12, 2)), obs) is False
    assert justwatch_predates_theatrical(_avail(date(2026, 9, 25)), obs) is False  # day-and-date


def test_guard_is_conservative_without_a_floor_or_vod() -> None:
    # no confirmed theatrical (premiere-only) -> can't judge -> keep the data
    premiere_only = [_theatrical(date(2026, 5, 17), ReleaseChannel.PREMIERE)]
    assert justwatch_predates_theatrical(_avail(date(2026, 1, 1)), premiere_only) is False
    # no dated VOD -> nothing to compare
    assert justwatch_predates_theatrical(_avail(None), [_theatrical(date(2026, 9, 25))]) is False
