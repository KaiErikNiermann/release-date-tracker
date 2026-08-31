"""Tests for season-scoped JustWatch offers.

These existed only as a hard skip before: both call sites bailed out on a season-pinned
entity on the belief that JustWatch answers at show level only. It does not, and the
show-level reading is actively wrong for the case the skip was protecting — Yellowjackets
carries Netflix on seasons 1-2 and not on 3, so answering a season-3 question with the show's
platforms names a service that does not have it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

import pytest

from release_tracker.sources import justwatch
from release_tracker.sources.justwatch import (
    JustWatchAvailability,
    UpcomingRelease,
    parse_season,
    parse_upcoming,
    season_availability,
)


def _offer(monetization: str, platform: str, when: str | None = None) -> dict[str, Any]:
    return {
        "monetizationType": monetization,
        "presentationType": "HD",
        "retailPrice": None,
        "currency": "USD",
        "availableFromTime": when,
        "package": {"clearName": platform},
    }


def _season(
    number: int | None, offers: list[dict[str, Any]], upcoming: list[Any]
) -> dict[str, Any]:
    return {
        "id": f"tss{number}",
        "objectId": number,
        "content": {
            "seasonNumber": number,
            "title": f"Season {number}",
            "upcomingReleases": upcoming,
        },
        "offers": offers,
    }


def _payload(*seasons: dict[str, Any]) -> dict[str, Any]:
    return {
        "data": {
            "popularTitles": {
                "edges": [
                    {
                        "node": {
                            "id": "ts228068",
                            "objectId": 228068,
                            "objectType": "SHOW",
                            "content": {"title": "Yellowjackets", "originalReleaseYear": 2021},
                            "seasons": list(seasons),
                        }
                    }
                ]
            }
        }
    }


def _run(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    *,
    season: int,
    countries: tuple[str, ...] = ("US",),
) -> tuple[JustWatchAvailability | None, list[str]]:
    """Drive season_availability with post_text answered from one payload, recording bodies."""
    sent: list[str] = []

    async def fake_post(_client: object, _url: str, *, content: str, **_kw: object) -> Any:
        sent.append(content)
        return payload

    monkeypatch.setattr(justwatch, "post_text", fake_post)

    def _noop(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(justwatch.log, "info", _noop)
    monkeypatch.setattr(justwatch.log, "warning", _noop)
    result = asyncio.run(
        season_availability(
            object(),  # type: ignore[arg-type]
            "Yellowjackets",
            season=season,
            countries=countries,
            year=2021,
        )
    )
    return result, sent


# --- the parsers ---------------------------------------------------------------------------
def test_parse_upcoming_reads_a_dated_announcement() -> None:
    (row,) = parse_upcoming(
        [
            {
                "releaseDate": "2026-11-20",
                "releaseType": "DIGITAL",
                "label": "DATE",
                "package": {"clearName": "Paramount Plus Premium"},
            }
        ],
        "us",
    )
    assert row == UpcomingRelease(
        "US", date(2026, 11, 20), "digital", "DATE", "Paramount Plus Premium"
    )
    assert row.firm


def test_parse_upcoming_drops_an_undated_row() -> None:
    """An "upcoming" with no date says nothing anyone can act on."""
    assert parse_upcoming([{"releaseType": "DIGITAL", "label": "WINDOW"}], "US") == []


def test_a_non_date_label_is_not_firm() -> None:
    (row,) = parse_upcoming(
        [{"releaseDate": "2026-11-20", "releaseType": "DIGITAL", "label": "WINDOW"}], "US"
    )
    assert not row.firm


def test_parse_season_refuses_an_unnumbered_season() -> None:
    """Guessing the number from list order would shift every offer wherever one is missing."""
    assert parse_season(_season(None, [_offer("FLATRATE", "Netflix")], []), "US") is None


# --- the fetch -----------------------------------------------------------------------------
def test_offers_come_from_the_pinned_season_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression the old skip existed to prevent, now prevented structurally."""
    avail, _ = _run(
        monkeypatch,
        _payload(
            _season(1, [_offer("FLATRATE", "Netflix"), _offer("FLATRATE", "Paramount Plus")], []),
            _season(3, [_offer("FLATRATE", "Paramount Plus")], []),
        ),
        season=3,
    )
    assert avail is not None
    assert avail.season == 3
    assert "Netflix" not in avail.streaming_platforms  # S1's homes must not leak onto S3


def test_one_request_per_country(monkeypatch: pytest.MonkeyPatch) -> None:
    """The search and the seasons are one document — N requests, not N+1."""
    _, sent = _run(
        monkeypatch,
        _payload(_season(2, [_offer("FLATRATE", "Netflix")], [])),
        season=2,
        countries=("US", "GB", "DE"),
    )
    assert len(sent) == 3
    assert all(json.loads(body)["variables"]["country"] in {"US", "GB", "DE"} for body in sent)


def test_a_season_with_no_offers_still_yields_its_announced_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The expected shape for a future season — a bare offer check would discard the answer."""
    avail, _ = _run(
        monkeypatch,
        _payload(
            _season(
                4,
                [],
                [
                    {
                        "releaseDate": "2026-11-20",
                        "releaseType": "DIGITAL",
                        "label": "DATE",
                        "package": {"clearName": "Paramount Plus"},
                    }
                ],
            )
        ),
        season=4,
    )
    assert avail is not None
    assert avail.offers == ()
    assert avail.announced is not None
    assert avail.announced.when == date(2026, 11, 20)


def test_a_season_justwatch_does_not_carry_is_an_honest_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Falling back to the show's offers would be the exact wrong-season answer."""
    avail, _ = _run(
        monkeypatch, _payload(_season(1, [_offer("FLATRATE", "Netflix")], [])), season=9
    )
    assert avail is None


def test_a_graphql_error_yields_none_and_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """A keyless best-effort source must never break the lookup around it."""
    avail, _ = _run(monkeypatch, {"errors": [{"message": "nope"}]}, season=2)
    assert avail is None


# --- the year guard, which a season legitimately trips ------------------------------------
def _avail(
    *, season: int | None, year: int | None, vod: date | None = None
) -> JustWatchAvailability:
    return JustWatchAvailability(
        object_id=1,
        title="Yellowjackets",
        year=year,
        offers=(),
        earliest_vod=vod,
        earliest_vod_country="US" if vod else None,
        earliest_vod_platform="Amazon Video" if vod else None,
        season=season,
    )


def test_a_show_older_than_its_season_is_not_a_collision() -> None:
    """JustWatch reports the *show's* first year; the hint is the *season's*.

    A long-running series legitimately puts years between them, and the symmetric ±1 check
    read that as a same-name collision — silently discarding the offers for every season past
    the second.
    """
    from release_tracker.lookup import justwatch_year_mismatch

    assert justwatch_year_mismatch(_avail(season=3, year=2021), 2025) is None


def test_a_show_that_postdates_its_own_season_still_fails() -> None:
    """That direction is still impossible, so it still catches a newer same-named title."""
    from release_tracker.lookup import justwatch_year_mismatch

    assert justwatch_year_mismatch(_avail(season=1, year=2030), 2021) is not None


def test_the_symmetric_check_still_holds_for_a_film() -> None:
    """Nothing about the film path changes — it compares two years of the same thing."""
    from release_tracker.lookup import justwatch_year_mismatch

    assert justwatch_year_mismatch(_avail(season=None, year=2001), 2026) is not None
    assert justwatch_year_mismatch(_avail(season=None, year=2026), 2026) is None
