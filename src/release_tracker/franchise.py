"""Where a film sits in its franchise, and whether the one being asked for is listed.

The film sibling of :mod:`release_tracker.seasons`, and the differences are what make it a
sibling rather than a parameter:

* **The ordinal is ours, not the source's.** A season carries ``season_number``; a
  collection carries an unordered ``parts[]`` with no index at all. Position after sorting
  by release date is a proxy, and every reason line says so.
* **Nothing here is ever firm.** A collection has no status field, and a franchise is never
  formally cancelled — it just stops. So "entry 5 is not listed" is the strongest available
  claim, which is honest: Shrek went fifteen years between its fourth and fifth entries.

**The spinoff is what makes the ordinal work.** Raw position is wrong whenever a collection
mixes in a side film: "Fast & Furious Presents: Hobbs & Shaw" sits at position 9, pushing F9
to 10 and Fast X to 11. TMDB's own ``spin off`` keyword separates them — measured at 11 of
13 hand-labelled spinoffs found and 0 of 11 mainline entries wrongly flagged — and dropping
the flagged ones makes position match the marketed number across Fast & Furious, Ice Age,
Shrek, Toy Story, John Wick, Dune and Harry Potter.

The keyword is crowd-sourced, so it can be missing (Venom carries none). It only ever
*removes* an entry from the count, never adds one, and the reason line names what it removed.

Pure: no database, no network, no clock — ``today`` is a parameter.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from typing import Final

from release_tracker.models import Stance

__all__ = [
    "UNBACKED",
    "Entry",
    "EntryStanding",
    "EntryVerdict",
    "FranchiseShape",
    "Placement",
    "check_entry",
    "movie_stance",
    "movie_status_notes",
    "ordinal_of",
    "place",
    "shape_reason",
]


# TMDB's movie `status` ladder. There is no cancelled film here in practice: TMDB drops the
# record rather than marking it, so Batgirl, Scoob! Holiday Haunt, Superman: Flyby and
# Justice League: Mortal have no entry at all and `Canceled` did not appear once in 120
# sampled films. What the ladder actually carries is how *real* a production is, which is a
# question TV has no analogue for — a series is renewed or it is not.
#
# `Canceled` is mapped anyway. It is documented, and an unmapped word would fail open to
# UNKNOWN, which is the right default for a word we have never seen but the wrong one for
# a word we know the meaning of.
_MOVIE_STANCE: Final[dict[str, Stance]] = {
    "Released": Stance.RELEASED,
    "Post Production": Stance.COMING,
    "In Production": Stance.COMING,
    "Planned": Stance.COMING,
    "Rumored": Stance.UNCERTAIN,
    "Canceled": Stance.SHELVED,
    "Cancelled": Stance.SHELVED,
}

# Only the words that change what the reader should believe. "Post Production" beside a date
# adds nothing the date has not already said; "Rumored" beside one says the date is a
# placeholder nobody has committed to.
_MOVIE_STATUS_NOTE: Final[dict[str, str]] = {
    "Rumored": "TMDB marks this “Rumored” — no official announcement backs it",
    "Canceled": "TMDB marks this “Canceled”",
    "Cancelled": "TMDB marks this “Cancelled”",
}


# The stances that mean nobody official has committed to the film. Its `release_date` in that
# state is a placeholder — Gladiator III reads "Rumored" with an empty one — so a to-the-day
# theatrical guess off it would invent a schedule out of a maybe.
UNBACKED: Final[frozenset[Stance]] = frozenset({Stance.UNCERTAIN, Stance.SHELVED})


def movie_stance(status: str | None) -> Stance | None:
    """What TMDB's status word says about whether a film is coming.

    None when TMDB said nothing; ``UNKNOWN`` when it said a word we do not recognise, which
    is the fail-open the TV path uses for the same reason — an unrecognised word is not
    evidence, and treating it as one would shelve a film on a vocabulary change.
    """
    if not (status or "").strip():
        return None
    return _MOVIE_STANCE.get(status or "", Stance.UNKNOWN)


def movie_status_notes(status: str | None) -> tuple[str, ...]:
    """TMDB's own word, quoted back, where it is worth saying at all."""
    said = _MOVIE_STATUS_NOTE.get((status or "").strip())
    return (said,) if said else ()


