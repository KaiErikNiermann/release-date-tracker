"""Tests for how search results are ordered, and for saying when one looks wrong.

Title similarity alone cannot separate "Hollow Knight Silksong" from "Hollow Knight: Silksong"
— `normalize` strips the colon, so both score exactly 1.000 and whichever the API returned
first wins. IGDB returns the fan demake first. An audience signal is what breaks that tie, and
it is deliberately kept out of `Candidate.score`, which means "how well the title matches" and
is read as exactly that by MATCH_FLOOR, the dominance band and the kind consensus.
"""

from __future__ import annotations

import pytest

from release_tracker.matching import POPULARITY_WEIGHT, rank_candidate, score_candidate
from release_tracker.models import MediaKind
from release_tracker.sources.base import Candidate, prominence


def _cand(
    title: str, *, score: float = 0.0, pop: float = 0.0, caveats: tuple[str, ...] = ()
) -> Candidate:
    return Candidate(
        source="igdb",
        id_key="igdb",
        canonical_id=title,
        title=title,
        score=score,
        popularity=pop,
        caveats=caveats,
    )


# --- prominence ------------------------------------------------------------------------------
def test_prominence_is_zero_without_an_audience() -> None:
    assert prominence(0) == 0.0
    assert prominence(-5) == 0.0


def test_prominence_grows_but_saturates() -> None:
    """Log-scaled: the interesting gap is *some* audience versus none, not 5000 versus 1000."""
    none_to_some = prominence(1000) - prominence(0)
    some_to_lots = prominence(5000) - prominence(1000)
    assert none_to_some > some_to_lots
    assert 0.0 < prominence(10) < prominence(1000) <= 1.0


# --- the tie the whole change exists for -------------------------------------------------------
def test_an_audience_breaks_a_tie_the_title_cannot() -> None:
    """The Silksong case: both titles normalise identically and score 1.000."""
    real = _cand("Hollow Knight: Silksong", score=1.0, pop=0.78)
    demake = _cand("Hollow Knight Silksong", score=1.0, pop=0.0)
    assert score_candidate("hollow knight silksong", None, real, MediaKind.GAME) == score_candidate(
        "hollow knight silksong", None, demake, MediaKind.GAME
    )
    assert rank_candidate(real) > rank_candidate(demake)


def test_a_better_title_match_is_never_buried_under_a_popular_one() -> None:
    """The bound that makes this safe: prominence may only reorder inside the band the code
    already calls too close to tell apart."""
    exact_obscure = _cand("Cliff Dexter", score=1.0, pop=0.0)
    fuzzy_famous = _cand("Dexter", score=1.0 - POPULARITY_WEIGHT - 0.01, pop=1.0)
    assert rank_candidate(exact_obscure) > rank_candidate(fuzzy_famous)


def test_prominence_reorders_within_the_band() -> None:
    """ "American Daredevils" (0 votes) outscores "Daredevil: Born Again" on title alone."""
    noise = _cand("American Daredevils", score=0.64, pop=0.0)
    real = _cand("Daredevil: Born Again", score=0.62, pop=0.76)
    assert rank_candidate(noise) < rank_candidate(real)


def test_classic_ranking_is_title_order() -> None:
    """`weight=0` restores pure title matching for anyone who would rather type the exact name."""
    real = _cand("Hollow Knight: Silksong", score=1.0, pop=0.78)
    demake = _cand("Hollow Knight Silksong", score=1.0, pop=0.0)
    assert rank_candidate(real, weight=0.0) == rank_candidate(demake, weight=0.0)


def test_score_stays_a_title_measure() -> None:
    """MATCH_FLOOR, the dominance band and `drafts._consensus_kind` all read `score` as title
    similarity, and `drafts` documents in prose that it is comparable across sources. Folding
    an audience signal into it would silently change what all three mean."""
    cand = _cand("Dexter", score=0.5, pop=1.0)
    assert cand.score == 0.5
    assert rank_candidate(cand) != cand.score


# --- caveats -----------------------------------------------------------------------------------
def test_caveats_are_carried_but_never_reorder() -> None:
    """A caveat is something to notice, not a verdict — it must not silently demote a row."""
    flagged = _cand("Hollow Knight", score=1.0, pop=0.0, caveats=("a mod of another game",))
    plain = _cand("Hollow Knight", score=1.0, pop=0.0)
    assert rank_candidate(flagged) == rank_candidate(plain)
    assert flagged.caveats


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"game_type": 5}, "a mod of another game"),
        ({"game_type": 14}, "an update to another game"),
        ({"game_type": 11}, "a port of another game"),
        ({"parent_game": 14593}, "an edition of another game"),
    ],
)
def test_igdb_names_a_derivative_from_its_own_fields(row: dict[str, object], expected: str) -> None:
    """Facts IGDB asserts, never a guess about platforms — "Game Boy Color only" reads as
    suspicious for a 2025 release and as ordinary for a retro one, and no rule tells them apart."""
    from release_tracker.sources.igdb import caveats_for

    assert expected in caveats_for(row, ratings=100.0, hypes=0.0)


@pytest.mark.parametrize("game_type", [0, 8, 9, 10])
def test_a_primary_work_carries_no_derivative_caveat(game_type: int) -> None:
    """A remake, remaster or expanded game is a work people search for by name."""
    from release_tracker.sources.igdb import caveats_for

    assert caveats_for({"game_type": game_type}, ratings=100.0, hypes=0.0) == ()


def test_no_audience_at_all_is_worth_saying() -> None:
    from release_tracker.sources.igdb import caveats_for

    assert "no ratings or hype" in caveats_for({"game_type": 0}, ratings=0.0, hypes=0.0)
