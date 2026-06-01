"""Tests for title normalisation helpers."""

from __future__ import annotations

from release_tracker.titles import normalize, search_title, split_season, title_similarity


def test_split_season_extracts_show_and_number() -> None:
    assert split_season("The Boys: Season 5") == ("The Boys", 5)
    assert split_season("Severance: Season 3") == ("Severance", 3)


def test_split_season_strips_qualifiers_without_number() -> None:
    assert split_season("Stranger Things: Finale") == ("Stranger Things", None)
    assert split_season("Dune: Part Two")[0] == "Dune"
    assert split_season("Slay the Spire 2") == ("Slay the Spire 2", None)  # bare number kept


def test_search_title_drops_parentheticals() -> None:
    assert search_title("Lazarus (Anime)") == "Lazarus"
    assert search_title("The Boys: Season 5") == "The Boys"


def test_normalize_is_punctuation_insensitive() -> None:
    assert normalize("Mission: Impossible - The Final Reckoning") == normalize(
        "mission impossible the final reckoning"
    )


def test_similarity_ranks_exact_over_partial() -> None:
    exact = title_similarity("Marvel's Blade", "Marvel's Blade")
    partial = title_similarity("Marvel's Blade", "Mount & Blade II")
    assert exact == 1.0
    assert exact > partial
