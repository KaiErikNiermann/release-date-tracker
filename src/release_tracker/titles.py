"""Title normalisation helpers shared by pullers and the matching layer.

Watchlist titles encode things the canonical DBs don't ("The Boys: Season 5",
"Stranger Things: Finale"). These helpers strip that down to a searchable show /
work title and provide a similarity score for ranking candidates.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Final, Protocol

_SEASON_RE = re.compile(r"^(?P<show>.+?)\s*[:\-]\s*Season\s+(?P<n>\d+)", re.IGNORECASE)
# trailing qualifiers that aren't part of the canonical title
_QUALIFIER_RE = re.compile(
    r"\s*[:\-]\s*(Part\s+[\w\d]+|Finale|Final\s+Season|Vol(?:ume)?\.?\s*\d+|"
    r"Season\s+\d+.*)$",
    re.IGNORECASE,
)
_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")
# parenthetical disambiguators a user might add, e.g. "Lazarus (Anime)"
_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*")


# The counting nouns a season's slices are numbered in. `Arc` is deliberately absent: every
# real one is name-attached and unnumbered ("Reze Arc", "Entertainment District Arc"), so it
# is matched by `_NAMED_SLICE_RE` below instead — "Arc 2" is not a thing anyone ships.
_PART_NOUN = r"(?:Part|Pt\.?|Volume|Vol\.?|Cour|Act|Chapter|Ch\.?|Book)"
_PART_RE = re.compile(rf"\b(?P<label>{_PART_NOUN})\s*(?P<n>\d+)\b", re.IGNORECASE)
# "Act II" — roman only right after a counting noun. `_roman_ordinal` refuses single letters
# because `split_version` has nothing but trailing position to go on; a preceding noun is a far
# stronger anchor, so `I` and `V` are safe here where they are not there.
_PART_ROMAN_RE = re.compile(rf"\b(?P<label>{_PART_NOUN})\s+(?P<r>[IVXL]{{1,4}})\b")
_PART_WORD_RE = re.compile(rf"\b(?P<label>{_PART_NOUN})\s+(?P<w>[A-Za-z]+)\b", re.IGNORECASE)
# A slice that is a *name*, not a number — the anime convention. Never yields a number.
_NAMED_SLICE_RE = re.compile(r"[:\-]\s*(?P<name>[^:\-]+?\s+(?:Arc|Saga))\s*$", re.IGNORECASE)
_ORDINAL_WORDS: Final[dict[str, int]] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
# No franchise ships an "Act XXX"; past this a numeral is far likelier part of a real title.
_MAX_SLICE: Final[int] = 12

DEFAULT_PART_LABEL: Final = "Part"
# Completion fodder, never a constraint — the label column is free text so a one-off
# marketing name ("The Finale") is expressible without a second field.
PART_LABELS: Final[tuple[str, ...]] = (
    "Part",
    "Volume",
    "Vol",
    "Act",
    "Cour",
    "Chapter",
    "Arc",
    "Book",
)

# Freeform: what a person types into a search box, where `_SEASON_RE`'s required ':'/'-'
# separator never appears. End-anchored and requiring the season *word* (or the `s<N>`
# shorthand) is what keeps it off "Dune 2", "Season of the Witch" and every limited series
# that never names a season at all.
_LOOSE_SEASON_RE = re.compile(
    r"^(?P<show>.+?)[\s:,\-]+(?:s|season|series|staffel|saison)\s*(?P<n>\d{1,2})$",
    re.IGNORECASE,
)
# A show can plausibly run to ~50; past that the number is far likelier to be part of a title.
_MAX_INFERRED_SEASON: Final[int] = 50
# whatever separator ran up to the season word, left behind once the number is taken
_TRAILING_PUNCT_RE = re.compile(r"[\s:;,\-\u2013\u2014]+$")


def split_season(title: str) -> tuple[str, int | None]:
    """('The Boys: Season 5') -> ('The Boys', 5); strip Part/Finale qualifiers too."""
    if (m := _SEASON_RE.match(title)) is not None:
        return m.group("show").strip(), int(m.group("n"))
    return _QUALIFIER_RE.sub("", title).strip(), None


def strip_trailing_season(text: str) -> tuple[str, int | None]:
    """('yellowjackets season 2') -> ('yellowjackets', 2); no marker -> (text.strip(), None).

    The freeform counterpart to :func:`split_season`, which needs a ':'/'-' because it parses
    titles *we* wrote. This one parses what someone types, so it must be conservative: the
    stem has to survive non-empty, and season 0 is never inferred — an explicit `season:0`
    still reaches Specials, but no one types "Specials" by accident.

    Returning the stem matters as much as the number: sending "yellowjackets season 2" to
    TMDB's search verbatim is a worse query than "yellowjackets".
    """
    if (m := _LOOSE_SEASON_RE.match(text.strip())) is None:
        return text.strip(), None
    show, n = _TRAILING_PUNCT_RE.sub("", m.group("show")).strip(), int(m.group("n"))
    if not show or not 1 <= n <= _MAX_INFERRED_SEASON:
        return text.strip(), None
    return show, n


class HasCoords(Protocol):
    """The bit of an entity these helpers read — so `titles` stays free of the model layer."""

    title: str
    season: int | None
    part: int | None


def coords_of(entity: HasCoords) -> tuple[int | None, int | None]:
    """A work's (season, part), explicit coord first and the title parsed as a fallback.

    The one ladder, because every consumer needs the same answer and had grown its own copy.
    The fallback is not legacy politeness: most season rows written before ``--season`` existed
    carry the coordinate only in their title, and a reader that trusts the column alone treats
    them as whole shows.

    Freeform parsing runs after :func:`split_season` for the separators it cannot reach —
    "Alien: Earth Season 2" and "Daredevil: Born Again ; Season 3" are both real rows that
    ``_SEASON_RE`` returns nothing for.
    """
    season = entity.season
    if season is None:
        season = split_season(entity.title)[1]
    if season is None:
        season = strip_trailing_season(entity.title)[1]
    return season, entity.part if entity.part is not None else extract_part(entity.title)


@dataclass(frozen=True, slots=True)
class Slice:
    """A mid-season cut as a title writes it.

    ``number`` is None for a *named* slice — the anime convention, where the cut has a title
    rather than an index ("Reze Arc"). One field carries both readings: with a number,
    ``label`` is the counting noun ("Act"); without one, it is the whole name.
    """

    number: int | None
    label: str
    token: str  # verbatim, exactly as the title spelled it

    @property
    def named(self) -> bool:
        return self.number is None


def slice_suffix(part: int | None, label: str | None) -> str:
    """How a slice reads: 'Act 2', 'Part 1', or a bare name. Empty when there is no slice."""
    if part is None:
        return (label or "").strip()
    return f"{(label or DEFAULT_PART_LABEL).strip()} {part}"


def season_label(show: str, season: int) -> str:
    """Canonical season title, e.g. ('Pluribus', 2) -> 'Pluribus: Season 2'.

    The part-less case of :func:`slice_title`, kept because most callers only have a season.
    """
    return slice_title(show, season)


def slice_title(
    show: str,
    season: int | None,
    part: int | None = None,
    label: str | None = None,
) -> str:
    """The canonical title for a coordinate — the inverse of :func:`coords_of`.

    Its absence is why ``--season 5 --part 1`` and ``--part 2`` used to mint the *same* entity
    id: every capture path titled a season row ``season_label(show, season)`` and dropped the
    part, so the two rows collided and the second overwrote the first.

    The two formats are not a fresh design — they are the ones already hand-written in the
    live tracker, so a capture converges onto the row that exists instead of forking a
    near-duplicate. That is the whole migration.

        ('Stranger Things', 5, 1, 'Part') -> 'Stranger Things: Season 5, Part 1'
        ('Arcane: Noxus', None, 1, 'Act') -> 'Arcane: Noxus (Act 1)'
        ('Pluribus', 2)                   -> 'Pluribus: Season 2'
        ('Dune', None)                    -> 'Dune'
    """
    stem = show.strip()
    suffix = slice_suffix(part, label) if (part is not None or label) else ""
    if season is None:
        return f"{stem} ({suffix})" if suffix else stem
    return f"{stem}: Season {season}, {suffix}" if suffix else f"{stem}: Season {season}"


def _slice_number(title: str) -> tuple[int, str, str] | None:
    """The first numbered slice in a title, as (number, label, verbatim token)."""
    for pattern, group in ((_PART_RE, "n"), (_PART_ROMAN_RE, "r"), (_PART_WORD_RE, "w")):
        if (m := pattern.search(title)) is None:
            continue
        raw = m.group(group)
        match group:
            case "n":
                number = int(raw)
            case "r":
                number = _roman_ordinal(raw.upper()) or 0
            case _:
                number = _ORDINAL_WORDS.get(raw.casefold(), 0)
        if 1 <= number <= _MAX_SLICE:
            return number, m.group("label").strip(), m.group(0).strip()
    return None


def extract_slice(title: str) -> Slice | None:
    """The slice a title names, numbered or named, or None.

    Deliberately kind-blind: "Dune: Part Three" really does *say* part three, and a parser
    that lied about that would be the wrong place to fix it. The guard is that every caller
    on the coordinate path gates on ``MediaKind.TV`` — which is also the only reason
    "Stranger Things: Finale" can be a slice while being filed as a movie.
    """
    if (found := _slice_number(title)) is not None:
        number, label, token = found
        return Slice(number, label, token)
    if (m := _NAMED_SLICE_RE.search(title)) is not None:
        name = m.group("name").strip()
        return Slice(None, name, name)
    return None


def extract_part(title: str) -> int | None:
    """('Stranger Things: Season 5, Part 2') -> 2; a mid-season cut (Part/Volume/Cour N)."""
    found = extract_slice(title)
    return found.number if found is not None else None


def search_title(title: str) -> str:
    """Best query string for an API search: drop season/qualifiers and parentheticals.

    Falls through to the freeform parser for the separators :data:`_SEASON_RE` cannot reach,
    so "Alien: Earth Season 2" searches for the show rather than for its own row title.
    """
    base, _ = split_season(title)
    base = strip_trailing_season(base)[0]
    return _PAREN_RE.sub(" ", base).strip() or base


def normalize(title: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace for comparison."""
    text = _PAREN_RE.sub(" ", title.lower())
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def title_similarity(a: str, b: str) -> float:
    """0..1 fuzzy similarity between two titles, normalisation-insensitive."""
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


