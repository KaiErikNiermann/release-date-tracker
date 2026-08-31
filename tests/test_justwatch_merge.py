"""Precedence when a JustWatch store date meets TMDB's confirmed Digital claim.

A store's ``availableFromTime`` is a *listing* date, so it is corroboration or an extra
region-scoped line — never a replacement for the studio's own date. See
:func:`release_tracker.lookup.merge_justwatch`.
"""

from __future__ import annotations

from datetime import date

from release_tracker import lookup
from release_tracker.lookup import Claim, merge_justwatch
from release_tracker.models import DatePrecision
from release_tracker.sources import justwatch
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


# --- season-scoped merges -------------------------------------------------------------------
def _season_avail(
    *,
    season: int,
    flatrate: tuple[str, ...] = (),
    upcoming: tuple[justwatch.UpcomingRelease, ...] = (),
) -> justwatch.JustWatchAvailability:
    return justwatch.JustWatchAvailability(
        object_id=228068,
        title="Yellowjackets",
        year=2021,
        offers=tuple(
            justwatch.Offer("US", "flatrate", p, "hd", None, None, None) for p in flatrate
        ),
        earliest_vod=None,
        earliest_vod_country=None,
        earliest_vod_platform=None,
        season=season,
        upcoming=upcoming,
    )


def test_a_season_lookup_answers_with_the_seasons_own_streaming_homes() -> None:
    """TMDB's watch-providers are show-level, so they name every service that ever carried it."""
    show_level = ("Netflix", "Paramount+ with Showtime", "Showtime")
    claims, streaming, _predicted, _notes = lookup.merge_justwatch(
        [], show_level, None, _season_avail(season=3, flatrate=("Paramount Plus Premium",))
    )
    assert streaming == ("Paramount+",)  # canonicalised, and Netflix is gone — S3 is not on it
    assert claims == []


def test_a_season_that_has_not_dropped_streams_nowhere() -> None:
    """Empty is the honest answer; falling back to the show's list is the failure."""
    _claims, streaming, _predicted, _notes = lookup.merge_justwatch(
        [], ("Netflix", "Showtime"), None, _season_avail(season=4)
    )
    assert streaming == ()


def test_merge_announced_adds_the_platforms_own_date() -> None:
    """A future season has no offer to read a date off, but the platform published one."""
    upcoming = (
        justwatch.UpcomingRelease("US", date(2026, 11, 20), "digital", "DATE", "Paramount Plus"),
    )
    claims, notes = lookup.merge_announced([], _season_avail(season=4, upcoming=upcoming))
    (claim,) = claims
    assert (claim.when, claim.stance) == (date(2026, 11, 20), "confirmed")
    assert claim.confidence == 0.9  # under the 0.95 a live offer earns — it has not happened
    assert "season 4" in notes[0]


def test_merge_announced_ignores_a_release_type_with_no_channel() -> None:
    upcoming = (justwatch.UpcomingRelease("US", date(2026, 11, 20), "tv", "DATE", "Showtime"),)
    assert lookup.merge_announced([], _season_avail(season=4, upcoming=upcoming)) == ([], ())


def test_merge_announced_does_not_restate_a_date_a_source_already_gave() -> None:
    existing = [
        lookup.Claim(
            "Digital",
            date(2026, 11, 20),
            DatePrecision.EXACT,
            "confirmed",
            0.95,
            None,
            "TMDB",
            "US",
        )
    ]
    upcoming = (
        justwatch.UpcomingRelease("US", date(2026, 11, 20), "digital", "DATE", "Paramount Plus"),
    )
    claims, notes = lookup.merge_announced(existing, _season_avail(season=4, upcoming=upcoming))
    assert len(claims) == 1 and notes == ()
