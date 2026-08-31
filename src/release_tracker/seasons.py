"""What a source says about a show's seasons: which exist, and whether more are coming.

Nothing here decides that a season will never exist — the tracker cannot know that. It reports
what TMDB *lists* and what TMDB *claims*, and phrases every answer as that claim. The
distinction matters because both halves of "the API says N seasons" can be wrong in opposite
directions:

* **The source lags.** Pluribus was renewed for a second season before the first aired, so
  ``/tv/225171/season/2`` is a 404 for a season that genuinely exists. Its status is
  "Returning Series", which is the whole reason this refuses to be firm about a running show.
* **The season is on another id.** TMDB sometimes folds a revival into the original (Futurama's
  eleven seasons, Twin Peaks' third) and sometimes does not — Beavis and Butt-Head ends at
  eight with the 2022 revival filed separately, Dexter at eight with New Blood separate. There
  the count is right about *that id* and the reader is asking about something else.

Measured rather than assumed: TMDB already carries a row for a confirmed-but-unaired season, in
two shapes — dated ahead (Yellowjackets S4, House of the Dragon S3) or an undated placeholder
with zero episodes (Severance S3, Wednesday S3). So ``number_of_seasons`` counts *announced*
seasons, and a row existing at all is the source saying one is coming.

Pure: no database, no network, no clock — ``today`` is a parameter.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Final

__all__ = [
    "MIN_SHARED_CAST",
    "TOP_CAST",
    "DidYouMean",
    "SeasonRef",
    "SeasonStanding",
    "SeasonVerdict",
    "ShowShape",
    "ShowStance",
    "Successor",
    "check_season",
    "pending_seasons",
    "rank_successors",
    "stance_of",
]

# TMDB's own status words. Both spellings of cancelled appear in the wild, and a show that was
# cancelled and one that ended by design are the same answer to "is there a season N+1" — the
# API conflates them anyway (Sense8 was cancelled and reads "Ended").
_FINISHED: Final[frozenset[str]] = frozenset({"Ended", "Canceled", "Cancelled"})
_RUNNING: Final[frozenset[str]] = frozenset(
    {"Returning Series", "In Production", "Planned", "Pilot"}
)


@dataclass(frozen=True, slots=True)
class SeasonRef:
    """One season of a show as the source lists it."""

    number: int
    name: str
    air_date: date | None
    episodes: int

    @property
    def specials(self) -> bool:
        """Season 0 — a specials bucket, not a season anyone means by "season"."""
        return self.number == 0


class ShowStance(enum.StrEnum):
    """What the source claims about whether more is coming."""

    FINISHED = "finished"  # it says the show is over
    CONFIRMED_NEXT = "confirmed_next"  # running, and a row exists for the next one
    UNCERTAIN = "uncertain"  # running, nothing listed — renewed, or merely quiet
    UNKNOWN = "unknown"  # no status, or a word we do not recognise


class SeasonStanding(enum.StrEnum):
    """Where a requested season sits against what the source lists."""

    LISTED = "listed"
    BEYOND_LISTED = "beyond_listed"  # past the end, show still running
    BEYOND_END = "beyond_end"  # past the end, show marked over


@dataclass(frozen=True, slots=True)
class ShowShape:
    """The show detail's answer to "what seasons are there".

    ``status`` is the source's own word, kept verbatim so it can be quoted back rather than
    paraphrased into something the source did not say.
    """

    name: str | None  # the source's own title, so a quote is its words and not the query's
    status: str | None
    seasons: tuple[SeasonRef, ...]
    listed: int  # `number_of_seasons` as reported — counts announced ones

    @property
    def numbered(self) -> tuple[SeasonRef, ...]:
        """The seasons anyone means by "season"; specials are reached via an explicit 0."""
        return tuple(s for s in self.seasons if not s.specials)

    @property
    def highest(self) -> int:
        """The largest numbered season listed, or 0 when none are."""
        return max((s.number for s in self.numbered), default=0)

    def row(self, season: int) -> SeasonRef | None:
        return next((s for s in self.seasons if s.number == season), None)


@dataclass(frozen=True, slots=True)
class SeasonVerdict:
    """Where a requested season stands, and why.

    ``reasons`` is printed verbatim by every surface — the picker's status line, the report's
    notes, the review form's provenance. Nothing downstream parses it.
    """

    season: int
    standing: SeasonStanding
    stance: ShowStance
    listed: SeasonRef | None
    highest: int
    reasons: tuple[str, ...] = ()

    @property
    def out_of_range(self) -> bool:
        return self.standing is not SeasonStanding.LISTED

    @property
    def firm(self) -> bool:
        """True only when the source itself says the show is over.

        Everything else is soft, and soft is the default on purpose: a running show with no
        row for the next season is the shape of a renewal the source has not caught up with.
        """
        return self.standing is SeasonStanding.BEYOND_END


def pending_seasons(seasons: Sequence[SeasonRef], today: date) -> tuple[SeasonRef, ...]:
    """Listed seasons that have not started airing — dated ahead, or announced with no date.

    Both shapes mean the same thing and both occur: Yellowjackets S4 carries 2026-11-22 while
    Severance S3 carries no date and zero episodes. A row existing at all is the source saying
    the season is coming.
    """

    def unaired(s: SeasonRef) -> bool:
        if s.air_date is None:
            return not s.episodes  # an empty placeholder: announced, nothing scheduled
        return s.air_date > today

    return tuple(s for s in seasons if not s.specials and unaired(s))


def stance_of(shape: ShowShape, today: date) -> ShowStance:
    """The three states, from two fields and no classifier.

    ``in_production`` is deliberately unread: it tracked ``status`` exactly on every show
    measured, and a second field carrying no new information is a second thing to keep true.
    """
    status = (shape.status or "").strip()
    if status in _FINISHED:
        return ShowStance.FINISHED
    if status not in _RUNNING:
        return ShowStance.UNKNOWN  # a word we do not know: fail open, stay soft
    return (
        ShowStance.CONFIRMED_NEXT if pending_seasons(shape.seasons, today) else ShowStance.UNCERTAIN
    )


def _standing(shape: ShowShape, season: int, stance: ShowStance) -> SeasonStanding:
    if shape.row(season) is not None:
        return SeasonStanding.LISTED
    return (
        SeasonStanding.BEYOND_END if stance is ShowStance.FINISHED else SeasonStanding.BEYOND_LISTED
    )


def check_season(
    shape: ShowShape, season: int, today: date, *, show: str | None = None
) -> SeasonVerdict:
    """Where season N stands against what the source lists, phrased as the source's claim.

    Firm only when the source calls the show over — and firm still never blocks a capture. The
    reader may know something the source does not, which is exactly the Pluribus case in the
    other direction.
    """
    # The source's own title unless the caller insists: quoting the query back ("pluribus")
    # reads as our claim, where TMDB's own "Pluribus" reads as theirs.
    show = show or shape.name or "this show"
    stance = stance_of(shape, today)
    standing = _standing(shape, season, stance)
    row = shape.row(season)
    seasons = f"{shape.highest} season{'' if shape.highest == 1 else 's'}"
    reasons: list[str] = []

    match standing:
        case SeasonStanding.BEYOND_END:
            reasons.append(
                f"TMDB lists {seasons} of “{show}” and marks it “{shape.status}”"
                f" — it carries no season {season}"
            )
        case SeasonStanding.BEYOND_LISTED:
            said = f"marks it “{shape.status}”" if shape.status else "states no status"
            reasons.append(
                f"TMDB lists {seasons} of “{show}” and {said} — season {season} is not listed yet"
            )
            for coming in pending_seasons(shape.seasons, today):
                when = (
                    f"announced for {coming.air_date.isoformat()}"
                    if coming.air_date is not None
                    else "announced, no date yet"
                )
                reasons.append(f"season {coming.number} is {when}")
        case SeasonStanding.LISTED:
            if row is not None and row.air_date is None:
                reasons.append(f"TMDB lists season {season} but has no air date for it yet")

    return SeasonVerdict(
        season=season,
        standing=standing,
        stance=stance,
        listed=row,
        highest=shape.highest,
        reasons=tuple(reasons),
    )


# How deep to compare casts. Measured at 25: the depth where Dexter's four shared names with
# New Blood still separate cleanly from Dexter's Laboratory's zero.
TOP_CAST: Final[int] = 25
# One shared name is enough to offer. Doctor Who 2005 -> 2024 shares exactly one, because the
# entire cast turns over at a regeneration, and it is unmistakably a continuation.
MIN_SHARED_CAST: Final[int] = 1


@dataclass(frozen=True, slots=True)
class Successor:
    """A show that might carry the season the base one does not."""

    title: str
    key: str  # the source id, so accepting one can pin it
    year: int | None
    seasons: int
    shared_cast: int
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DidYouMean:
    """What fell short, and what else might carry the season that was asked for.

    A list a human picks from, never an answer. Computing the mapping was tried three ways and
    each failed: a word-aligned prefix filter drops the base show itself ("Marvel's Daredevil"
    is not prefixed by "daredevil"); token-sharing admits strangers ("Dexter's Laboratory"
    passes a prefix test on "dexter" and contributes four seasons to a cumulative count); and
    same-named eras (Doctor Who 1963/2005/2024) are genuinely ambiguous. Counting cumulatively
    answers "daredevil season 4" with "Marvel's Daredevil season 1".
    """

    verdict: SeasonVerdict
    offer: tuple[Successor, ...]
    reasons: tuple[str, ...] = ()

    @property
    def after(self) -> int:
        """The offset a ``continues`` edge would record: seasons the base show ran."""
        return self.verdict.highest

    def native(self, successor: Successor) -> int | None:
        """The asked-for season renumbered onto a successor, or None if it lands below one."""
        landed = self.verdict.season - self.after
        return landed if landed >= 1 and landed <= max(successor.seasons, landed) else None


def rank_successors(
    base_title: str,
    candidates: Sequence[Successor],
    *,
    min_shared: int = MIN_SHARED_CAST,
) -> tuple[tuple[Successor, ...], tuple[str, ...]]:
    """Order same-named shows by how much of the base show's cast they carry.

    Cast overlap is the one signal measured to separate a continuation from a stranger that
    merely shares a name: against Dexter, New Blood shares four names and Resurrection three,
    while Dexter's Laboratory shares none. The cheaper signals were tried and are worse —
    ranking by debut date, shared title words and vote count puts a zero-vote "Dexter Procter"
    above New Blood.

    A candidate sharing nobody is dropped and *said*, not silently omitted: the reader asked a
    question and deserves to know the pool was narrowed.
    """
    kept: list[Successor] = []
    dropped: list[Successor] = []
    for cand in candidates:
        (kept if cand.shared_cast >= min_shared else dropped).append(cand)

    ranked = tuple(
        replace(c, reasons=(*c.reasons, *_successor_reasons(c, base_title)))
        for c in sorted(kept, key=lambda c: (-c.shared_cast, -(c.year or 0)))
    )
    reasons: list[str] = []
    if dropped:
        names = ", ".join(f"“{c.title}”" for c in dropped)
        reasons.append(f"{names} share no cast with “{base_title}” — not offered")
    return ranked, tuple(reasons)


def _successor_reasons(cand: Successor, base_title: str) -> tuple[str, ...]:
    shared = (
        f"{cand.shared_cast} of the top {TOP_CAST} cast also appear in “{base_title}”"
        if cand.shared_cast > 1
        else f"one shared name with “{base_title}”"
    )
    if cand.shared_cast > 1:
        return (shared,)
    # A whole cast really does turn over — Doctor Who recasts the lead every era — so one name
    # is offered rather than dismissed, and flagged rather than trusted.
    return (shared, "a single overlap can be a coincidence — worth checking this one")