class VersionKind(enum.StrEnum):
    """How the generation marker was written."""

    ARABIC = "arabic"  # "Steam Deck 2"
    ROMAN = "roman"  # "Sony Xperia 1 VIII"


@dataclass(frozen=True, slots=True)
class Version:
    """A trailing generation marker, as written and as a number."""

    token: str  # exactly as it appeared: "VIII", "2"
    ordinal: int  # 8, 2
    kind: VersionKind


# A generation, not a model number. Sony ships an "Xperia 1"; nobody ships a "Deck 1", and
# "Nokia 3310" / "iPhone 2007" are a model code and a year. Bounding the ordinal is what
# separates the three, and it is also what keeps a roman parse honest: "MIX" is a valid
# numeral (1009), so without the bound "Xiaomi MIX" would split into "Xiaomi" + generation.
_MIN_ORDINAL: Final = 2
_MAX_ORDINAL: Final = 30

_ARABIC_RE: Final = re.compile(r"^\d{1,2}$")
# Strict: rejects "IIII", "VV", "IC". Anchored so a partial match can't slip through.
_ROMAN_RE: Final = re.compile(r"^M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")
_ROMAN_VALUES: Final[dict[str, int]] = {
    "I": 1,
    "V": 5,
    "X": 10,
    "L": 50,
    "C": 100,
    "D": 500,
    "M": 1000,
}


