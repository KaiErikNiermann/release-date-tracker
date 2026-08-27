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
from typing import Final

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


_PART_RE = re.compile(r"\b(?:Part|Pt\.?|Volume|Vol\.?|Cour)\s*(\d+)\b", re.IGNORECASE)


def split_season(title: str) -> tuple[str, int | None]:
    """('The Boys: Season 5') -> ('The Boys', 5); strip Part/Finale qualifiers too."""
    if (m := _SEASON_RE.match(title)) is not None:
        return m.group("show").strip(), int(m.group("n"))
    return _QUALIFIER_RE.sub("", title).strip(), None


def season_label(show: str, season: int) -> str:
    """Canonical season title, e.g. ('Pluribus', 2) -> 'Pluribus: Season 2'.

    The inverse of :func:`split_season`; used by the explicit ``--season`` capture path so a
    season entry is titled consistently regardless of how the user typed the show name.
    """
    return f"{show.strip()}: Season {season}"


def extract_part(title: str) -> int | None:
    """('Stranger Things: Season 5, Part 2') -> 2; a mid-season cut (Part/Volume/Cour N)."""
    m = _PART_RE.search(title)
    return int(m.group(1)) if m else None


def search_title(title: str) -> str:
    """Best query string for an API search: drop season/qualifiers and parentheticals."""
    base, _ = split_season(title)
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
