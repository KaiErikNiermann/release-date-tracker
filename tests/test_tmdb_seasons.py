"""Tests for explicit TV-season resolution in the TMDB puller.

The puller prefers the structured ``entity.season`` coord over title parsing and treats a
missing season (TMDB 404 — a very-early renewal not yet in their DB) as TBA, never letting an
already-aired season's date leak onto an unaired one. These exercise ``TmdbSource.pull`` (the
public entrypoint) with ``get_json`` monkeypatched, so no network is touched.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import httpx
import pytest

from release_tracker.config import Settings
from release_tracker.models import Entity, MediaKind, ReleaseChannel
from release_tracker.sources import tmdb
from release_tracker.sources.base import SourceResult


def _tv_entity(title: str, *, season: int | None = None) -> Entity:
    # pin the show id so the puller skips search and goes straight to the season endpoint
    return Entity.create(title, MediaKind.TV, external_ids={"tmdb": "225171"}, season=season)


def _run(
    monkeypatch: pytest.MonkeyPatch, entity: Entity, responses: dict[str, Any]
) -> tuple[SourceResult, list[str]]:
    """Drive TmdbSource.pull with get_json answered from a {url-substring: payload} table.

    A payload of the literal 404 sentinel raises an HTTPStatusError(404) for that url.
    """
    seen: list[str] = []

    async def fake_get_json(_client: object, url: str, **_kw: object) -> Any:
        seen.append(url)
        for needle, payload in responses.items():
            if needle in url:
                if payload == 404:
                    raise httpx.HTTPStatusError(
                        "not found",
                        request=httpx.Request("GET", url),
                        response=httpx.Response(404, request=httpx.Request("GET", url)),
                    )
                return payload
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(tmdb, "get_json", fake_get_json)

    # silence the module logger: structlog's PrintLogger writes to a stderr that pytest's
    # capture may have closed by the time the full suite reaches here (order-dependent flake).
    def _noop(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(tmdb.log, "info", _noop)
    monkeypatch.setattr(tmdb.log, "warning", _noop)
    monkeypatch.setenv("TMDB_API_KEY", "x")
    result = asyncio.run(tmdb.TmdbSource().pull(object(), entity, Settings()))  # type: ignore[arg-type]
    return result, seen


def test_explicit_season_hits_that_season_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    # the title says Season 5, but the explicit coord (3) must win
    entity = _tv_entity("Whatever: Season 5", season=3)
    result, seen = _run(monkeypatch, entity, {"/season/3": {"air_date": "2026-07-02"}})
    assert any("/season/3" in u for u in seen)  # used the coord, not the title's 5
    assert not any("/season/5" in u for u in seen)
    (obs,) = result.observations
    assert obs.channel is ReleaseChannel.TV_BROADCAST
    assert obs.release_date == date(2026, 7, 2)


def test_absent_season_404_is_tba_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # a renewed-but-unlisted season (e.g. Pluribus S2): /season/2 -> 404 -> no date, no raise,
    # and crucially NO fallback to the show's first_air_date (which would leak S1's date)
    entity = _tv_entity("Pluribus", season=2)
    result, _ = _run(monkeypatch, entity, {"/season/2": 404})
    assert result.observations == []
    assert result.external_ids == {"tmdb": "225171"}  # still resolves the show id


def test_title_parse_is_fallback_when_no_coord(monkeypatch: pytest.MonkeyPatch) -> None:
    # no structured coord -> parse "Show: Season 4" from the title (back-compat shorthand)
    entity = _tv_entity("Silo: Season 4")  # season=None
    _, seen = _run(monkeypatch, entity, {"/season/4": {"air_date": None}})
    assert any("/season/4" in u for u in seen)