class EntryStanding(enum.StrEnum):
    """Where a requested entry sits against what the collection lists.

    Two values where a season has three. ``BEYOND_END`` has no film analogue: it means "the
    source says this is over", and no collection carries a status to say it with.
    """

    LISTED = "listed"
    BEYOND_LISTED = "beyond_listed"


@dataclass(frozen=True, slots=True)
class Entry:
    """One film as the collection lists it."""

    key: str  # the source id, so an accepted entry can be pinned
    title: str
    released: date | None
    status: str | None  # TMDB's own word, kept verbatim so it can be quoted
    spinoff: bool  # carries TMDB's `spin off` keyword

    @property
    def stance(self) -> Stance | None:
        return movie_stance(self.status)


@dataclass(frozen=True, slots=True)
class FranchiseShape:
    """A collection's answer to "what films are in this".

    ``entries`` arrives in whatever order TMDB returned — ``parts[]`` is genuinely
    unsorted, with Fast & Furious coming back 2003, 2006, 2001, 2011 — so every consumer
    goes through :attr:`ordered` rather than indexing it.
    """

    name: str | None
    entries: tuple[Entry, ...]

    @property
    def ordered(self) -> tuple[Entry, ...]:
        """Every entry in release order. An undated one sorts last: it is announced but
        unscheduled, which is where it belongs in a sequence."""

        def when(e: Entry) -> tuple[bool, date]:
            return (e.released is None, e.released or date.max)

        return tuple(sorted(self.entries, key=when))

    @property
    def mainline(self) -> tuple[Entry, ...]:
        """Release order with spinoffs dropped — the sequence the marketed numbers count."""
        return tuple(e for e in self.ordered if not e.spinoff)

    @property
    def highest(self) -> int:
        return len(self.mainline)

    def at(self, entry: int) -> Entry | None:
        """The 1-based nth mainline entry, or None past the end."""
        return self.mainline[entry - 1] if 1 <= entry <= self.highest else None


@dataclass(frozen=True, slots=True)
class EntryVerdict:
    """Where a requested entry stands, and why.

    ``reasons`` is printed verbatim by every surface, as ``SeasonVerdict.reasons`` is.
    Nothing downstream parses it.
    """

    entry: int
    standing: EntryStanding
    stance: Stance | None
    listed: Entry | None
    highest: int
    reasons: tuple[str, ...] = ()

    @property
    def out_of_range(self) -> bool:
        return self.standing is not EntryStanding.LISTED

    @property
    def firm(self) -> bool:
        """Always False, and not by oversight.

        A franchise has no status to be firm with. It is never formally cancelled — it stops,
        and sometimes restarts fifteen years later.
        """
        return False


def ordinal_of(shape: FranchiseShape, key: str) -> int | None:
    """Which mainline entry a film is, or None if it is a spinoff or not in the collection.

    Derived from release order, never asserted by the source. A spinoff deliberately has no
    ordinal rather than a shared one: Hobbs & Shaw is not the ninth Fast & Furious film, and
    numbering it would be the exact error this module exists to avoid.
    """
    return next((n for n, e in enumerate(shape.mainline, start=1) if e.key == key), None)


