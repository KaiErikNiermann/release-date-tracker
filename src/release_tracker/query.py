"""Discord-style query language over tracked works.

Pure — no I/O, no database, no terminal. ``cli.py`` and the TUI both parse the same strings
and apply the same predicates, so the two frontends cannot drift apart::

    reacher kind:tv cast:"Alan Ritchson" year:2026
    "the odyssey" -tag:comedy is:upcoming
    tag:"body horror" genre:sci-fi year:2020..2026

A bare (or quoted) word is an implicit ``name:`` term; everything else is ``field:value``.
Terms are AND-ed, comma-separated values within one term are OR-ed, and a leading ``-``
negates the term.

The field vocabulary is *derived from the enums* (:class:`CreditRole`,
:class:`DescriptorKind`, :class:`MediaKind`, ...), so adding a credit role or descriptor
kind grows the language with no edit here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import TYPE_CHECKING, Literal

from release_tracker.models import (
    Bucket,
    ConsumptionState,
    CreditRole,
    DescriptorKind,
    MediaKind,
)

if TYPE_CHECKING:  # pragma: no cover - typing only; avoids a runtime import cycle
    from release_tracker.views import PlatformLine, TrackRow

__all__ = [
    "BARE_FIELD",
    "FIELDS",
    "NumRange",
    "Query",
    "Suggestion",
    "Term",
    "Token",
    "VocabEntry",
    "Vocabulary",
    "active_span",
    "apply",
    "canonical_field",
    "filter_rows",
    "lex",
    "matches",
    "parse",
    "rank_values",
    "suggest",
]

BARE_FIELD = "name"
QUOTE = '"'

# --- field vocabulary ---------------------------------------------------------------------
# Derived from the enums: a new CreditRole member is a new query field, for free.
_ROLE_FIELDS: dict[str, CreditRole] = {r.value: r for r in CreditRole}
_DESCRIPTOR_FIELDS: dict[str, DescriptorKind] = {d.value: d for d in DescriptorKind}

_CORE_FIELDS: frozenset[str] = frozenset(
    {
        BARE_FIELD,
        "kind",
        "state",
        "is",
        "tag",
        "platform",
        "region",
        "series",
        "person",
        "year",
        "season",
        "part",
    }
)

FIELDS: frozenset[str] = _CORE_FIELDS | frozenset(_ROLE_FIELDS) | frozenset(_DESCRIPTOR_FIELDS)

# A role and a descriptor kind sharing a name would silently shadow one another in
# `_match_term`, so fail loudly at import rather than answering queries wrongly.
_COLLISIONS = (_CORE_FIELDS & frozenset(_ROLE_FIELDS)) | (
    frozenset(_ROLE_FIELDS) & frozenset(_DESCRIPTOR_FIELDS)
)
if _COLLISIONS:  # pragma: no cover - a guard against future enum edits
    raise RuntimeError(f"query field name collision: {sorted(_COLLISIONS)}")

_FIELD_ALIASES: dict[str, str] = {
    "title": BARE_FIELD,
    "on": "platform",
    "platforms": "platform",
    "regions": "region",
    "country": "region",
    "in": "region",
    "credit": "person",
    "people": "person",
    "who": "person",
    "tags": "tag",
    "genres": "genre",
    "themes": "theme",
    "actor": CreditRole.CAST.value,
    "actors": CreditRole.CAST.value,
    "star": CreditRole.CAST.value,
    "stars": CreditRole.CAST.value,
    "studios": CreditRole.STUDIO.value,
    "dev": CreditRole.DEVELOPER.value,
    "devs": CreditRole.DEVELOPER.value,
    "directors": CreditRole.DIRECTOR.value,
    "writers": CreditRole.WRITER.value,
    "composers": CreditRole.COMPOSER.value,
}

# ``anime`` is a medium, not a kind (see MediaKind's note) — desugar `kind:anime` to its
# origin tag instead of matching nothing.
_KIND_TO_ORIGIN: dict[str, str] = {"anime": "anime"}

_KIND_ALIASES: dict[str, MediaKind] = {
    "tv-show": MediaKind.TV,
    "tvshow": MediaKind.TV,
    "show": MediaKind.TV,
    "shows": MediaKind.TV,
    "series": MediaKind.TV,
    "film": MediaKind.MOVIE,
    "films": MediaKind.MOVIE,
    "movies": MediaKind.MOVIE,
    "games": MediaKind.GAME,
    "gadget": MediaKind.TECH,
    "hardware": MediaKind.TECH,
}

_IS_BUCKETS: frozenset[str] = frozenset(b.value for b in Bucket)
_IS_FLAGS: frozenset[str] = frozenset(
    {
        "blocked",
        "notes",
        "dated",
        "tba",
        "confirmed",
        "speculative",
        "predicted",
        "fresh",
        "aging",
        "stale",
    }
)
_IS_VALUES: frozenset[str] = _IS_BUCKETS | _IS_FLAGS

_NUMERIC_FIELDS: frozenset[str] = frozenset({"year", "season", "part"})


# --- lexing -------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Token:
    """One lexed token plus its source span, so completion can splice by offset."""

    text: str  # quote characters stripped
    start: int  # inclusive offset into the source
    end: int  # exclusive


def lex(source: str) -> tuple[Token, ...]:
    """Split a query into tokens. **Never raises.**

    Hand-rolled rather than ``shlex`` for three reasons, all of which matter for a *media*
    search box:

    * ``shlex`` treats ``'`` as a quote, so ``Don't Look Up`` raises — and closing the quote
      to recover silently mangles it to ``Dont Look Up``. Here only ``"`` groups; an
      apostrophe is always a literal character.
    * An unterminated quote is the normal state while typing ``cast:"Alan Rit``, not an error.
    * ``shlex`` discards offsets, and cursor-aware completion needs spans — using it would
      mean a second lexer for completion, which could then disagree with this one.
    """
    tokens: list[Token] = []
    i, n = 0, len(source)
    while i < n:
        while i < n and source[i].isspace():
            i += 1
        if i >= n:
            break
        start = i
        chars: list[str] = []
        quoted = False
        while i < n:
            ch = source[i]
            if ch == QUOTE:
                quoted = not quoted
            elif ch.isspace() and not quoted:
                break
            else:
                chars.append(ch)
            i += 1
        tokens.append(Token(text="".join(chars), start=start, end=i))
    return tuple(tokens)


def active_span(source: str, cursor: int) -> tuple[int, int]:
    """The ``[start, end)`` span of the token under ``cursor`` (empty span on whitespace)."""
    c = max(0, min(cursor, len(source)))
    for tok in lex(source):
        if tok.start <= c <= tok.end:
            return tok.start, tok.end
    return c, c


# --- parsed shapes ------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class NumRange:
    """An inclusive numeric window; an open end is ``None`` (``year:>=2026``)."""

    lo: int | None = None
    hi: int | None = None

    def contains(self, n: int) -> bool:
        return (self.lo is None or n >= self.lo) and (self.hi is None or n <= self.hi)


@dataclass(frozen=True, slots=True)
class Term:
    """One clause. ``values`` are OR-ed; ``negated`` inverts the whole term."""

    field: str
    values: tuple[str, ...] = ()
    ranges: tuple[NumRange, ...] = ()
    negated: bool = False


@dataclass(frozen=True, slots=True)
class Query:
    """A parsed query. Drives *both* local filtering and the external add-search."""

    terms: tuple[Term, ...] = ()
    unknown_fields: tuple[str, ...] = ()

    def _first(self, field: str) -> Term | None:
        return next((t for t in self.terms if t.field == field and not t.negated), None)

    @property
    def is_empty(self) -> bool:
        return not self.terms

    @property
    def text(self) -> str:
        """The bare words, joined — the free-text query for an external source search."""
        return " ".join(v for t in self.terms if t.field == BARE_FIELD for v in t.values)

    @property
    def external(self) -> str:
        """The sub-query an external source search understands: free text plus the hints.

        Filters that only mean something locally (``is:``, ``tag:``, ``cast:``, ``state:``)
        drop out — they narrow what you already track, not what TMDB returns. Carrying the
        rest over is what lets you refine a search in the browse bar and hand it straight
        to the add palette without retyping.
        """
        parts = [self.text]
        if (kind := self.kind_hint) is not None:
            parts.append(f"kind:{kind.value}")
        if (year := self.year_hint) is not None:
            parts.append(f"year:{year}")
        if (season := self.season_hint) is not None:
            parts.append(f"season:{season}")
        if (part := self.part_hint) is not None:
            parts.append(f"part:{part}")
        return " ".join(p for p in parts if p)

    @property
    def kind_hint(self) -> MediaKind | None:
        """``kind:`` as a :class:`MediaKind`, for ``capture_candidates(kind_hint=...)``."""
        term = self._first("kind")
        return as_kind(term.values[0]) if term and term.values else None

    @property
    def year_hint(self) -> int | None:
        """``year:`` as an exact year, for ``select_candidate(want_year=...)``."""
        return _exact(self._first("year"))

    @property
    def season_hint(self) -> int | None:
        return _exact(self._first("season"))

    @property
    def part_hint(self) -> int | None:
        """``part:`` as an exact mid-season cut, for the capture coords."""
        return _exact(self._first("part"))


def _exact(term: Term | None) -> int | None:
    """The single year/season a term pins, or None when it's a range or absent."""
    if term is None or len(term.ranges) != 1:
        return None
    rng = term.ranges[0]
    return rng.lo if rng.lo is not None and rng.lo == rng.hi else None


