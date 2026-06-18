"""Studio/publisher release-timing trends — refine a coarse date toward a pattern.

When a source only gives us a *year* (or a quarter) for a game, the naive guess is
the midpoint of that period (see :func:`release_tracker.deltas.precise_from_coarse`).
But a publisher that has shipped every prior title in, say, late October is a much
better prior than "mid-year". This module mines a studio's historical release
*months* and, **only when the pattern is statistically clear**, biases the coarse
estimate toward it — staged by how tight the pattern is:

* months cluster tightly (high circular concentration) -> narrow to that month;
* months only cluster to a *season* -> narrow to that season (wider margin);
* no clear pattern -> leave the naive midpoint untouched.

Crucially we only ever narrow *within* the coarse bound we already hold: a known
quarter always beats this prior, and if the studio's typical month falls outside
the stated period we trust the harder data and do not override it.

Everything here is pure (no I/O) — the mining/caching lives in the sources + cache.
"""

from __future__ import annotations

import calendar
import enum
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from math import atan2, cos, hypot, pi, sin
from typing import Final

from release_tracker.deltas import Estimate
from release_tracker.models import DatePrecision, MediaKind

# --- tuning knobs ---------------------------------------------------------
# Need at least this many prior releases before a pattern is trustworthy.
MIN_SAMPLES: Final[int] = 4
# Circular resultant length (0..1) above which months are "the same month-ish".
# 1.0 == all identical; ~0.97 == split across two adjacent months; a full season
# spread (3 consecutive months, even) sits ~0.91, so this deliberately excludes it.
TAU_MONTH: Final[float] = 0.93
# Fraction of releases that must fall in one season to call it a seasonal studio.
TAU_SEASON: Final[float] = 0.6


class Season(enum.StrEnum):
    """Meteorological seasons (Northern hemisphere) for the coarser stage."""

    WINTER = "winter"
    SPRING = "spring"
    SUMMER = "summer"
    FALL = "fall"


_SEASON_MONTHS: Final[dict[Season, tuple[int, int, int]]] = {
    Season.WINTER: (12, 1, 2),
    Season.SPRING: (3, 4, 5),
    Season.SUMMER: (6, 7, 8),
    Season.FALL: (9, 10, 11),
}
_MONTH_TO_SEASON: Final[dict[int, Season]] = {
    m: s for s, months in _SEASON_MONTHS.items() for m in months
}


@dataclass(slots=True, frozen=True)
class StudioTrend:
    """A studio's mined release-timing pattern (per media kind)."""

    studio_key: str  # stable cache key, e.g. "igdb:1234"
    studio_name: str
    kind: MediaKind
    n: int  # number of historical releases the stats are built from
    months: tuple[int, ...]  # raw 1..12 samples, sorted (kept for transparency)
    month_mode: int | None  # circular-mean month (1..12), None only when n == 0
    month_concentration: float  # circular resultant length R in [0, 1]
    season_mode: Season | None
    season_concentration: float  # modal-season share in [0, 1]


def compute_trend(
    studio_key: str, studio_name: str, kind: MediaKind, months: tuple[int, ...]
) -> StudioTrend:
    """Reduce a bag of release months to a :class:`StudioTrend` (circular stats).

    Months are circular (Dec is adjacent to Jan), so concentration is the resultant
    vector length of the month angles, and the "mode" is the circular mean — both
    handle a December/January studio correctly where a plain histogram would not.
    """
    ms = tuple(sorted(m for m in months if 1 <= m <= 12))
    n = len(ms)
    if n == 0:
        return StudioTrend(studio_key, studio_name, kind, 0, (), None, 0.0, None, 0.0)

    angles = [2 * pi * (m - 1) / 12 for m in ms]
    cos_sum = sum(cos(a) for a in angles)
    sin_sum = sum(sin(a) for a in angles)
    resultant = hypot(cos_sum, sin_sum) / n
    mean_month = round(atan2(sin_sum, cos_sum) / (2 * pi) * 12) % 12 + 1

    season_counts = Counter(_MONTH_TO_SEASON[m] for m in ms)
    season_mode, season_count = season_counts.most_common(1)[0]

    return StudioTrend(
        studio_key=studio_key,
        studio_name=studio_name,
        kind=kind,
        n=n,
        months=ms,
        month_mode=mean_month,
        month_concentration=round(resultant, 3),
        season_mode=season_mode,
        season_concentration=round(season_count / n, 3),
    )


def narrow_coarse(when: date, precision: DatePrecision, trend: StudioTrend) -> Estimate | None:
    """Bias a coarse date toward the studio's pattern, or ``None`` to keep the naive guess.

    ``when`` is the start of the coarse period (year/quarter). Returns a refined
    precise :class:`Estimate` only when the pattern is clear *and* the studio's
    typical month falls inside the stated period; otherwise ``None`` (the caller
    keeps :func:`release_tracker.deltas.precise_from_coarse`).
    """
    if trend.n < MIN_SAMPLES or trend.month_mode is None:
        return None
    bound = _coarse_bound(when, precision)
    if bound is None:  # already month/exact precision — nothing to narrow
        return None
    lo, hi = bound
    target = date(lo.year, trend.month_mode, 15)
    if not (lo <= target <= hi):
        # the studio's habitual month contradicts the stated period — trust the
        # harder data rather than dragging the estimate out of its own bound.
        return None

    month_name = calendar.month_name[trend.month_mode]
    if trend.month_concentration >= TAU_MONTH:
        return Estimate(
            when=target,
            margin_days=14,
            confidence=0.55,
            basis=(
                f"{trend.studio_name} almost always releases in {month_name} "
                f"(R={trend.month_concentration:.2f}, n={trend.n})"
            ),
        )
    if trend.season_mode is not None and trend.season_concentration >= TAU_SEASON:
        return Estimate(
            when=target,
            margin_days=45,
            confidence=0.45,
            basis=(
                f"{trend.studio_name} usually releases in {trend.season_mode.value} "
                f"(~{month_name}, {round(trend.season_concentration * 100)}% of {trend.n})"
            ),
        )
    return None


def _coarse_bound(when: date, precision: DatePrecision) -> tuple[date, date] | None:
    """The [first, last] day of the coarse period, or ``None`` if it's already tight."""
    match precision:
        case DatePrecision.YEAR:
            return date(when.year, 1, 1), date(when.year, 12, 31)
        case DatePrecision.QUARTER:
            start_month = ((when.month - 1) // 3) * 3 + 1
            lo = date(when.year, start_month, 1)
            hi = (
                date(when.year, 12, 31)
                if start_month + 3 > 12
                else date(when.year, start_month + 3, 1) - timedelta(days=1)
            )
            return lo, hi
        case _:
            return None
