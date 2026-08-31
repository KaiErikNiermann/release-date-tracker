"""Tests for region-tagged where-facts out of TMDB.

Two things are being pinned here. A provider lookup must ask about *markets*, never about
the ``RDT_REGIONS`` profile — which can hold the ``ANY``/``*`` VPN sentinel that keys nothing
at TMDB and silently yielded no providers at all. And the answer must carry the market it was
read from, because the same service in two countries is two facts, not one.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from release_tracker.config import Settings
from release_tracker.sources import tmdb
from release_tracker.sources.base import PlatformOffer

_PROVIDERS: dict[str, Any] = {
    "results": {
        "US": {
            "flatrate": [
                {"provider_name": "Netflix"},
                {"provider_name": "Paramount Plus Premium"},
                {"provider_name": "Paramount+ Amazon Channel"},
                {"provider_name": "Paramount Plus Apple TV channel"},
            ]
        },
        "DE": {"flatrate": [{"provider_name": "Paramount Plus"}]},
        "JP": {"flatrate": [{"provider_name": "U-NEXT"}]},
    }
}
_DETAIL: dict[str, Any] = {"networks": [{"name": "Showtime"}], "origin_country": ["US"]}


def _platforms(
    monkeypatch: pytest.MonkeyPatch, regions: tuple[str, ...]
) -> tuple[PlatformOffer, ...]:
    async def fake_get_json(_client: object, url: str, **_kw: object) -> Any:
        return _PROVIDERS if "watch/providers" in url else _DETAIL

    monkeypatch.setattr(tmdb, "get_json", fake_get_json)
    return asyncio.run(
        tmdb.TmdbSource().tv_platforms(object(), "k", "117488", regions)  # type: ignore[arg-type]
    )


def test_offers_carry_the_market_they_were_read_from(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same service in two countries is two offers, each tagged."""
    offers = _platforms(monkeypatch, ("US", "DE"))
    assert PlatformOffer("Netflix", "US") in offers
    assert PlatformOffer("Paramount Plus", "DE") in offers


def test_origin_networks_carry_no_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """A home channel is a fact about the production, not an availability in a market."""
    network = next(o for o in _platforms(monkeypatch, ("US",)) if o.name == "Showtime")
    assert network.region is None


def test_reseller_add_ons_are_dropped_case_insensitively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TMDB spells them both ways; a cased test kept every lowercase one."""
    names = {o.name for o in _platforms(monkeypatch, ("US",))}
    assert "Paramount+ Amazon Channel" not in names
    assert "Paramount Plus Apple TV channel" not in names
    assert "Paramount Plus Premium" in names  # the base service survives


def test_unknown_region_yields_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure mode the wildcard caused: a region TMDB does not key on matches nothing."""
    assert [o for o in _platforms(monkeypatch, ("ANY",)) if o.region is not None] == []


# --- the setting that feeds those regions -------------------------------------------------
def test_provider_regions_passes_real_markets_through() -> None:
    assert Settings(RDT_REGIONS="US,DE").provider_regions == ("US", "DE")


@pytest.mark.parametrize("sentinel", ["ANY", "*", "any", "US,*"])
def test_provider_regions_resolves_the_wildcard_to_the_scan_basket(sentinel: str) -> None:
    """`ANY` means "region does not gate me", not "no markets" — a lookup needs real codes.

    This is the bug: `RDT_REGIONS=ANY` is a legitimate profile (a VPN user), and it made
    every flatrate lookup return an empty list, so `where:` fell back to origin networks.
    """
    settings = Settings(RDT_REGIONS=sentinel, RDT_JUSTWATCH_REGIONS="US,GB,JP")
    assert settings.provider_regions == ("US", "GB", "JP")
    assert "ANY" not in settings.provider_regions
