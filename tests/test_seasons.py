"""Tests for where a requested season stands against what a source lists.

Every fixture is a real show's shape, because the whole point is that the two ways "the API says
N seasons" can be wrong pull in opposite directions. Pluribus was renewed for a second season
before the first aired, so its season 2 is a 404 for a season that exists; Marvel's Daredevil
really will never have a fourth, because the fourth is Born Again's first and lives on another id.

Pure — no database, no network, and `today` is a parameter, so none of this drifts with the clock.
"""

from __future__ import annotations

from datetime import date

import pytest

from release_tracker.seasons import (
    DidYouMean,
    SeasonRef,
    SeasonStanding,
    ShowShape,
    ShowStance,
    Successor,
    check_season,
    pending_seasons,
    rank_successors,
    stance_of,
)

TODAY = date(2026, 8, 31)


def _s(number: int, air: str | None = None, episodes: int = 10) -> SeasonRef:
    return SeasonRef(number, f"Season {number}", date.fromisoformat(air) if air else None, episodes)


def _shape(status: str | None, *seasons: SeasonRef) -> ShowShape:
    return ShowShape("A Show", status, seasons, len(seasons))


# real shapes, measured
_DAREDEVIL = _shape("Ended", _s(1, "2015-04-10"), _s(2, "2016-03-18"), _s(3, "2018-10-19"))
_PLURIBUS = _shape("Returning Series", _s(1, "2025-11-06", 9))
_SEVERANCE = _shape("Returning Series", _s(1, "2022-02-17"), _s(2, "2025-01-16"), _s(3, None, 0))
_YELLOWJACKETS = _shape(
    "Returning Series",
    _s(1, "2021-11-14"),
    _s(2, "2023-03-26"),
    _s(3, "2025-02-16"),
    _s(4, "2026-11-22", 1),
)
_WITCHER = _shape("Returning Series", _s(1, "2019-12-20"), _s(2, "2021-12-17"), _s(3, "2023-06-29"))


# --- the three states ------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "want"),
    [
        ("Ended", ShowStance.FINISHED),
        ("Canceled", ShowStance.FINISHED),
        ("Cancelled", ShowStance.FINISHED),  # both spellings occur
        ("Returning Series", ShowStance.UNCERTAIN),
        ("In Production", ShowStance.UNCERTAIN),
        (None, ShowStance.UNKNOWN),
        ("", ShowStance.UNKNOWN),
        ("Something New", ShowStance.UNKNOWN),  # a word we do not know: fail open
    ],
)
def test_stance_reads_the_sources_own_word(status: str | None, want: ShowStance) -> None:
    assert stance_of(_shape(status, _s(1, "2020-01-01")), TODAY) is want


def test_a_pending_row_is_what_separates_confirmed_from_uncertain() -> None:
    """Both are "Returning Series"; only one has somewhere for the next season to be."""
    assert stance_of(_SEVERANCE, TODAY) is ShowStance.CONFIRMED_NEXT
    assert stance_of(_WITCHER, TODAY) is ShowStance.UNCERTAIN


# --- what counts as pending -------------------------------------------------------------------
def test_both_shapes_of_an_announced_season_count() -> None:
    """Measured: Yellowjackets S4 carries a future date, Severance S3 carries none and 0 episodes.
    A row existing at all is the source saying the season is coming."""
    assert [s.number for s in pending_seasons(_YELLOWJACKETS.seasons, TODAY)] == [4]
    assert [s.number for s in pending_seasons(_SEVERANCE.seasons, TODAY)] == [3]


def test_an_aired_season_is_not_pending() -> None:
    assert pending_seasons(_DAREDEVIL.seasons, TODAY) == ()


def test_specials_are_never_pending() -> None:
    """Season 0 is a bucket, not a season anyone is waiting for."""
    assert pending_seasons((SeasonRef(0, "Specials", None, 0),), TODAY) == ()


# --- where a requested season stands ------------------------------------------------------------
def test_a_listed_season_has_nothing_to_say() -> None:
    verdict = check_season(_YELLOWJACKETS, 4, TODAY, show="Yellowjackets")
    assert verdict.standing is SeasonStanding.LISTED
    assert not verdict.out_of_range
    assert verdict.reasons == ()


def test_a_listed_but_undated_season_says_so() -> None:
    verdict = check_season(_SEVERANCE, 3, TODAY, show="Severance")
    assert verdict.standing is SeasonStanding.LISTED
    assert "no air date" in verdict.reasons[0]


def test_a_finished_show_is_firm_about_a_season_it_lacks() -> None:
    """Marvel's Daredevil really has three. The fourth is Born Again's first, on another id —
    so the claim is true about *this* id, which is all it says."""
    verdict = check_season(_DAREDEVIL, 4, TODAY, show="Marvel's Daredevil")
    assert verdict.standing is SeasonStanding.BEYOND_END
    assert verdict.firm
    assert "carries no season 4" in verdict.reasons[0]
    assert "Ended" in verdict.reasons[0]


