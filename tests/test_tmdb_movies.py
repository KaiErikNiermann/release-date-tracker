"""Tests for what the TMDB movie pull makes of a film's production state.

TMDB has no cancelled film. It drops the record instead of marking it — Batgirl, Scoob!
Holiday Haunt, Superman: Flyby and Justice League: Mortal all return no search hit at all —
and ``Canceled`` did not appear once across 120 sampled films. So the film signal is not a
word for death but a ladder of how *real* a production is, which is a question TV does not
have: a series is renewed or it is not, while a film can be announced by nobody in
particular and carry a date anyway.

``TmdbSource.pull`` is driven with the fetchers monkeypatched, so no network is touched.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import pytest

from release_tracker.config import Settings
from release_tracker.franchise import ordinal_of
from release_tracker.models import Entity, MediaKind, ReleaseChannel, Stance
from release_tracker.sources import tmdb
from release_tracker.sources.base import SourceResult
from release_tracker.sources.tmdb import movie_stance, movie_status_notes

_ID = "693134"


def _movie(title: str = "Ontos") -> Entity:
    """A film with its id pinned, so the pull skips search and goes straight to the detail."""
    return Entity.create(title, MediaKind.MOVIE, external_ids={"tmdb": _ID})


def _detail(status: str, *, released: str | None = None, dates: list[str] | None = None) -> Any:
    """A `/movie/{id}?append_to_response=release_dates` payload, shaped as TMDB ships it."""
    rows = [{"type": 3, "release_date": f"{d}T00:00:00.000Z"} for d in dates or []]
    return {
        "status": status,
        "title": "Ontos",
        "release_date": released or "",
        "production_companies": [],
        "release_dates": {"results": [{"iso_3166_1": "US", "release_dates": rows}] if rows else []},
    }


def _run(
    monkeypatch: pytest.MonkeyPatch, entity: Entity, responses: dict[str, Any]
) -> tuple[SourceResult, list[str]]:
    """Same table-driven harness as the season tests: {url-substring: payload}, 404 sentinel."""
    seen: list[str] = []

    def _answer(url: str) -> Any:
        seen.append(url)
        for needle, payload in responses.items():
            if needle in url:
                return payload
        raise AssertionError(f"unexpected url: {url}")

    async def fake_get_json(_client: object, url: str, **_kw: object) -> Any:
        payload = _answer(url)
        if payload == 404:
            return {"success": False, "status_code": 34}
        return payload

    async def fake_absentable(_client: object, url: str, **_kw: object) -> Any:
        payload = _answer(url)
        return None if payload == 404 else payload

    monkeypatch.setattr(tmdb, "get_json", fake_get_json)
    monkeypatch.setattr(tmdb, "get_json_absentable", fake_absentable)

    def _noop(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(tmdb.log, "info", _noop)
    monkeypatch.setattr(tmdb.log, "warning", _noop)
    monkeypatch.setenv("TMDB_API_KEY", "x")
    result = asyncio.run(tmdb.TmdbSource().pull(object(), entity, Settings()))  # type: ignore[arg-type]
    return result, seen


# --- the ladder ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "want"),
    [
        ("Released", Stance.RELEASED),
        ("Post Production", Stance.COMING),  # Dune: Part Three
        ("In Production", Stance.COMING),  # Shrek 5, The Batman Part II
        ("Planned", Stance.COMING),  # Avengers: Secret Wars
        ("Rumored", Stance.UNCERTAIN),  # Kingsman: The Blue Blood, Gladiator III
        ("Canceled", Stance.SHELVED),  # documented; never observed in 120 films
        ("Cancelled", Stance.SHELVED),
    ],
)
def test_the_production_ladder_maps_to_a_stance(status: str, want: Stance) -> None:
    assert movie_stance(status) is want


def test_no_status_and_an_unknown_word_are_different_answers() -> None:
    """Silence is not a claim; an unrecognised word is one we cannot read. Neither shelves —
    a vocabulary change on TMDB's side must not quietly empty the upcoming queue."""
    assert movie_stance(None) is None
    assert movie_stance("") is None
    assert movie_stance("Something New") is Stance.UNKNOWN


@pytest.mark.parametrize("status", ["Released", "Planned", "In Production", "Post Production"])
def test_an_ordinary_status_says_nothing(status: str) -> None:
    """ "Post Production" beside a date adds nothing the date has not already said."""
    assert movie_status_notes(status) == ()


def test_rumored_is_quoted_back() -> None:
    (said,) = movie_status_notes("Rumored")
    assert "Rumored" in said and "TMDB" in said


# --- the pull ------------------------------------------------------------------------------
def test_dates_and_status_come_off_one_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """`append_to_response` folds the release-dates call into the detail call, so the status
    is free: this must not cost a request more than fetching the dates alone used to."""
    result, seen = _run(
        monkeypatch,
        _movie(),
        {f"/movie/{_ID}": _detail("Released", released="2024-02-27", dates=["2024-02-27"])},
    )
    assert len(seen) == 1
    assert "append_to_response" not in seen[0]  # it rides in params, not the path
    (obs,) = result.observations
    assert obs.channel is ReleaseChannel.THEATRICAL
    assert obs.release_date == date(2024, 2, 27)
    assert result.stance is Stance.RELEASED