def _roman_ordinal(token: str) -> int | None:
    """Parse an uppercase roman numeral, or None if it isn't one.

    Single letters are refused outright. "X", "V" and "C" are product *letters* far more
    often than numerals — an Xperia X, a ThinkPad X, a Pixel C — and no consumer line
    writes its fifth generation as "V" while writing the rest in arabic.
    """
    if len(token) < 2 or not _ROMAN_RE.match(token):
        return None
    total = 0
    for current, following in zip(token, token[1:] + "\0", strict=True):
        value = _ROMAN_VALUES[current]
        nxt = _ROMAN_VALUES.get(following, 0)
        total += -value if value < nxt else value
    return total


def split_version(title: str) -> tuple[str, Version | None]:
    """('Steam Deck 2') -> ('Steam Deck', Version('2', 2, ARABIC)); no marker -> (title, None).

    Only a *trailing* token counts, which is what keeps this off titles that merely contain
    a numeral: "Final Fantasy VII Rebirth" keeps its VII, while "Sony Xperia 10 IV" splits
    at the IV and leaves the 10 in the stem where it belongs.

    Deliberately not applied to every kind — see the caller. For film and games a trailing
    numeral is usually part of the name ("Dune: Part Two"), and TMDB/IGDB can find those
    anyway; this exists for tech, where nothing else knows the lineage.
    """
    stem, _, last = title.strip().rpartition(" ")
    if not stem or not last:
        return title.strip(), None
    if _ARABIC_RE.match(last):
        ordinal, kind = int(last), VersionKind.ARABIC
    elif (roman := _roman_ordinal(last)) is not None and last.isupper():
        ordinal, kind = roman, VersionKind.ROMAN
    else:
        return title.strip(), None
    if not _MIN_ORDINAL <= ordinal <= _MAX_ORDINAL:
        return title.strip(), None
    return stem.strip(), Version(token=last, ordinal=ordinal, kind=kind)