def test_a_running_show_is_never_firm() -> None:
    """The Pluribus regression, and the reason the whole soft/firm split exists.

    Renewed for a second season before the first aired, so `/tv/225171/season/2` is a 404 for a
    season that genuinely exists. Saying "there is no season 2" here would be flatly wrong.
    """
    verdict = check_season(_PLURIBUS, 2, TODAY, show="Pluribus")
    assert verdict.standing is SeasonStanding.BEYOND_LISTED
    assert not verdict.firm
    said = " ".join(verdict.reasons).casefold()
    assert "not listed yet" in said
    for never in ("carries no", "does not exist", "never", "cancel"):
        assert never not in said


def test_an_unknown_status_stays_soft() -> None:
    """Fail open: a status word we do not recognise must not become a firm denial."""
    verdict = check_season(_shape("Who Knows", _s(1, "2020-01-01")), 2, TODAY, show="X")
    assert not verdict.firm
    assert verdict.stance is ShowStance.UNKNOWN


def test_a_confirmed_next_season_is_named_when_asking_past_it() -> None:
    """Asking for 4 of a show whose 3 is announced should say the 3 is coming."""
    said = " ".join(check_season(_SEVERANCE, 4, TODAY, show="Severance").reasons)
    assert "season 4 is not listed yet" in said
    assert "season 3 is announced, no date yet" in said


def test_the_boundary_is_the_highest_listed_season() -> None:
    for asked, standing in ((3, SeasonStanding.LISTED), (4, SeasonStanding.BEYOND_END)):
        assert check_season(_DAREDEVIL, asked, TODAY, show="D").standing is standing


def test_specials_are_reachable_by_asking_for_zero() -> None:
    shape = _shape("Ended", SeasonRef(0, "Specials", None, 3), _s(1, "2020-01-01"))
    assert check_season(shape, 0, TODAY, show="X").standing is SeasonStanding.LISTED
    assert shape.highest == 1  # but they never count toward the end


def test_a_show_with_no_seasons_listed_is_still_soft() -> None:
    verdict = check_season(_shape("Returning Series"), 1, TODAY, show="Announced Thing")
    assert verdict.highest == 0
    assert not verdict.firm


def test_the_quote_is_the_sources_own_title() -> None:
    """Quoting the query back ("pluribus") reads as our claim; TMDB's "Pluribus" reads as theirs."""
    shape = ShowShape("Pluribus", "Returning Series", (_s(1, "2025-11-06", 9),), 1)
    assert "“Pluribus”" in check_season(shape, 2, TODAY).reasons[0]
    # an explicit caller still wins, and a nameless shape degrades rather than crashing
    assert "“Mine”" in check_season(shape, 2, TODAY, show="Mine").reasons[0]
    nameless = ShowShape(None, "Returning Series", (_s(1, "2025-11-06", 9),), 1)
    assert "this show" in check_season(nameless, 2, TODAY).reasons[0]


# --- which show carries the season the base one does not ----------------------------------------
def _succ(title: str, shared: int, *, year: int = 2020, seasons: int = 1) -> Successor:
    return Successor(title, title, year, seasons, shared)


def test_shared_cast_orders_the_offer() -> None:
    """The measured Dexter numbers. Cheaper signals were tried and are worse: ranking by debut,
    shared title words and vote count puts a zero-vote "Dexter Procter" above New Blood."""
    ranked, _ = rank_successors(
        "Dexter",
        [
            _succ("Dexter: Original Sin", 1),
            _succ("Dexter: New Blood", 4),
            _succ("Dexter: Resurrection", 3),
        ],
    )
    assert [s.title for s in ranked] == [
        "Dexter: New Blood",
        "Dexter: Resurrection",
        "Dexter: Original Sin",
    ]


def test_a_stranger_is_dropped_and_named() -> None:
    """Dexter's Laboratory shares nobody. Silently omitting it would leave the reader unable to
    tell a narrowed pool from an empty one."""
    ranked, reasons = rank_successors(
        "Dexter", [_succ("Dexter: New Blood", 4), _succ("Dexter's Laboratory", 0)]
    )
    assert [s.title for s in ranked] == ["Dexter: New Blood"]
    assert "Dexter's Laboratory" in reasons[0]
    assert "share no cast" in reasons[0]


def test_a_single_overlap_is_offered_but_flagged() -> None:
    """Doctor Who 2005 -> 2024 shares exactly one name, because the whole cast turns over at a
    regeneration — and it is unmistakably a continuation."""
    (only,), _ = rank_successors("Doctor Who", [_succ("Doctor Who (2024)", 1)])
    assert only.shared_cast == 1
    assert any("coincidence" in r for r in only.reasons)


def test_the_offset_is_the_base_shows_last_season() -> None:
    """What a `continues` edge would record, and what renumbers the ask onto the successor."""
    ask = DidYouMean(check_season(_DAREDEVIL, 4, TODAY), (_succ("Born Again", 5, seasons=2),))
    assert ask.after == 3
    assert ask.native(ask.offer[0]) == 1  # season 4 of the continuity is Born Again's first


def test_a_season_below_the_offset_has_no_landing() -> None:
    """Asking for season 2 of a franchise whose base ran three cannot land on the successor."""
    ask = DidYouMean(check_season(_DAREDEVIL, 2, TODAY), (_succ("Born Again", 5, seasons=2),))
    assert ask.native(ask.offer[0]) is None