def test_a_rumored_film_takes_an_uncertain_stance_and_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gladiator III: "Rumored", an empty release_date, no release-date blocks at all."""
    result, _ = _run(monkeypatch, _movie(), {f"/movie/{_ID}": _detail("Rumored")})
    assert result.observations == []
    assert result.stance is Stance.UNCERTAIN
    assert any("Rumored" in note for note in result.notes)


def test_a_vanished_id_is_reported_and_takes_no_stance(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 on the detail means the *pin* stopped resolving — TMDB merges and deletes
    duplicate records — which says nothing about the film. Shelving it would be a claim
    about the wrong subject entirely."""
    result, _ = _run(monkeypatch, _movie(), {f"/movie/{_ID}": 404})
    assert result.observations == []
    assert result.stance is None
    assert any(_ID in note for note in result.notes)
    assert result.skipped is None  # we did look, and we did get an answer
    assert result.external_ids == {"tmdb": _ID}  # the pin is still what we asked about


def test_an_absent_record_is_not_the_same_as_a_film_with_no_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The distinction the old code could not draw: `/movie/{id}/release_dates` returns an
    empty block list for both, and `get_json` hands a 404 envelope back as if it were data."""
    gone, _ = _run(monkeypatch, _movie(), {f"/movie/{_ID}": 404})
    undated, _ = _run(monkeypatch, _movie(), {f"/movie/{_ID}": _detail("Planned")})
    assert gone.observations == undated.observations == []
    assert gone.notes != undated.notes
    assert gone.stance is None
    assert undated.stance is Stance.COMING


# --- the collection walk -------------------------------------------------------------------
def _collection(*part_ids: int) -> Any:
    return {"name": "Ontos Collection", "parts": [{"id": i} for i in part_ids]}


def _part(
    title: str, when: str, *, status: str = "Released", words: list[str] | None = None
) -> Any:
    return {
        "title": title,
        "release_date": when,
        "status": status,
        "keywords": {"keywords": [{"name": w} for w in words or []]},
    }


def _shape(monkeypatch: pytest.MonkeyPatch, responses: dict[str, Any]) -> tuple[Any, list[str]]:
    """Same table-driven harness, aimed at `collection_shape` instead of `pull`."""
    seen: list[str] = []

    async def fake_absentable(_client: object, url: str, **_kw: object) -> Any:
        seen.append(url)
        for needle, payload in responses.items():
            if needle in url:
                return None if payload == 404 else payload
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(tmdb, "get_json_absentable", fake_absentable)
    shape = asyncio.run(tmdb.TmdbSource().collection_shape(object(), "k", "9"))  # type: ignore[arg-type]
    return shape, seen


def test_the_walk_costs_one_request_per_entry_and_no_more(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reason this is opt-in. `parts[]` carries neither `status` nor the keywords, so the
    per-entry GET is unavoidable — but it must buy both at once, not one each."""
    shape, seen = _shape(
        monkeypatch,
        {
            "/collection/9": _collection(1, 2),
            "/movie/1": _part("One", "2020-01-01"),
            "/movie/2": _part("Two", "2022-01-01"),
        },
    )
    assert len(seen) == 3  # the collection, then one per part
    assert shape is not None
    assert shape.name == "Ontos Collection"
    assert shape.highest == 2


def test_the_spinoff_keyword_is_read_off_the_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of paying for the per-entry GET: without it "Side" sits at position 2
    and pushes "Two" to 3."""
    shape, _ = _shape(
        monkeypatch,
        {
            "/collection/9": _collection(1, 2, 3),
            "/movie/1": _part("One", "2020-01-01"),
            "/movie/2": _part("Side", "2021-01-01", words=["spin off"]),
            "/movie/3": _part("Two", "2022-01-01"),
        },
    )
    assert shape is not None
    assert [e.title for e in shape.mainline] == ["One", "Two"]
    assert ordinal_of(shape, "3") == 2


def test_an_entry_that_stopped_resolving_is_dropped_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TMDB merges and deletes duplicate film records; one dead part must not lose the walk."""
    shape, _ = _shape(
        monkeypatch,
        {
            "/collection/9": _collection(1, 2),
            "/movie/1": _part("One", "2020-01-01"),
            "/movie/2": 404,
        },
    )
    assert shape is not None
    assert shape.highest == 1


def test_a_dead_collection_id_answers_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fact about the id, not about the franchise — so it must not read as "no films"."""
    shape, seen = _shape(monkeypatch, {"/collection/9": 404})
    assert shape is None
    assert len(seen) == 1  # and it does not go on to walk anything


def test_the_collection_id_rides_out_on_the_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    """`collection_shape` needs an id it cannot derive; the film detail has always had it."""

    async def fake_get_json(_client: object, _url: str, **_kw: object) -> Any:
        return {
            "status": "Released",
            "title": "Ontos",
            "release_date": "2020-01-01",
            "production_companies": [],
            "belongs_to_collection": {"id": 9, "name": "Ontos Collection"},
        }

    monkeypatch.setattr(tmdb, "get_json", fake_get_json)
    meta = asyncio.run(tmdb.TmdbSource().movie_meta(object(), "k", _ID))  # type: ignore[arg-type]
    assert meta.collection_id == "9"


def test_a_standalone_film_carries_no_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_json(_client: object, _url: str, **_kw: object) -> Any:
        return {"status": "Released", "title": "Ontos", "production_companies": []}

    monkeypatch.setattr(tmdb, "get_json", fake_get_json)
    meta = asyncio.run(tmdb.TmdbSource().movie_meta(object(), "k", _ID))  # type: ignore[arg-type]
    assert meta.collection_id is None
