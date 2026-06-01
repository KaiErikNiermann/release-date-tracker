"""Tests for the matching / resolution logic (pure functions)."""

from __future__ import annotations

from datetime import date

from release_tracker.matching import (
    is_released,
    needs_resolution,
    required_id_key,
    score_candidate,
    year_hint,
)
from release_tracker.models import Entity, MediaKind
from release_tracker.sources.base import Candidate, pinned_id

TODAY = date(2026, 6, 1)


def _cand(title: str, year: int | None) -> Candidate:
    return Candidate(source="tmdb", id_key="tmdb", canonical_id="1", title=title, year=year)


def test_score_uses_year_for_movies() -> None:
    near = score_candidate("Blade", 2027, _cand("Blade", 2027), MediaKind.GAME)
    far = score_candidate("Blade", 2027, _cand("Blade", 2011), MediaKind.GAME)
    assert near > far  # same title, year proximity breaks the tie


def test_score_ignores_year_for_tv() -> None:
    # a 2016 show debut shouldn't be penalised when the watchlist date is a S5 date
    a = score_candidate("Stranger Things", 2025, _cand("Stranger Things", 2016), MediaKind.TV)
    b = score_candidate("Stranger Things", 2025, _cand("Stranger Things", 2025), MediaKind.TV)
    assert a == b == 1.0


def test_is_released_only_when_all_dates_past() -> None:
    assert is_released([date(2025, 1, 1)], TODAY) is True
    assert is_released([date(2025, 1, 1), date(2027, 1, 1)], TODAY) is False
    assert is_released([], TODAY) is False  # unknown date != released


def test_year_hint_prefers_soonest_upcoming() -> None:
    dates = [date(2020, 1, 1), date(2027, 5, 1), date(2026, 9, 1)]
    assert year_hint(dates, TODAY) == 2026
    assert year_hint([date(2019, 1, 1)], TODAY) == 2019  # falls back to latest known
    assert year_hint([], TODAY) is None


def test_needs_resolution_and_required_key() -> None:
    assert required_id_key(MediaKind.MOVIE) == "tmdb"
    assert required_id_key(MediaKind.GAME) == "igdb"
    assert required_id_key(MediaKind.BOOK) is None  # no Tier-0 source

    unresolved = Entity.create("Blade", MediaKind.GAME)
    assert needs_resolution(unresolved) is True
    resolved = Entity.create("Blade", MediaKind.GAME, external_ids={"igdb": "279646"})
    assert needs_resolution(resolved) is False
    book = Entity.create("Some Novel", MediaKind.BOOK)
    assert needs_resolution(book) is False  # not resolvable -> not on the worklist


def test_pinned_id_skip_sentinel() -> None:
    assert pinned_id({"steam_appid": "123"}, "steam_appid") == ("123", False)
    assert pinned_id({"steam_appid": "none"}, "steam_appid") == (None, True)
    assert pinned_id({}, "steam_appid") == (None, False)