def check_entry(
    shape: FranchiseShape, entry: int, today: date, *, name: str | None = None
) -> EntryVerdict:
    """Where entry N stands against what the collection lists, phrased as our own inference.

    Never blocks anything, and never firm. The reader may know about a film TMDB has not
    recorded — which is the common case, since TMDB drops a production that dies rather than
    marking it, and only adds one once it is real.
    """
    name = name or shape.name or "this franchise"
    listed = shape.at(entry)
    standing = EntryStanding.LISTED if listed is not None else EntryStanding.BEYOND_LISTED
    count = f"{shape.highest} film{'' if shape.highest == 1 else 's'}"
    reasons: list[str] = []

    if listed is None:
        reasons.append(
            f"TMDB lists {count} in “{name}” — counting by release order, there is no {entry}th"
        )
        reasons.extend(
            f"“{coming.title}” is listed but not out yet{_when(coming)}"
            for coming in _unreleased(shape, today)
        )
    elif listed.stance is Stance.UNCERTAIN:
        reasons.append(
            f"“{listed.title}” is entry {entry} by release order, but {_unbacked(listed)}"
        )

    reasons.extend(_dropped_note(shape))

    return EntryVerdict(
        entry=entry,
        standing=standing,
        stance=listed.stance if listed is not None else None,
        listed=listed,
        highest=shape.highest,
        reasons=tuple(reasons),
    )


def _unreleased(shape: FranchiseShape, today: date) -> tuple[Entry, ...]:
    """Mainline entries the collection lists but that have not come out.

    Unlike a season, there is no undated-placeholder shape to look for: every future entry
    measured carried a real date (Dune: Part Three, Avatar 4 and 5, Fast Forever in 2028).
    """
    return tuple(e for e in shape.mainline if e.released is None or e.released > today)


def _when(entry: Entry) -> str:
    return f" ({entry.released.isoformat()})" if entry.released is not None else ""


def _dropped_note(shape: FranchiseShape, *, besides: str | None = None) -> tuple[str, ...]:
    """What the count left out, named. Empty when it left nothing out.

    Every surface prints the spinoff-filtered number, so the filter has to be auditable:
    the keyword is crowd-sourced, and this line is how a reader catches it being wrong.
    ``besides`` drops the film being asked about, which says so in its own line already.
    """
    dropped = tuple(e for e in shape.ordered if e.spinoff and e.key != besides)
    if not dropped:
        return ()
    named = ", ".join(f"“{e.title}”" for e in dropped)
    return (f"{named} not counted — TMDB marks it a spin-off",)


def _unbacked(entry: Entry) -> str:
    return f"TMDB marks it “{entry.status}” — no official announcement backs it"


def shape_reason(shape: FranchiseShape) -> str:
    """The standing attribution: our number, from release order, not the source's."""
    return (
        f"franchise position is ours, counted by release date across "
        f"{shape.highest} of {len(shape.entries)} entries — TMDB numbers none of them"
    )


@dataclass(frozen=True, slots=True)
class Placement:
    """Where one film sits in the collection it belongs to — the inverse of a season ask.

    ``rdt rd "fast x"`` asks "what number is this", where ``--season N`` asks "does N
    exist". Same shape underneath, opposite direction, so this carries the number rather
    than a standing.

    ``entry`` is None for a film that has no number rather than an unknown one: a spin-off
    is deliberately outside the count.
    """

    name: str | None
    entry: int | None
    highest: int
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "entry": self.entry,
            "highest": self.highest,
            "reasons": list(self.reasons),
        }


def place(shape: FranchiseShape, key: str, *, name: str | None = None) -> Placement:
    """Number a film within its collection, saying every time what the number rests on.

    The attribution is not decoration. TMDB numbers none of these entries, so the number is
    an inference off release order that a mislabelled spin-off can move — which is exactly
    what it did to F9 before the keyword filter went in.
    """
    entry = ordinal_of(shape, key)
    reasons: list[str] = [shape_reason(shape)]

    if entry is None and any(e.key == key and e.spinoff for e in shape.entries):
        reasons.append("this one is not numbered — TMDB marks it a spin-off of the franchise")
    elif entry is None:
        reasons.append("this one is not among the films the collection lists")

    reasons.extend(_dropped_note(shape, besides=key))

    return Placement(
        name=name or shape.name,
        entry=entry,
        highest=shape.highest,
        reasons=tuple(reasons),
    )
