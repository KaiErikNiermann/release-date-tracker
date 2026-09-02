"""Tests for numbering a film within its franchise.

Every fixture is a real TMDB collection, because the thing being tested is exactly where
the naive answer goes wrong. Position after sorting by release date matches the marketed
number on most franchises and breaks on precisely one shape — a collection carrying a side
film — so both are here, and Fast & Furious is the regression the whole module exists for.

The numbers below were measured against the live API, including the two that make the
feature honest: TMDB numbers none of these entries itself, and its ``parts[]`` is not even
sorted (Fast & Furious comes back 2003, 2006, 2001, 2011…).

Pure: no database, no network, and ``today`` is a parameter.
"""

from __future__ import annotations

import io
from datetime import date
from typing import cast

import pytest

from release_tracker.franchise import (
    Entry,
    EntryStanding,
    FranchiseShape,
    Placement,
    check_entry,
    movie_stance,
    ordinal_of,
    place,
)
from release_tracker.models import Stance

TODAY = date(2026, 9, 1)


def _e(title: str, when: str | None, *, spinoff: bool = False, status: str = "Released") -> Entry:
    return Entry(
        key=title,
        title=title,
        released=date.fromisoformat(when) if when else None,
        status=status,
        spinoff=spinoff,
    )


# The real collection, in the real (unsorted) order TMDB returns, with the one entry it
# marks `spin off`. Marketed numbers: F9 is the ninth film, Fast X the tenth.
_FAST = FranchiseShape(
    "The Fast and the Furious Collection",
    (
        _e("2 Fast 2 Furious", "2003-06-05"),
        _e("The Fast and the Furious: Tokyo Drift", "2006-06-03"),
        _e("The Fast and the Furious", "2001-06-22"),
        _e("Fast & Furious", "2009-04-02"),
        _e("Fast Five", "2011-04-20"),
        _e("Fast & Furious 6", "2013-05-21"),
        _e("Furious 7", "2015-04-01"),
        _e("The Fate of the Furious", "2017-04-12"),
        _e("Fast & Furious Presents: Hobbs & Shaw", "2019-07-31", spinoff=True),
        _e("F9", "2021-05-19"),
        _e("Fast X", "2023-05-17"),
        _e("Fast Forever", "2028-03-16", status="Planned"),
    ),
)

_TOY_STORY = FranchiseShape(
    "Toy Story Collection",
    (
        _e("Toy Story", "1995-11-22"),
        _e("Toy Story 2", "1999-10-30"),
        _e("Toy Story 3", "2010-06-16"),
        _e("Toy Story 4", "2019-06-19"),
        _e("Toy Story 5", "2026-06-17"),
    ),
)

_DUNE = FranchiseShape(
    "Dune Collection",
    (
        _e("Dune", "2021-09-15"),
        _e("Dune: Part Two", "2024-02-27"),
        _e("Dune: Part Three", "2026-12-15", status="Post Production"),
    ),
)


# --- the regression the module exists for -------------------------------------------------
def test_a_spinoff_does_not_push_the_numbers_along() -> None:
    """Hobbs & Shaw sits ninth by date. Counting it makes F9 the tenth film and Fast X the
    eleventh — wrong for both, and wrong for every entry after it forever."""
    assert ordinal_of(_FAST, "F9") == 9
    assert ordinal_of(_FAST, "Fast X") == 10
    assert ordinal_of(_FAST, "Fast Forever") == 11


def test_a_spinoff_has_no_number_rather_than_a_shared_one() -> None:
    """It is not the ninth Fast & Furious film. Giving it 9 anyway would be the exact error
    this module exists to avoid, one step further along."""
    assert ordinal_of(_FAST, "Fast & Furious Presents: Hobbs & Shaw") is None


def test_dropping_the_spinoff_is_said_not_done_silently() -> None:
    """The keyword is crowd-sourced, so the reader has to be able to check it."""
    said = " ".join(check_entry(_FAST, 9, TODAY).reasons)
    assert "Hobbs & Shaw" in said
    assert "spin-off" in said


@pytest.mark.parametrize(
    ("shape", "title", "want"),
    [
        (_TOY_STORY, "Toy Story", 1),
        (_TOY_STORY, "Toy Story 5", 5),
        (_DUNE, "Dune: Part Three", 3),  # the number is spelled as a word
        (_FAST, "The Fast and the Furious", 1),  # unsorted input; 2001 really is first
    ],
)
def test_release_order_matches_the_marketed_number(
    shape: FranchiseShape, title: str, want: int
) -> None:
    assert ordinal_of(shape, title) == want


def test_the_parts_list_is_sorted_before_anything_reads_it() -> None:
    """TMDB's `parts[]` is genuinely unordered, so indexing it directly is always wrong."""
    assert [e.title for e in _FAST.ordered][:3] == [
        "The Fast and the Furious",
        "2 Fast 2 Furious",
        "The Fast and the Furious: Tokyo Drift",
    ]


# --- where a requested entry stands --------------------------------------------------------
def test_a_listed_entry_has_nothing_to_report() -> None:
    verdict = check_entry(_TOY_STORY, 5, TODAY)
    assert verdict.standing is EntryStanding.LISTED
    assert not verdict.out_of_range
    assert verdict.listed is not None
    assert verdict.listed.title == "Toy Story 5"
    assert verdict.reasons == ()


def test_an_unlisted_entry_says_how_it_counted() -> None:
    verdict = check_entry(_TOY_STORY, 6, TODAY)
    assert verdict.standing is EntryStanding.BEYOND_LISTED
    assert verdict.highest == 5
    assert "release order" in verdict.reasons[0]


