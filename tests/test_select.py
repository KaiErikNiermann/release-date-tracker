"""Tests for the /rd-add disambiguation gate (``select_candidate``).

Pure over ``Candidate`` lists — no I/O — so it exhaustively exercises the pick/refuse
logic that decides whether a capture auto-adds or surfaces the list for an explicit pick.
"""

from __future__ import annotations

from datetime import date

from release_tracker.lookup import select_candidate
from release_tracker.sources.base import Candidate


def _c(
    cid: str,
    title: str,
    *,
    year: int | None = None,
    rel: date | None = None,
    score: float = 1.0,
    id_key: str = "tmdb",
) -> Candidate:
    return Candidate(
        source=id_key,
        id_key=id_key,
        canonical_id=cid,
        title=title,
        year=year,
        release_date=rel,
        score=score,
    )


def test_empty_is_no_match() -> None:
    assert select_candidate([]).outcome == "no_match"


def test_below_floor_is_no_match() -> None:
    assert select_candidate([_c("1", "Unrelated", year=2020, score=0.2)]).outcome == "no_match"


def test_single_above_floor_is_picked() -> None:
    pick = select_candidate([_c("1", "X", year=2026, score=0.9)])
    assert pick.outcome == "picked"
    assert pick.cand is not None and pick.cand.canonical_id == "1"


def test_dominant_top_is_picked() -> None:
    cands = [_c("1", "Odyssey", year=2026, score=0.95), _c("2", "Odyssey II", year=2020, score=0.6)]
    pick = select_candidate(cands)
    assert pick.outcome == "picked"
    assert pick.cand is not None and pick.cand.canonical_id == "1"


def test_close_scores_are_ambiguous() -> None:
    cands = [
        _c("1576", "Resident Evil", year=2002, score=1.0),
        _c("1423191", "Resident Evil", year=2026, score=1.0),
    ]
    pick = select_candidate(cands)
    assert pick.outcome == "ambiguous"
    assert {c.canonical_id for c in pick.candidates} == {"1576", "1423191"}


def test_latest_picks_newest_dated() -> None:
    cands = [
        _c("1576", "Resident Evil", year=2002, rel=date(2002, 3, 15), score=1.0),
        _c("1423191", "Resident Evil", year=2026, rel=date(2026, 9, 9), score=1.0),
    ]
    pick = select_candidate(cands, latest=True)
    assert pick.outcome == "picked"
    assert pick.cand is not None and pick.cand.canonical_id == "1423191"


def test_latest_orders_on_year_when_no_full_date() -> None:
    cands = [_c("a", "T", year=2001, score=1.0), _c("b", "T", year=2030, score=1.0)]
    pick = select_candidate(cands, latest=True)
    assert pick.cand is not None and pick.cand.canonical_id == "b"


def test_latest_falls_back_to_dominance_when_all_undated() -> None:
    cands = [_c("a", "T", score=0.95), _c("b", "T", score=0.6)]
    pick = select_candidate(cands, latest=True)
    assert pick.outcome == "picked"
    assert pick.cand is not None and pick.cand.canonical_id == "a"


def test_latest_all_undated_and_close_is_ambiguous() -> None:
    pick = select_candidate([_c("a", "T", score=1.0), _c("b", "T", score=1.0)], latest=True)
    assert pick.outcome == "ambiguous"


def test_latest_ignores_undated_while_a_dated_one_exists() -> None:
    cands = [_c("dated", "T", year=2024, score=1.0), _c("undated", "T", score=1.0)]
    pick = select_candidate(cands, latest=True)
    assert pick.cand is not None and pick.cand.canonical_id == "dated"


def test_latest_ignores_weaker_newer_match() -> None:
    # a newer but weak-title match (a same-year promo/featurette whose title merely contains the
    # query) must not beat the strong film match — latest picks among the top-band contenders only.
    cands = [
        _c("film", "The Mandalorian and Grogu", year=2026, rel=date(2026, 5, 22), score=1.0),
        _c(
            "promo",
            "Insider: The Mandalorian and Grogu — A New Mission",
            year=2026,
            rel=date(2026, 9, 1),
            score=0.5,
        ),
    ]
    pick = select_candidate(cands, latest=True)
    assert pick.cand is not None and pick.cand.canonical_id == "film"


def test_year_narrows_to_one() -> None:
    cands = [
        _c("1576", "Resident Evil", year=2002, score=1.0),
        _c("1423191", "Resident Evil", year=2026, score=1.0),
    ]
    pick = select_candidate(cands, want_year=2026)
    assert pick.outcome == "picked"
    assert pick.cand is not None and pick.cand.canonical_id == "1423191"


def test_year_no_such_year_is_no_match() -> None:
    assert (
        select_candidate([_c("1", "T", year=2002, score=1.0)], want_year=2030).outcome == "no_match"
    )


def test_year_then_latest_combines() -> None:
    cands = [
        _c("a", "T", year=2026, rel=date(2026, 1, 1), score=1.0),
        _c("b", "T", year=2026, rel=date(2026, 12, 1), score=1.0),
        _c("c", "T", year=2002, rel=date(2002, 1, 1), score=1.0),
    ]
    pick = select_candidate(cands, want_year=2026, latest=True)
    assert pick.cand is not None and pick.cand.canonical_id == "b"


def test_id_pick_exact_match() -> None:
    cands = [
        _c("1576", "RE", year=2002, score=1.0),
        _c("1423191", "RE", year=2026, score=1.0),
    ]
    pick = select_candidate(cands, id_pick={"tmdb": "1423191"})
    assert pick.outcome == "picked"
    assert pick.cand is not None and pick.cand.canonical_id == "1423191"


def test_id_pick_wins_even_below_floor() -> None:
    # an explicit id is user-directed: it must resolve regardless of the score floor.
    cands = [_c("42", "Weird Title", year=2026, score=0.05)]
    pick = select_candidate(cands, id_pick={"tmdb": "42"})
    assert pick.outcome == "picked"
    assert pick.cand is not None and pick.cand.canonical_id == "42"


def test_id_pick_not_found_is_no_match() -> None:
    assert (
        select_candidate([_c("1576", "RE", year=2002, score=1.0)], id_pick={"tmdb": "999"}).outcome
        == "no_match"
    )
