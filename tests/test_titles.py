"""Tests for title normalisation helpers."""

from __future__ import annotations

import pytest

from release_tracker.titles import (
    coords_of,
    normalize,
    search_title,
    split_season,
    split_version,
    strip_trailing_season,
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


# --- freeform season inference ------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("yellowjackets season 2", ("yellowjackets", 2)),
        ("Andor s2", ("Andor", 2)),
        ("the boys season 5", ("the boys", 5)),
        ("Reacher Staffel 3", ("Reacher", 3)),
        # separators `_SEASON_RE` cannot handle — the live db has both of these
        ("Alien: Earth Season 2", ("Alien: Earth", 2)),
        ("Daredevil: Born Again ; Season 3", ("Daredevil: Born Again", 3)),
    ],
)
def test_strip_trailing_season_reads_what_a_person_types(text: str, want: tuple[str, int]) -> None:
    assert strip_trailing_season(text) == want


@pytest.mark.parametrize(
    "text",
    [
        "Season of the Witch",  # the word, but not as a coordinate
        "Dune 2",  # a sequel number is not a season
        "GTA 6",
        "season 2",  # no stem left — nothing to search for
        "Silo",
        "Show season 99",  # past any plausible run; likelier part of a title
        "Andor season 0",  # specials are never *inferred*; explicit `season:0` still works
    ],
)
def test_strip_trailing_season_declines_when_it_is_not_a_coordinate(text: str) -> None:
    """A false positive silently files a film as a TV season, so the parser stays timid."""
    assert strip_trailing_season(text) == (text, None)


# --- the shared coordinate ladder ---------------------------------------------------------
class _Work:
    """The bit of an entity `coords_of` reads."""

    def __init__(self, title: str, season: int | None = None, part: int | None = None) -> None:
        self.title, self.season, self.part = title, season, part


def test_coords_of_prefers_the_stored_coordinate() -> None:
    """An explicit `--season` is authoritative over whatever the title happens to say."""
    assert coords_of(_Work("Reacher: Season 1", season=3)) == (3, None)


def test_coords_of_falls_back_to_the_title() -> None:
    """Most season rows written before `--season` existed carry it only in their title."""
    assert coords_of(_Work("Reacher: Season 3")) == (3, None)


@pytest.mark.parametrize(
    ("title", "season"),
    [("Alien: Earth Season 2", 2), ("Daredevil: Born Again ; Season 3", 3)],
)
def test_coords_of_reaches_separators_split_season_cannot(title: str, season: int) -> None:
    """Both are real rows in the live db that `_SEASON_RE` returns nothing for."""
    assert coords_of(_Work(title))[0] == season


def test_coords_of_reads_a_mid_season_part() -> None:
    assert coords_of(_Work("Stranger Things: Season 5, Part 2")) == (5, 2)


def test_search_title_strips_a_season_it_could_not_parse_before() -> None:
    """Otherwise the row searches an API for its own title, season words and all."""
    assert search_title("Alien: Earth Season 2") == "Alien: Earth"
