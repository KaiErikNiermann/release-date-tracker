"""Tests for title normalisation helpers."""

from __future__ import annotations

import pytest

from release_tracker.titles import (
    normalize,
    search_title,
    split_season,
    split_version,
    title_similarity,
)


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


def test_season_label_is_inverse_of_split_season() -> None:
    from release_tracker.titles import season_label

    assert season_label("Pluribus", 2) == "Pluribus: Season 2"
    assert split_season(season_label("The Boys", 5)) == ("The Boys", 5)  # round-trips


# --- generation markers -----------------------------------------------------------------
@pytest.mark.parametrize(
    ("title", "stem", "token", "ordinal"),
    [
        ("Steam Deck 2", "Steam Deck", "2", 2),
        ("Sony Xperia 1 VIII", "Sony Xperia 1", "VIII", 8),
        # The trailing rule leaves the tier where it belongs: 10 is the line, IV the generation.
        ("Sony Xperia 10 IV", "Sony Xperia 10", "IV", 4),
        ("Nintendo Switch 3", "Nintendo Switch", "3", 3),
    ],
)
def test_split_version_takes_the_trailing_marker(
    title: str, stem: str, token: str, ordinal: int
) -> None:
    got_stem, version = split_version(title)
    assert got_stem == stem
    assert version is not None
    assert (version.token, version.ordinal) == (token, ordinal)


@pytest.mark.parametrize(
    "title",
    [
        # A valid roman numeral (1009) that is plainly a product name. The ordinal bound is
        # what catches it — without one, every MIX would look like a generation.
        "Xiaomi MIX",
        # Single letters are product letters far more often than numerals.
        "Sony Xperia X",
        "ThinkPad X",
        # Not trailing, so the numeral is part of the name.
        "Final Fantasy VII Rebirth",
        # A model code and a year — both out of range for a generation.
        "Nokia 3310",
        "iPhone 2007",
        # "1" is a tier ("Xperia 1"), never a successor marker.
        "Sony Xperia 1",
        "Nintendo Switch",
        # Lowercase roman is far more likely to be a word than a numeral.
        "some device vi",
    ],
)
def test_split_version_leaves_a_name_alone(title: str) -> None:
    assert split_version(title) == (title, None)


def test_split_version_never_returns_an_empty_stem() -> None:
    """A bare marker has no family to look up, so it isn't one."""
    assert split_version("2") == ("2", None)
    assert split_version("VIII") == ("VIII", None)
