"""Precedence when a JustWatch store date meets TMDB's confirmed Digital claim.

A store's ``availableFromTime`` is a *listing* date, so it is corroboration or an extra
region-scoped line — never a replacement for the studio's own date. See
:func:`release_tracker.lookup.merge_justwatch`.
"""

from __future__ import annotations

from datetime import date

from release_tracker.lookup import Claim, merge_justwatch
from release_tracker.models import DatePrecision
from release_tracker.sources.justwatch import JustWatchAvailability, Offer


def _tmdb_digital(when: date) -> Claim:
    return Claim(
        "Digital", when, DatePrecision.EXACT, "confirmed", 0.9, None, "TMDB type-4 (US)", "US"
    )


def _guess(when: date) -> Claim:
    return Claim(
        "Digital (est.)", when, DatePrecision.EXACT, "speculative", 0.4, 21, "window model"
    )


def _avail(vod: date, country: str = "ES", platform: str = "Amazon Video") -> JustWatchAvailability:
    return JustWatchAvailability(
        object_id=1,
        title="Toy Story 5",
        year=2026,
        offers=(Offer(country, "buy", platform, "hd", 16.99, "EUR", vod),),
        earliest_vod=vod,
        earliest_vod_country=country,
        earliest_vod_platform=platform,
    )


def _labels(claims: list[Claim]) -> list[str]:
    return [c.label for c in claims]


def test_earlier_store_date_is_added_beside_tmdb_not_over_it() -> None:
    claims, _, _, notes = merge_justwatch(
        [_tmdb_digital(date(2026, 8, 18))], (), None, _avail(date(2026, 8, 17))
    )
    # TMDB's own date survives, and the earlier market shows up as its own line (the VPN answer).
    assert _labels(claims) == ["Digital", "Digital (earliest · ES)"]
    assert next(c for c in claims if c.label == "Digital").when == date(2026, 8, 18)
    assert next(c for c in claims if c.label.startswith("Digital (earliest")).when == date(
        2026, 8, 17
    )
    assert any("earlier in ES" in n for n in notes)


def test_a_wildly_early_store_date_cannot_erase_tmdb() -> None:
    # the Toy Story 5 shape: a store date two months early must not become *the* digital date.
    claims, _, _, _ = merge_justwatch(
        [_tmdb_digital(date(2026, 8, 18))],
        (),
        None,
        _avail(date(2026, 6, 17), platform="Apple TV Store"),
    )
    assert "Digital" in _labels(claims)
    assert next(c for c in claims if c.label == "Digital").when == date(2026, 8, 18)


def test_later_store_date_only_corroborates() -> None:
    claims, _, _, notes = merge_justwatch(
        [_tmdb_digital(date(2026, 8, 18))], (), None, _avail(date(2026, 9, 1))
    )
    assert _labels(claims) == ["Digital"]
    assert any("corroborates" in n for n in notes)


def test_a_real_store_offer_still_drops_a_guess() -> None:
    claims, _, _, _ = merge_justwatch(
        [_tmdb_digital(date(2026, 8, 18)), _guess(date(2026, 7, 1))],
        (),
        None,
        _avail(date(2026, 9, 1)),
    )
    assert _labels(claims) == ["Digital"]


def test_store_offer_leads_when_tmdb_has_no_digital() -> None:
    claims, _, _, _ = merge_justwatch(
        [_guess(date(2026, 7, 1))], (), None, _avail(date(2026, 8, 17))
    )
    assert _labels(claims) == ["Digital (earliest · ES)"]
