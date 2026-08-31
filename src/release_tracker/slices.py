"""Propose where a season was split, from air dates alone.

No canonical source models an intra-season slice. TMDB carries Money Heist "Part 1-5", Better
Call Saul S6 Part 1/2, Stranger Things S4 Vol 1/2, Ozark S4 Part 1/2 and Cobra Kai S6
Part 1/2/3 as plain seasons named ``"Season N"``. So a slice can never be *sourced* — only
authored, or read off the one signal that is sourced: the episodes stop for a while.

The threshold is relative to the season's own cadence rather than absolute, because the two
release shapes are opposites. A weekly drama's split is a 49-day break against a 7-day beat;
a binge season's is a 120-day break against a 0-day beat. No fixed number sees both.

This module proposes and shows its working. It never writes, and the caller is expected to
put a whole-season option beside every proposal — the false-positive class is real (a US
network winter hiatus looks exactly like a marketed split) and the cost of being wrong should
be one keystroke, not a wrong row.

Pure: no database, no network, no clock.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from typing import Final

__all__ = [
    "CADENCE_MULTIPLE",
    "MIN_EPISODES",
    "MIN_GAP_DAYS",
    "Episode",
    "SliceProposal",
    "SliceScan",
    "scan_slices",
]

# A gap must clear both bars to count: an absolute floor, so a daily-release show's ordinary
# week off is not a split, and a multiple of the season's own beat, so a weekly show's
# mid-season break is.
MIN_GAP_DAYS: Final[int] = 21
CADENCE_MULTIPLE: Final[float] = 3.0
# Below this there is no cadence worth taking a median of.
MIN_EPISODES: Final[int] = 4
# A sub-threshold gap is worth naming once it stands out against the season's own beat — that
# is how a real extra block gets missed. Stranger Things S5 drops its finale 6 days after
# volume 2 on an otherwise 0-day cadence: far under any bar, and still a third block.
NEAR_MISS_MULTIPLE: Final[float] = 2.0
NEAR_MISS_FLOOR: Final[int] = 2
# Past this a break is too long to be a scheduling hiatus, whatever month it crosses.
HIATUS_MAX_DAYS: Final[int] = 90


@dataclass(frozen=True, slots=True)
class Episode:
    """One dated episode. An undated one carries no signal, so callers drop it before scanning."""

    number: int
    air_date: date


@dataclass(frozen=True, slots=True)
class SliceProposal:
    """One release block within a season."""

    index: int  # 1-based
    first_episode: int
    last_episode: int
    starts: date
    gap_before_days: int | None  # None on the first block

    @property
    def episodes(self) -> int:
        return self.last_episode - self.first_episode + 1


@dataclass(frozen=True, slots=True)
class SliceScan:
    """What the air dates say about a season, and why.

    ``reasons`` follows the ``drafts.Draft.reasons`` idiom: the review surfaces print it
    verbatim, so a proposal nobody can account for never reaches the user.
    """

    proposals: tuple[SliceProposal, ...]
    cadence_days: int | None
    threshold_days: int | None
    reasons: tuple[str, ...] = ()

    @property
    def split(self) -> bool:
        """True when the season broke into more than one release block."""
        return len(self.proposals) > 1


def _empty(reason: str) -> SliceScan:
    return SliceScan(proposals=(), cadence_days=None, threshold_days=None, reasons=(reason,))


def scan_slices(
    episodes: Sequence[Episode],
    *,
    min_gap_days: int = MIN_GAP_DAYS,
    cadence_multiple: float = CADENCE_MULTIPLE,
    min_episodes: int = MIN_EPISODES,
) -> SliceScan:
    """Read a season's release blocks off its episode air dates.

    A single proposal means "we looked and there is no split" — which is a real answer worth
    showing, not an empty result. An empty ``proposals`` means there was nothing to look at.
    """
    dated = sorted(episodes, key=lambda e: e.number)
    if len(dated) < min_episodes:
        return _empty(f"only {len(dated)} dated episode(s) — too few to read a cadence")

    gaps = [(dated[i].air_date - dated[i - 1].air_date).days for i in range(1, len(dated))]
    cadence = int(statistics.median(gaps))
    threshold = max(min_gap_days, int(cadence * cadence_multiple))
    reasons = [f"cadence {cadence}d (median of {len(gaps)} gaps)"]

    cuts = [i for i, gap in enumerate(gaps, start=1) if gap > threshold]
    reasons.extend(
        f"cut before ep {dated[i].number} — {gaps[i - 1]}d gap clears {threshold}d" for i in cuts
    )

    # The near miss is the honest part. Stranger Things S5 really has three blocks, and the
    # 6-day Volume-2-to-Finale gap is below any sane bar — naming it turns a silent
    # under-count into something the reader can fix in one keystroke.
    if (near := _near_miss(dated, gaps, threshold, cuts, cadence)) is not None:
        reasons.append(near)
    if _looks_like_a_winter_break(dated, gaps, cuts, cadence):
        reasons.append(
            "weekly show broken over a Dec-Jan boundary — a network hiatus looks like this"
        )

    return SliceScan(
        proposals=_blocks(dated, gaps, cuts),
        cadence_days=cadence,
        threshold_days=threshold,
        reasons=tuple(reasons),
    )


def _blocks(
    dated: Sequence[Episode], gaps: Sequence[int], cuts: Sequence[int]
) -> tuple[SliceProposal, ...]:
    """Cut the episode run into blocks at the given indices."""
    bounds = [0, *cuts, len(dated)]
    return tuple(
        SliceProposal(
            index=n,
            first_episode=dated[lo].number,
            last_episode=dated[hi - 1].number,
            starts=dated[lo].air_date,
            gap_before_days=gaps[lo - 1] if lo else None,
        )
        for n, (lo, hi) in enumerate(pairwise(bounds), start=1)
    )


def _near_miss(
    dated: Sequence[Episode],
    gaps: Sequence[int],
    threshold: int,
    cuts: Sequence[int],
    cadence: int,
) -> str | None:
    """The largest gap that did *not* make the cut, when it was close enough to matter."""
    rest = [(i, g) for i, g in enumerate(gaps, start=1) if i not in set(cuts)]
    if not rest:
        return None
    i, gap = max(rest, key=lambda pair: pair[1])
    # Measured against the cadence, not the bar: on a binge season the cadence is 0, so a
    # six-day pause is enormous relatively and invisible absolutely.
    if gap <= max(NEAR_MISS_FLOOR, int(cadence * NEAR_MISS_MULTIPLE)):
        return None
    return f"nearest gap under the bar: {gap}d before ep {dated[i].number} (bar {threshold}d)"


def _looks_like_a_winter_break(
    dated: Sequence[Episode], gaps: Sequence[int], cuts: Sequence[int], cadence: int
) -> bool:
    """Does a cut have the shape of a US network winter hiatus rather than a marketed split?

    Weekly, across a calendar year end, and *short*. The length bound is what keeps the
    caveat off genuine long splits — Attack on Titan's Final Season pauses 287 days over a
    New Year and The Walking Dead's S11 pauses 133, and neither is a scheduling gap.
    """
    if not cadence or cadence > 7:
        return False
    return any(
        dated[i - 1].air_date.year != dated[i].air_date.year and gaps[i - 1] < HIATUS_MAX_DAYS
        for i in cuts
    )