def as_kind(value: str) -> MediaKind | None:
    """Resolve a user-typed kind (``tv-show``, ``film``, ``movie``) to a :class:`MediaKind`."""
    key = value.strip().lower()
    if (alias := _KIND_ALIASES.get(key)) is not None:
        return alias
    try:
        return MediaKind(key)
    except ValueError:
        return None


# --- parsing ------------------------------------------------------------------------------
def _parse_range(value: str) -> NumRange | None:
    """``2026`` / ``2020..2026`` / ``>=2026`` / ``<2030`` -> a window, or None if unparseable."""
    v = value.strip()
    try:
        if v.startswith(">="):
            return NumRange(lo=int(v[2:]))
        if v.startswith("<="):
            return NumRange(hi=int(v[2:]))
        if v.startswith(">"):
            return NumRange(lo=int(v[1:]) + 1)
        if v.startswith("<"):
            return NumRange(hi=int(v[1:]) - 1)
        if ".." in v:
            lo, _, hi = v.partition("..")
            return NumRange(
                lo=int(lo) if lo.strip() else None,
                hi=int(hi) if hi.strip() else None,
            )
        n = int(v)
    except ValueError:
        return None
    return NumRange(lo=n, hi=n)


def parse(source: str) -> Query:
    """Parse a query string. Never raises — malformed input degrades, it does not explode."""
    terms: list[Term] = []
    unknown: list[str] = []

    for token in lex(source):
        raw = token.text
        if not raw:
            continue
        negated = len(raw) > 1 and raw[0] in "-!"
        body = raw[1:] if negated else raw
        head, sep, value = body.partition(":")

        if not sep:
            terms.append(Term(field=BARE_FIELD, values=(body,), negated=negated))
            continue

        name = _FIELD_ALIASES.get(head.strip().lower(), head.strip().lower())
        if name not in FIELDS:
            # A colonated *title* ("Andor: Season 2") is far more likely than a typo'd
            # field, so an unknown field degrades to literal text rather than vanishing.
            unknown.append(head)
            terms.append(Term(field=BARE_FIELD, values=(body,), negated=negated))
            continue

        values = tuple(p.strip() for p in value.split(",") if p.strip())
        if not values:
            continue  # `kind:` mid-typing constrains nothing

        if name == "kind" and len(values) == 1 and values[0].lower() in _KIND_TO_ORIGIN:
            terms.append(
                Term(
                    field=DescriptorKind.ORIGIN.value,
                    values=(_KIND_TO_ORIGIN[values[0].lower()],),
                    negated=negated,
                )
            )
            continue

        ranges: tuple[NumRange, ...] = ()
        if name in _NUMERIC_FIELDS:
            ranges = tuple(r for v in values if (r := _parse_range(v)) is not None)
            if not ranges:
                continue

        terms.append(Term(field=name, values=values, ranges=ranges, negated=negated))

    return Query(terms=tuple(terms), unknown_fields=tuple(dict.fromkeys(unknown)))