def test_nothing_here_is_ever_firm() -> None:
    """A collection carries no status, and a franchise is never formally cancelled — it
    stops, and Shrek restarted fifteen years later."""
    assert check_entry(_TOY_STORY, 99, TODAY).firm is False
    assert check_entry(_FAST, 99, TODAY).firm is False


def test_an_announced_entry_is_named_when_asking_past_it() -> None:
    """Fast Forever is dated 2028 and counts toward the total, so asking for 12 should be
    told what the 11th is rather than just a number."""
    said = " ".join(check_entry(_FAST, 12, TODAY).reasons)
    assert "Fast Forever" in said
    assert "2028-03-16" in said


def test_a_listed_but_unbacked_entry_is_flagged() -> None:
    """Being in a collection is not the same as being made."""
    shape = FranchiseShape("X", (_e("One", "2020-01-01"), _e("Two", None, status="Rumored")))
    verdict = check_entry(shape, 2, TODAY)
    assert verdict.standing is EntryStanding.LISTED
    assert verdict.stance is Stance.UNCERTAIN
    assert "Rumored" in " ".join(verdict.reasons)


# --- shape and degenerate input --------------------------------------------------------------
def test_an_undated_entry_sorts_last_rather_than_first() -> None:
    """Announced but unscheduled is where it belongs in a sequence; sorting it to the front
    would renumber everything behind it."""
    shape = FranchiseShape("X", (_e("Later", None), _e("First", "2020-01-01")))
    assert [e.title for e in shape.ordered] == ["First", "Later"]


def test_an_empty_collection_answers_rather_than_crashing() -> None:
    verdict = check_entry(FranchiseShape("X", ()), 1, TODAY)
    assert verdict.highest == 0
    assert verdict.listed is None
    assert not verdict.firm


def test_a_film_outside_the_collection_has_no_ordinal() -> None:
    assert ordinal_of(_TOY_STORY, "Shrek") is None


def test_the_status_ladder_reaches_the_entry() -> None:
    """`Entry.stance` is the same mapping the movie pull uses, so a collection member and a
    directly-looked-up film cannot disagree about what TMDB said."""
    assert _e("X", None, status="Post Production").stance is movie_stance("Post Production")
    assert _e("X", None, status="Rumored").stance is Stance.UNCERTAIN
    assert _e("X", None, status=None).stance is None  # pyright: ignore[reportArgumentType]


# --- numbering the film we looked up, rather than asking for a number ---------------------
def test_the_film_we_asked_about_gets_its_number() -> None:
    placement = place(_FAST, "Fast X")
    assert placement.entry == 10
    assert placement.highest == 11
    assert placement.name == "The Fast and the Furious Collection"


def test_the_basis_is_stated_on_every_placement() -> None:
    """TMDB numbers none of these films, so a bare number would be an unattributed guess.
    Ice Age and Fast & Furious both had one moved by a spin-off before the filter went in."""
    for shape, key in ((_FAST, "Fast X"), (_TOY_STORY, "Toy Story"), (_DUNE, "Dune")):
        assert "release date" in place(shape, key).reasons[0]


def test_a_spinoff_is_told_it_has_no_number_rather_than_given_one() -> None:
    placement = place(_FAST, "Fast & Furious Presents: Hobbs & Shaw")
    assert placement.entry is None
    assert placement.highest == 11
    assert "spin-off" in " ".join(placement.reasons)


def test_a_spinoff_is_not_also_listed_as_something_it_left_out() -> None:
    """The film being asked about is told "this one is not numbered". Naming it again in the
    left-out line would read as being about some *other* film in the collection."""
    said = place(_FAST, "Fast & Furious Presents: Hobbs & Shaw").reasons
    assert not any("not counted" in r for r in said)
    assert len(said) == 2


def test_a_collection_with_no_spinoff_says_nothing_about_one() -> None:
    assert len(place(_TOY_STORY, "Toy Story 3").reasons) == 1


def test_a_film_the_collection_does_not_list_is_said_so() -> None:
    """Defensive: the collection is read *off* the film, so this should not arise — and if it
    does, a silent `entry: None` would look identical to a spin-off."""
    assert "not among the films" in " ".join(place(_TOY_STORY, "Shrek").reasons)


def test_the_placement_survives_the_json_boundary() -> None:
    """`--json` is the /rd skill's contract; the reasons must cross it intact."""
    out = place(_FAST, "F9").to_dict()
    assert out["entry"] == 9
    assert out["highest"] == 11
    reasons = out["reasons"]
    assert isinstance(reasons, list)
    assert any("spin-off" in str(r) for r in cast("list[object]", reasons))


# --- the surfaces ---------------------------------------------------------------------------
def _rendered(placement: Placement | None) -> str:
    from rich.console import Console

    from release_tracker import cli

    console = Console(file=io.StringIO(), width=200, no_color=True)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "console", console)
        cli._render_franchise(placement)  # pyright: ignore[reportPrivateUsage]
    out = console.file
    assert isinstance(out, io.StringIO)
    return out.getvalue()


def test_the_cli_prints_the_number_and_what_it_rests_on() -> None:
    """The reasons are load-bearing on this line, not decoration: the number is ours."""
    said = _rendered(place(_FAST, "Fast X"))
    assert "entry 10 of 11" in said
    assert "The Fast and the Furious Collection" in said
    assert "release date" in said
    assert "Hobbs & Shaw" in said


def test_the_cli_does_not_invent_a_number_for_a_spinoff() -> None:
    said = _rendered(place(_FAST, "Fast & Furious Presents: Hobbs & Shaw"))
    assert "unnumbered" in said
    assert "entry" not in said


def test_a_standalone_film_gets_no_section_at_all() -> None:
    """`--franchise` on a film in no collection must print nothing, not an empty heading."""
    assert _rendered(None) == ""
