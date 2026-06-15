"""Speculative-date reference data + estimators.

The headline guess this project makes is the **digital** release date for a movie
that only has a theatrical date so far. The theatrical->digital ("PVOD") window
compressed hard post-2020 and varies per title, but a *distributor's* typical
behaviour is a much better prior than a blind global guess — so we keep a small
keyed table of windows and fall back to a default when the studio is unknown.

The same idea turns a coarse game date (a quarter / a year) into a single precise
point plus a margin of error: deliberately precise, knowingly approximate.

Everything here is pure (no I/O), so it is trivially testable and reusable.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

from release_tracker.models import DatePrecision

# Theatrical -> digital/PVOD window in days, by distributor. Matched by
# case-insensitive substring against the production-company name, longest key
# first (so "warner bros" beats a hypothetical "warner"). Rough on purpose.
STUDIO_DIGITAL_DELTA_DAYS: Final[dict[str, int]] = {
    "netflix": 0,  # day-and-date streaming
    "apple original": 30,
    "amazon mgm": 30,
    "amazon studios": 30,
    "universal": 31,  # post-2021 PVOD deal, ~17-31d
    "focus features": 31,
    "blumhouse": 31,  # distributed by Universal
    "monkeypaw": 31,
    "warner bros": 45,
    "new line": 45,
    "dc studios": 45,  # Warner-distributed
    "dc films": 45,
    "legendary": 45,
    "paramount": 45,
    "skydance": 45,
    "sony pictures": 45,
    "columbia": 45,
    "tristar": 45,
    "screen gems": 45,
    "lionsgate": 45,
    "metro-goldwyn": 45,
    "united artists": 45,
    "a24": 70,
    "neon": 70,
    "searchlight": 70,
    "20th century": 60,
    "walt disney": 90,
    "marvel studios": 90,
    "pixar": 90,
    "lucasfilm": 90,
}

# When we know nothing about the distributor: an industry-ish median window.
DEFAULT_DIGITAL_DELTA_DAYS: Final[int] = 75


# Distributor -> the streaming service a film typically lands on (the studio's
# in-house service or its current pay-1-window partner). This is the *predicted*
# eventual home, usable before a film is streaming at all — when JustWatch/TMDB
# watch-providers is still empty. Matched by substring, longest key first.
STUDIO_STREAMING: Final[dict[str, str]] = {
    "walt disney": "Disney+",
    "marvel studios": "Disney+",
    "pixar": "Disney+",
    "lucasfilm": "Disney+",
    "20th century": "Disney+/Hulu",
    "searchlight": "Hulu",
    "warner bros": "HBO Max",
    "new line": "HBO Max",
    "dc studios": "HBO Max",
    "dc films": "HBO Max",
    "universal": "Peacock",
    "focus features": "Peacock",
    "blumhouse": "Peacock",
    "dreamworks animation": "Peacock",
    "paramount": "Paramount+",
    "skydance": "Paramount+",
    "nickelodeon": "Paramount+",
    "sony": "Netflix",  # Sony's pay-1 output deal runs through Netflix
    "columbia": "Netflix",
    "tristar": "Netflix",
    "screen gems": "Netflix",
    "lionsgate": "Starz",
    "a24": "Max",  # output varies; A24 has run Max/Showtime deals
    "neon": "Hulu",
    "netflix": "Netflix",
    "apple original": "Apple TV+",
    "amazon mgm": "Prime Video",
    "amazon studios": "Prime Video",
    "metro-goldwyn": "Prime Video",  # MGM, now under Amazon
}


@dataclass(slots=True, frozen=True)
class Estimate:
    """A computed precise date, its uncertainty and how it was derived."""

    when: date
    margin_days: int
    confidence: float  # 0..1
    basis: str


def match_studio(names: Iterable[str]) -> str | None:
    """Pick the distributor to estimate from: first name with a known window, else first.

    TMDB's ``production_companies`` lists the lead production company first, which
    is often *not* the distributor (e.g. Batman II lists "DC Studios", not Warner).
    Scanning the whole list for any name we have a window for fixes most misses.
    """
    listed = [n for n in names if n]
    for name in listed:
        if digital_delta_days(name)[1] is not None:
            return name
    return listed[0] if listed else None


def studio_platform(studio: str | None) -> str | None:
    """The streaming service this distributor's films typically land on, if known."""
    if studio:
        norm = studio.lower()
        for key in sorted(STUDIO_STREAMING, key=len, reverse=True):
            if key in norm:
                return STUDIO_STREAMING[key]
    return None


def predicted_platform(studios: Iterable[str]) -> str | None:
    """First known streaming home across all listed companies (not just the lead)."""
    return next((platform for s in studios if (platform := studio_platform(s))), None)


def digital_delta_days(studio: str | None) -> tuple[int, str | None]:
    """Return (window_days, matched_studio_key) — default window if no match."""
    if studio:
        norm = studio.lower()
        for key in sorted(STUDIO_DIGITAL_DELTA_DAYS, key=len, reverse=True):
            if key in norm:
                return STUDIO_DIGITAL_DELTA_DAYS[key], key
    return DEFAULT_DIGITAL_DELTA_DAYS, None


def estimate_digital(
    theatrical: date, studio: str | None, *, theatrical_confirmed: bool = True
) -> Estimate:
    """Predict a precise digital date as theatrical + the distributor's window."""
    delta, matched = digital_delta_days(studio)
    when = theatrical + timedelta(days=delta)
    if matched is not None:
        confidence, margin = 0.55, 21
        basis = f"theatrical + {studio} window (~{delta}d)"
    else:
        confidence, margin = 0.40, 35
        basis = f"theatrical + default window (~{delta}d)"
    if not theatrical_confirmed:
        # the theatrical date we built on was itself a guess
        confidence *= 0.5
        margin += 40
        basis = f"guessed {basis}"
    return Estimate(when=when, margin_days=margin, confidence=round(confidence, 2), basis=basis)


def precise_from_coarse(when: date, precision: DatePrecision) -> Estimate:
    """Collapse a coarse date (start of month/quarter/year) to a precise midpoint."""
    match precision:
        case DatePrecision.EXACT:
            return Estimate(when, 0, 0.85, "exact date")
        case DatePrecision.MONTH:
            return Estimate(when + timedelta(days=14), 14, 0.60, "mid-month of stated month")
        case DatePrecision.QUARTER:
            return Estimate(when + timedelta(days=45), 45, 0.45, "mid-quarter of stated quarter")
        case DatePrecision.YEAR:
            return Estimate(when + timedelta(days=181), 181, 0.30, "mid-year of stated year")
        case _:
            return Estimate(when, 90, 0.20, "no usable precision")
