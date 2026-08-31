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
from dataclasses import dataclass
from datetime import date
from typing import Final

__all__ = [
    "SeasonRef",
    "SeasonStanding",
    "SeasonVerdict",
    "ShowShape",
    "ShowStance",
    "check_season",
    "pending_seasons",
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