# --- matching -----------------------------------------------------------------------------
def _any_substr(needles: Sequence[str], haystacks: Iterable[str]) -> bool:
    hays = [h.lower() for h in haystacks]
    return any(any(n.lower() in h for h in hays) for n in needles)


def _any_exact(needles: Sequence[str], value: str) -> bool:
    return any(n.lower() == value.lower() for n in needles)


def _match_is(values: Sequence[str], row: TrackRow) -> bool:
    flags: dict[str, bool] = {
        "blocked": bool(row.blockers),
        "notes": row.has_notes,
        "dated": row.pivot_when is not None,
        "tba": row.pivot_when is None,
        "confirmed": row.pivot_confirmed,
        "speculative": row.pivot_when is not None and not row.pivot_confirmed,
        "predicted": any(t.predicted for t in row.what),
        "fresh": row.freshness == "fresh",
        "aging": row.freshness == "aging",
        "stale": row.freshness == "stale",
    }
    return any(
        (v in _IS_BUCKETS and row.bucket.value == v) or flags.get(v, False)
        for v in (v.lower() for v in values)
    )


def _match_num(ranges: Sequence[NumRange], values: Iterable[int]) -> bool:
    pool = list(values)
    return any(r.contains(n) for r in ranges for n in pool)


def _match_term(term: Term, row: TrackRow) -> bool:
    """Does one clause hold for this row (before negation)?"""
    values, field = term.values, term.field

    if field == BARE_FIELD:
        return _any_substr(values, (row.title, *row.aliases))
    if field == "kind":
        return _any_exact(values, row.kind.value) or any(
            (k := as_kind(v)) is not None and k is row.kind for v in values
        )
    if field == "state":
        return _any_exact(values, row.state.value)
    if field == "is":
        return _match_is(values, row)
    if field == "tag":
        return _any_substr(values, (t.name for t in row.what))
    if field in _DESCRIPTOR_FIELDS:
        want = _DESCRIPTOR_FIELDS[field]
        return _any_substr(values, (t.name for t in row.what if t.kind is want))
    if field == "person":
        return _any_substr(values, (c.name for c in row.credits))
    if field in _ROLE_FIELDS:
        role = _ROLE_FIELDS[field]
        return _any_substr(values, (c.name for c in row.credits if c.role is role))
    if field == "platform":
        return any(_platform_hit(_split_platform(v), row.platforms) for v in values)
    if field == "region":
        wanted = frozenset(v.strip().upper() for v in values)
        return any(wanted & frozenset(p.regions) for p in row.platforms)
    if field == "series":
        return _any_substr(values, row.series)
    if field == "year":
        return _match_num(term.ranges, row.years)
    if field == "season":
        return _match_num(term.ranges, () if row.season is None else (row.season,))
    if field == "part":
        return _match_num(term.ranges, () if row.part is None else (row.part,))
    return True  # pragma: no cover - unreachable while FIELDS and _match_term agree


