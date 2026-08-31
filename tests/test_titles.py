"""Tests for title normalisation helpers."""

from __future__ import annotations

import pytest

from release_tracker.titles import (
    coords_of,
    extract_part,
    extract_slice,
    normalize,
    search_title,
    season_label,
    slice_suffix,
    slice_title,
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


# --- the slice coordinate and its title ----------------------------------------------------
@pytest.mark.parametrize(
    ("args", "want"),
    [
        (("Stranger Things", 5, 1, "Part"), "Stranger Things: Season 5, Part 1"),
        (("Arcane: Noxus", None, 1, "Act"), "Arcane: Noxus (Act 1)"),
        (("Pluribus", 2), "Pluribus: Season 2"),
        (("Dune", None), "Dune"),
        (("Show", 5, 3, "Volume"), "Show: Season 5, Volume 3"),
        (("Show", 5, None, "The Finale"), "Show: Season 5, The Finale"),
    ],
)
def test_slice_title_formats(args: tuple[object, ...], want: str) -> None:
    assert slice_title(*args) == want  # type: ignore[arg-type]


def test_slice_title_reproduces_the_titles_already_in_the_tracker() -> None:
    """The no-backfill guarantee.

    `Entity.make_id` hashes the title, so generating the format the user already types by
    hand makes a capture converge onto the row that exists instead of forking a duplicate.
    These three ids are the live ones; if this test fails, the migration story is broken.
    """
    from release_tracker.models import Entity, MediaKind

    live = {
        slice_title("Stranger Things", 5, 1, "Part"): "tv-stranger-things-season-5-part-1-c287b9",
        slice_title("Stranger Things", 5, 2, "Part"): "tv-stranger-things-season-5-part-2-9cfdaf",
        slice_title("Arcane: Noxus", None, 1, "Act"): "tv-arcane-noxus-act-1-b9ed54",
    }
    assert {t: Entity.create(t, MediaKind.TV).id for t in live} == live


def test_two_parts_of_one_season_are_two_rows() -> None:
    """The collision this whole change exists to fix: both used to mint the same id."""
    from release_tracker.models import Entity, MediaKind

    a = Entity.create(slice_title("Stranger Things", 5, 1), MediaKind.TV, season=5, part=1)
    b = Entity.create(slice_title("Stranger Things", 5, 2), MediaKind.TV, season=5, part=2)
    assert a.id != b.id


def test_season_label_is_the_partless_case() -> None:
    """Kept as its own name because most callers only ever have a season."""
    assert season_label("Pluribus", 2) == slice_title("Pluribus", 2)


@pytest.mark.parametrize(
    ("title", "number", "label"),
    [
        ("Stranger Things: Season 5, Part 2", 2, "Part"),
        ("Arcane: Noxus (Act 1)", 1, "Act"),
        ("Show: Act II", 2, "Act"),
        ("Show: Part Three", 3, "Part"),
        ("Frieren: Cour 2", 2, "Cour"),
        ("Show: Chapter 4", 4, "Chapter"),
        ("Show: Vol. 3", 3, "Vol."),
    ],
)
def test_extract_slice_reads_a_numbered_cut(title: str, number: int, label: str) -> None:
    found = extract_slice(title)
    assert found is not None
    assert (found.number, found.label) == (number, label)


@pytest.mark.parametrize(
    "title", ["Chainsaw Man: Reze Arc", "Demon Slayer: Entertainment District Arc"]
)
def test_extract_slice_reads_a_named_cut(title: str) -> None:
    """The anime convention: the cut has a title, not an index. "Arc 2" is not a thing."""
    found = extract_slice(title)
    assert found is not None and found.named
    assert found.label.endswith("Arc")


@pytest.mark.parametrize("title", ["Yellowjackets", "Pluribus: Season 2", "Severance"])
def test_extract_slice_declines_when_there_is_no_cut(title: str) -> None:
    assert extract_slice(title) is None


def test_a_films_part_is_parsed_but_never_becomes_a_coordinate() -> None:
    """ "Dune: Part Three" really does say part three, and a parser that denied it would be
    the wrong place to fix this. The guard is that the coordinate path is TV-gated — which
    is also the only reason "Stranger Things: Finale" can be a slice while being a movie."""
    from release_tracker.models import Entity, MediaKind

    assert extract_part("Dune: Part Three") == 3
    film = Entity.create("Dune: Part Three", MediaKind.MOVIE)
    assert (film.season, film.part) == (None, None)


@pytest.mark.parametrize(
    ("part", "label", "want"),
    [
        (2, "Act", "Act 2"),
        (1, None, "Part 1"),
        (None, "The Finale", "The Finale"),
        (None, None, ""),
    ],
)
def test_slice_suffix_reads_both_ways(part: int | None, label: str | None, want: str) -> None:
    assert slice_suffix(part, label) == want
