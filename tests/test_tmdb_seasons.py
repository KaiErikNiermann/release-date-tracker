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

import pytest

from release_tracker.config import Settings
from release_tracker.models import Entity, MediaKind, ReleaseChannel, Stance
from release_tracker.sources import tmdb
from release_tracker.sources.base import SourceResult


def _tv_entity(title: str, *, season: int | None = None) -> Entity:
    # pin the show id so the puller skips search and goes straight to the season endpoint
    return Entity.create(title, MediaKind.TV, external_ids={"tmdb": "225171"}, season=season)


def _run(
    monkeypatch: pytest.MonkeyPatch, entity: Entity, responses: dict[str, Any]
) -> tuple[SourceResult, list[str]]:
    """Drive TmdbSource.pull with the fetchers answered from a {url-substring: payload} table.

    A payload of the literal 404 sentinel stands for a resource TMDB does not have. It is
    returned as ``None`` from `get_json_absentable` rather than raised, because that is what
    the real thing does: TMDB answers 404 with a JSON body, `get_json` only raises for
    429/5xx, and an earlier version of this harness raised instead — which made a branch pass
    here that could never run in production.
    """
    seen: list[str] = []

    def _answer(url: str) -> Any:
        seen.append(url)
        for needle, payload in responses.items():
            if needle in url:
                return payload
        raise AssertionError(f"unexpected url: {url}")

    async def fake_get_json(_client: object, url: str, **_kw: object) -> Any:
        payload = _answer(url)
        if payload == 404:  # `get_json` hands the error envelope back as if it were data
            return {"success": False, "status_code": 34}
        return payload

    async def fake_absentable(_client: object, url: str, **_kw: object) -> Any:
        payload = _answer(url)
        return None if payload == 404 else payload

    monkeypatch.setattr(tmdb, "get_json", fake_get_json)
    monkeypatch.setattr(tmdb, "get_json_absentable", fake_absentable)

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
    # a renewed-but-unlisted season (Pluribus S2): /season/2 -> 404 -> no date, no raise,
    # and crucially NO fallback to the show's first_air_date (which would leak S1's date)
    entity = _tv_entity("Pluribus", season=2)
    result, _ = _run(
        monkeypatch,
        entity,
        {
            "/season/2": 404,
            "/tv/225171": {"status": "Returning Series", "number_of_seasons": 1, "seasons": []},
        },
    )
    assert result.observations == []
    assert result.external_ids == {"tmdb": "225171"}  # still resolves the show id


def test_an_absent_season_says_why_it_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Pluribus regression. A running show's missing season is "not listed yet" — never a
    denial — because it was renewed before season 1 aired and TMDB has not caught up."""
    result, _ = _run(
        monkeypatch,
        _tv_entity("Pluribus", season=2),
        {
            "/season/2": 404,
            "/tv/225171": {
                "status": "Returning Series",
                "number_of_seasons": 1,
                "seasons": [
                    {
                        "season_number": 1,
                        "name": "Season 1",
                        "air_date": "2025-11-06",
                        "episode_count": 9,
                    }
                ],
            },
        },
    )
    said = " ".join(result.notes).casefold()
    assert "not listed yet" in said
    for never in ("carries no", "does not exist", "cancel"):
        assert never not in said


def test_a_finished_shows_missing_season_is_stated_plainly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Marvel's Daredevil has three; the fourth is Born Again's first, on another id."""
    result, _ = _run(
        monkeypatch,
        _tv_entity("Daredevil", season=4),
        {
            "/season/4": 404,
            "/tv/225171": {
                "status": "Ended",
                "number_of_seasons": 3,
                "seasons": [
                    {
                        "season_number": n,
                        "name": f"Season {n}",
                        "air_date": f"201{n + 4}-01-01",
                        "episode_count": 13,
                    }
                    for n in (1, 2, 3)
                ],
            },
        },
    )
    assert "carries no season 4" in " ".join(result.notes)
    assert "Ended" in " ".join(result.notes)


def test_an_in_range_season_costs_no_extra_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """The show detail is fetched only on the anomaly — every row of a refresh batch must
    cost exactly what it did before this existed."""
    _, seen = _run(
        monkeypatch,
        _tv_entity("Whatever", season=2),
        {"/season/2": {"air_date": "2026-07-02"}},
    )
    assert not [u for u in seen if u.endswith("/tv/225171")]


def test_title_parse_is_fallback_when_no_coord(monkeypatch: pytest.MonkeyPatch) -> None:
    # no structured coord -> parse "Show: Season 4" from the title (back-compat shorthand)
    entity = _tv_entity("Silo: Season 4")  # season=None
    _, seen = _run(monkeypatch, entity, {"/season/4": {"air_date": None}})
    assert any("/season/4" in u for u in seen)


# --- the stance the show itself takes ---------------------------------------------------------
def _show(status: str, seasons: list[int], *, aired: str = "2020-01-01") -> Any:
    return {
        "status": status,
        "number_of_seasons": len(seasons),
        "seasons": [
            {"season_number": n, "name": f"Season {n}", "air_date": aired, "episode_count": 10}
            for n in seasons
        ],
        "first_air_date": aired,
    }


@pytest.mark.parametrize(
    ("status", "want"),
    [
        ("Ended", Stance.FINISHED),  # Dexter — ran and stopped; ordinary rows, never shelved
        ("Canceled", Stance.FINISHED),  # a cancelled *series* still aired what it aired
        ("Returning Series", Stance.UNCERTAIN),  # running, nothing scheduled
        ("Something New", Stance.UNKNOWN),  # a word we do not know: fail open
    ],
)
def test_a_whole_show_pull_takes_a_stance(
    monkeypatch: pytest.MonkeyPatch, status: str, want: Stance
) -> None:
    """Free: `/tv/{id}` has always carried `status` and the season list, and without reading
    them `stance:finished` could never find a show that ended."""
    result, seen = _run(monkeypatch, _tv_entity("Silo"), {"/tv/225171": _show(status, [1, 2])})
    assert result.stance is want
    assert len(seen) == 1  # and it costs no request the pull did not already make


def test_a_show_with_a_scheduled_season_is_coming(monkeypatch: pytest.MonkeyPatch) -> None:
    """`stance_of`'s third state: running *and* a row exists for a season not yet aired."""
    show = _show("Returning Series", [1, 2])
    show["seasons"][1]["air_date"] = "2099-01-01"
    result, _ = _run(monkeypatch, _tv_entity("Silo"), {"/tv/225171": show})
    assert result.stance is Stance.COMING


def test_an_in_range_season_pull_takes_no_stance(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cost gate. A refresh batch pulls one row per season, and a show-level GET on each
    would double it — so an ordinary season pull says nothing rather than paying to."""
    result, seen = _run(
        monkeypatch,
        _tv_entity("Silo", season=2),
        {"/tv/225171/season/2": {"air_date": "2025-01-01"}},
    )
    assert result.stance is None
    assert len(seen) == 1
    assert all("/season/2" in url for url in seen)


def test_the_absent_season_branch_takes_the_stance_it_already_paid_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The anomaly branch fetches the shape to explain itself; the stance rides along free."""
    result, _ = _run(
        monkeypatch,
        _tv_entity("Dexter", season=9),
        {"/tv/225171/season/9": 404, "/tv/225171": _show("Ended", [1, 2, 3])},
    )
    assert result.stance is Stance.FINISHED
    assert any("season 9" in note for note in result.notes)