@dataclass(frozen=True, slots=True)
class PlatformTerm:
    """A ``platform:`` value split into the service and the market it is scoped to."""

    name: str  # "" means any platform
    region: str | None = None  # ISO-2, upper; None means any market


def _split_platform(value: str) -> PlatformTerm:
    """``netflix@us`` -> ('netflix', 'US'); ``netflix`` -> ('netflix', None).

    The ``@`` form exists because ``platform:netflix region:us`` is a conjunction of two
    independent row predicates — "has a Netflix edge" AND "has a US edge" — which matches a
    row on Netflix in Germany and Paramount+ in the States. That is a false positive on the
    exact question the syntax is for.
    """
    name, sep, region = value.strip().partition("@")
    return PlatformTerm(name.strip().casefold(), region.strip().upper() if sep and region else None)


def _platform_hit(term: PlatformTerm, lines: Sequence[PlatformLine]) -> bool:
    """One platform term against a row's where-lines."""
    return any(
        (not term.name or term.name in p.name.casefold())
        # An unscoped line matches only an unscoped query: "we don't know where" cannot
        # honestly answer "is it live in the US".
        and (term.region is None or term.region in p.regions)
        for p in lines
    )


def matches(q: Query, row: TrackRow) -> bool:
    """All terms hold (negated terms must *not* hold). An empty query matches everything."""
    return all(_match_term(t, row) != t.negated for t in q.terms)


