"""EDTF (ISO 8601-2 / Library of Congress Extended Date-Time Format) at the boundary.

A thin, two-way bridge between the internal date model — a real ``date`` plus a
:class:`DatePrecision` and a :class:`Certainty` — and the EDTF *level-1 single-date*
literal that humans (and a future API/dashboard) read and author. EDTF is the standard
spelling for the two things this tracker cares about most: a **partial** date (year /
year-month / quarter) and an **uncertain/approximate** one.

Mapping (lossy on certainty — by design; the internal stance model is richer than EDTF's
three qualifiers, so display collapses it and parsing widens it back conservatively):

* precision  → EXACT ``2026-09-18`` · MONTH ``2026-09`` · QUARTER ``2026-34`` (L2 codes
  33-36 = Q1-Q4) · YEAR ``2026`` · TBA ``XXXX``;
* certainty  → CONFIRMED/DELAYED *(none)* · ESTIMATED/PREDICTED ``~`` (approximate) ·
  RUMORED/LEAKED ``?`` (uncertain). ``%`` (both) parses back to ESTIMATED.

Scope is deliberately the single-date subset that maps to one observation: intervals,
sets (``[2026, 2027]``) and seasons would need a richer internal model and are out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from release_tracker.models import Certainty, DatePrecision

# certainty -> trailing EDTF qualifier (serialize)
_QUALIFIER: dict[Certainty, str] = {
    Certainty.CONFIRMED: "",
    Certainty.DELAYED: "",  # a firm superseding date — no qualifier
    Certainty.ESTIMATED: "~",
    Certainty.PREDICTED: "~",
    Certainty.RUMORED: "?",
    Certainty.LEAKED: "?",
}
# qualifier -> certainty (parse; the lossy inverse — widen to the common stance)
_CERTAINTY: dict[str, Certainty] = {
    "": Certainty.CONFIRMED,
    "~": Certainty.ESTIMATED,
    "?": Certainty.RUMORED,
    "%": Certainty.ESTIMATED,
}
_QUALIFIER_CHARS = "?~%"
_UNKNOWN = "XXXX"


@dataclass(frozen=True, slots=True)
class EdtfDate:
    """A parsed EDTF literal, decoded into the internal date model."""

    when: date | None
    precision: DatePrecision
    certainty: Certainty


def _quarter_of(month: int) -> int:
    """EDTF level-2 sub-year code for a month's quarter (Q1->33 … Q4->36)."""
    return 33 + (month - 1) // 3


def _quarter_start_month(code: int) -> int:
    """First month of an EDTF quarter code (33->1, 34->4, 35->7, 36->10)."""
    return (code - 33) * 3 + 1


def to_edtf(
    when: date | None,
    precision: DatePrecision,
    certainty: Certainty = Certainty.CONFIRMED,
) -> str:
    """Render the internal (date, precision, certainty) triple as an EDTF literal."""
    if when is None or precision is DatePrecision.TBA:
        return _UNKNOWN
    core = {
        DatePrecision.YEAR: f"{when.year:04d}",
        DatePrecision.QUARTER: f"{when.year:04d}-{_quarter_of(when.month)}",
        DatePrecision.MONTH: f"{when.year:04d}-{when.month:02d}",
        DatePrecision.EXACT: when.isoformat(),
    }[precision]
    return core + _QUALIFIER[certainty]


def parse_edtf(text: str) -> EdtfDate:
    """Decode an EDTF level-1 single-date literal into the internal model.

    Raises :class:`ValueError` on anything outside the supported subset (so callers at
    a boundary can surface a clean message).
    """
    body = text.strip()
    if not body:
        raise ValueError("empty date")
    qualifier = ""
    if body[-1] in _QUALIFIER_CHARS:
        qualifier, body = body[-1], body[:-1]
    when, precision = _parse_core(body)
    return EdtfDate(when, precision, _CERTAINTY[qualifier])


def _parse_core(core: str) -> tuple[date | None, DatePrecision]:
    if core in (_UNKNOWN, ""):
        return None, DatePrecision.TBA
    parts = core.split("-")
    widths = [len(p) for p in parts]
    if not all(p.isdigit() for p in parts) or not widths or widths[0] != 4:
        raise ValueError(f"not an EDTF date: {core!r}")
    try:
        year = int(parts[0])
        match parts:
            case [_]:
                return date(year, 1, 1), DatePrecision.YEAR
            case [_, sub] if widths[1] == 2 and 33 <= int(sub) <= 36:
                return date(year, _quarter_start_month(int(sub)), 1), DatePrecision.QUARTER
            case [_, month] if widths[1] == 2:
                return date(year, int(month), 1), DatePrecision.MONTH
            case [_, month, day] if widths[1] == 2 and widths[2] == 2:
                return date(year, int(month), int(day)), DatePrecision.EXACT
            case _:
                raise ValueError(f"not an EDTF date: {core!r}")
    except ValueError as exc:  # date() rejects an out-of-range month/day
        raise ValueError(f"not an EDTF date: {core!r}") from exc
