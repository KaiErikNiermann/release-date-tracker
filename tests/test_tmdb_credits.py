"""Tests for TMDB filmography mining (the artist-radar's canonical pipeline).

The network-touching ``person_credits`` is a thin loop over two pure helpers — the
role filtering, "Self" drop, and per-work seniority collapse live in ``consider_credit``
/ ``is_self``, which are unit-tested here directly on combined_credits-shaped dicts.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from release_tracker.sources.tmdb import FilmCredit, consider_credit, is_self, pick_person_id


def _crew(job: str, **kw: Any) -> dict[str, Any]:
    return {"media_type": "movie", "id": 1, "title": "A Film", "job": job, **kw}


def test_is_self_flags_talk_show_appearances() -> None:
    assert is_self("self")
    assert is_self("self - guest")
    assert is_self("himself")
    assert not is_self("ellen ripley")


def test_creative_crew_jobs_are_kept_and_labelled() -> None:
    best: dict[tuple[str, str], FilmCredit] = {}
    consider_credit(best, _crew("Director", release_date="2026-09-18"), "Director")
    (credit,) = best.values()
    assert credit.role == "Director"
    assert credit.media == "movie"
    assert credit.when == date(2026, 9, 18)
    assert credit.url == "https://www.themoviedb.org/movie/1"


def test_most_senior_role_wins_per_work() -> None:
    # someone who both writes and directs one film collapses to a single Director line
    best: dict[tuple[str, str], FilmCredit] = {}
    consider_credit(best, _crew("Writer", release_date="2026-01-01"), "Writer")
    consider_credit(best, _crew("Director", release_date="2026-01-01"), "Director")
    assert len(best) == 1
    assert next(iter(best.values())).role == "Director"
    # a later cast bit-part on the same film does not override the directing credit
    consider_credit(best, {"media_type": "movie", "id": 1, "name": "A Film"}, "Actor")
    assert next(iter(best.values())).role == "Director"


def test_tv_credit_uses_name_and_first_air_date() -> None:
    best: dict[tuple[str, str], FilmCredit] = {}
    raw = {"media_type": "tv", "id": 9, "name": "A Series", "first_air_date": "2027-04-02"}
    consider_credit(best, raw, "Creator")
    (credit,) = best.values()
    assert (credit.title, credit.media, credit.when) == ("A Series", "tv", date(2027, 4, 2))


def test_pick_person_id_prefers_creators_then_popularity() -> None:
    # a creative-department match outranks a more-popular incidental one...
    results = [
        {"id": 1, "known_for_department": "Sound", "popularity": 90.0},
        {"id": 2, "known_for_department": "Writing", "popularity": 20.0},
    ]
    assert pick_person_id(results) == "2"
    # ...and within creators, the most popular wins
    creators = [
        {"id": 3, "known_for_department": "Directing", "popularity": 5.0},
        {"id": 4, "known_for_department": "Directing", "popularity": 50.0},
    ]
    assert pick_person_id(creators) == "4"
    assert pick_person_id([]) is None
    assert pick_person_id([{"known_for_department": "Writing"}]) is None  # no id -> skipped


def test_undated_and_typeless_entries_are_handled() -> None:
    best: dict[tuple[str, str], FilmCredit] = {}
    # a person credit with no date is kept (when=None) so the caller can drop it later
    consider_credit(best, _crew("Director"), "Director")
    assert next(iter(best.values())).when is None
    # a non movie/tv media_type (person, etc.) is skipped entirely
    consider_credit(best, {"media_type": "person", "id": 2, "name": "x"}, "Actor")
    assert len(best) == 1