def filter_rows(q: Query, rows: Sequence[TrackRow]) -> list[TrackRow]:
    """Filter an already-built snapshot. Pure and in-memory — cheap enough per keystroke."""
    return [r for r in rows if matches(q, r)]


# --- completion ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Suggestion:
    """One completion. Splice with :func:`apply`.

    ``kind`` says which half of the grammar it completes: a ``field`` suggestion turns a
    bare word into ``field:``, a ``value`` suggestion fills in what follows the colon.
    The widget treats them differently — only a value completion is worth previewing,
    because only it narrows the result set rather than emptying it.
    """

    insert: str
    label: str
    detail: str
    start: int
    end: int
    kind: Literal["field", "value"] = "value"


def apply(source: str, suggestion: Suggestion) -> str:
    """``source`` with ``suggestion`` spliced over the token it was derived from."""
    return source[: suggestion.start] + suggestion.insert + source[suggestion.end :]


@dataclass(frozen=True, slots=True)
class VocabEntry:
    """One completable value, with how many works use it (drives ranking)."""

    value: str
    uses: int = 0
    descriptor_kind: DescriptorKind | None = None


@dataclass(frozen=True, slots=True)
class Vocabulary:
    """Completable values snapshotted from the graph. Built by ``views.build_vocabulary``."""

    descriptors: tuple[VocabEntry, ...] = ()
    people: tuple[VocabEntry, ...] = ()
    orgs: tuple[VocabEntry, ...] = ()
    platforms: tuple[VocabEntry, ...] = ()
    # the markets where-edges are actually scoped to, so `region:` completes against reality
    # rather than against a list of every ISO-2 in the world
    regions: tuple[VocabEntry, ...] = ()
    series: tuple[VocabEntry, ...] = ()
    titles: tuple[VocabEntry, ...] = ()
    # keyed by role, because `director:` completing against every person in the graph
    # offers names that provably cannot match — the query is role-scoped, so the
    # completion has to be too.
    credits: dict[CreditRole, tuple[VocabEntry, ...]] = dc_field(
        default_factory=dict[CreditRole, tuple[VocabEntry, ...]]
    )


def _static(values: Iterable[str], detail: str) -> list[tuple[VocabEntry, str]]:
    return [(VocabEntry(value=v), detail) for v in values]


def _dynamic(entries: Iterable[VocabEntry], detail: str) -> list[tuple[VocabEntry, str]]:
    return [(e, detail) for e in entries]


def _values_for(field: str, vocab: Vocabulary) -> list[tuple[VocabEntry, str]]:
    """Candidate ``(entry, detail)`` pairs for a field."""
    if field == "kind":
        return _static((k.value for k in MediaKind), "kind")
    if field == "state":
        return _static((s.value for s in ConsumptionState), "state")
    if field == "is":
        return [
            (VocabEntry(value=v), "bucket" if v in _IS_BUCKETS else "flag")
            for v in sorted(_IS_VALUES)
        ]
    if field == "tag":
        return [
            (e, e.descriptor_kind.value if e.descriptor_kind else "tag") for e in vocab.descriptors
        ]
    if field in _DESCRIPTOR_FIELDS:
        want = _DESCRIPTOR_FIELDS[field]
        return _dynamic((e for e in vocab.descriptors if e.descriptor_kind is want), want.value)
    if field == "platform":
        return _dynamic(vocab.platforms, "platform")
    if field == "region":
        return _dynamic(vocab.regions, "region")
    if field == "series":
        return _dynamic(vocab.series, "series")
    if field == "person":
        return _dynamic(vocab.people, "person")
    if field in _ROLE_FIELDS:
        role = _ROLE_FIELDS[field]
        return _dynamic(vocab.credits.get(role, ()), role.value)
    if field == BARE_FIELD:
        return _dynamic(vocab.titles, "title")
    return []


