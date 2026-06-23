"""Title normalisation helpers shared by pullers and the matching layer.

Watchlist titles encode things the canonical DBs don't ("The Boys: Season 5",
"Stranger Things: Finale"). These helpers strip that down to a searchable show /
work title and provide a similarity score for ranking candidates.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

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
