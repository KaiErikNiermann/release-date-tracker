"""Tests for the JustWatch offer source (pure parsing/derivation; fetch is best-effort HTTP)."""

from __future__ import annotations

from datetime import date
from typing import Any

from release_tracker.sources.justwatch import (
    JustWatchAvailability,
    Offer,
    dedupe,
    earliest_vod,
    parse_from_time,
    parse_offers,
    parse_price,
    pick_node,
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