_WORDS = re.compile(r"[^0-9a-z]+")


def _word_prefixed(value: str, needle: str) -> bool:
    """Does ``value`` begin with ``needle``, at the start of the value or of one of its words?

    Prefix rather than substring, so that typing narrows monotonically: ``is:a`` offers
    *aging* and *available* but not *dated*, which the typed ``a`` has already ruled out
    as far as the reader is concerned. Word starts still count, because a name is looked
    up by whichever part of it comes to mind — ``cast:rit`` must find *Alan Ritchson*.
    """
    low = value.lower()
    return low.startswith(needle) or any(w.startswith(needle) for w in _WORDS.split(low) if w)


def _quote(value: str) -> str:
    return f"{QUOTE}{value}{QUOTE}" if any(c.isspace() or c == "," for c in value) else value


def canonical_field(name: str) -> str:
    """The field an alias names — ``dir`` -> ``director``. Idempotent on a real field."""
    lowered = name.strip().lower()
    return _FIELD_ALIASES.get(lowered, lowered)


def rank_values(
    field: str, vocab: Vocabulary, needle: str, *, limit: int = 8
) -> tuple[tuple[VocabEntry, str], ...]:
    """The completable ``(value, detail)`` pairs for one field, best match first.

    The ranking half of :func:`suggest`, split out because the query bar is not the only
    place that completes a value against the graph: a card's ``director`` field wants the
    same candidates in the same order, and only differs in how it splices the answer back.
    """
    pool = _values_for(canonical_field(field), vocab)
    hits = [(e, d) for e, d in pool if _word_prefixed(e.value, needle)]
    if not hits:  # nothing *starts* with it, so fall back to anywhere-in-the-value
        hits = [(e, d) for e, d in pool if needle in e.value.lower()]
    hits.sort(key=lambda ed: (not ed[0].value.lower().startswith(needle), -ed[0].uses, ed[0].value))
    return tuple(hits[:limit])


def suggest(
    source: str, cursor: int, vocab: Vocabulary, *, limit: int = 8
) -> tuple[Suggestion, ...]:
    """Completions for the token under the cursor — field names, then values for that field.

    Pure, so it is unit-testable without a terminal and reusable for shell completion.
    """
    start, end = active_span(source, cursor)
    token = source[start:end]
    negated = token[:1] in ("-", "!")
    prefix = token[:1] if negated else ""
    body = token[1:] if negated else token
    head, sep, value = body.partition(":")

    if not sep:
        needle = head.strip().strip(QUOTE).lower()
        names = sorted(f for f in FIELDS if f.startswith(needle) and f != BARE_FIELD)
        return tuple(
            Suggestion(
                insert=f"{prefix}{name}:",
                label=f"{name}:",
                detail="field",
                start=start,
                end=end,
                kind="field",
            )
            for name in names[:limit]
        )

    field = canonical_field(head)
    # complete only the segment under the caret in a comma list
    lead, _, segment = value.rpartition(",")
    needle = segment.strip().strip(QUOTE).lower()
    kept = f"{lead}," if lead else ""
    return tuple(
        Suggestion(
            insert=f"{prefix}{field}:{_quote(kept + entry.value)}",
            label=entry.value,
            detail=f"{detail} · {entry.uses}" if entry.uses else detail,
            start=start,
            end=end,
        )
        for entry, detail in rank_values(field, vocab, needle, limit=limit)
    )
