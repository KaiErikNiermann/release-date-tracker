"""Tests for the EDTF boundary layer (to_edtf / parse_edtf)."""

from __future__ import annotations

from datetime import date

import pytest

from release_tracker.dates_edtf import parse_edtf, to_edtf
from release_tracker.models import Certainty, DatePrecision


@pytest.mark.parametrize(
    ("when", "precision", "certainty", "expected"),
    [
        (date(2026, 9, 18), DatePrecision.EXACT, Certainty.CONFIRMED, "2026-09-18"),
        (date(2026, 9, 1), DatePrecision.MONTH, Certainty.ESTIMATED, "2026-09~"),
        (date(2026, 7, 1), DatePrecision.QUARTER, Certainty.CONFIRMED, "2026-35"),  # Q3
        (date(2026, 1, 1), DatePrecision.YEAR, Certainty.RUMORED, "2026?"),
        (None, DatePrecision.TBA, Certainty.CONFIRMED, "XXXX"),
    ],
)
def test_to_edtf(
    when: date | None, precision: DatePrecision, certainty: Certainty, expected: str
) -> None:
    assert to_edtf(when, precision, certainty) == expected


def test_quarter_codes_cover_the_year() -> None:
    assert to_edtf(date(2026, 2, 1), DatePrecision.QUARTER, Certainty.CONFIRMED) == "2026-33"  # Q1
    assert to_edtf(date(2026, 5, 1), DatePrecision.QUARTER, Certainty.CONFIRMED) == "2026-34"  # Q2
    assert to_edtf(date(2026, 8, 1), DatePrecision.QUARTER, Certainty.CONFIRMED) == "2026-35"  # Q3
    assert to_edtf(date(2026, 11, 1), DatePrecision.QUARTER, Certainty.CONFIRMED) == "2026-36"  # Q4


@pytest.mark.parametrize(
    ("text", "when", "precision", "certainty"),
    [
        ("2026-09-18", date(2026, 9, 18), DatePrecision.EXACT, Certainty.CONFIRMED),
        ("2026-09", date(2026, 9, 1), DatePrecision.MONTH, Certainty.CONFIRMED),
        ("2026-34", date(2026, 4, 1), DatePrecision.QUARTER, Certainty.CONFIRMED),  # Q2 -> April
        ("2027", date(2027, 1, 1), DatePrecision.YEAR, Certainty.CONFIRMED),
        ("XXXX", None, DatePrecision.TBA, Certainty.CONFIRMED),
        ("2026-09~", date(2026, 9, 1), DatePrecision.MONTH, Certainty.ESTIMATED),
        ("2027?", date(2027, 1, 1), DatePrecision.YEAR, Certainty.RUMORED),
        ("2026-09-18%", date(2026, 9, 18), DatePrecision.EXACT, Certainty.ESTIMATED),
        ("  2027~ ", date(2027, 1, 1), DatePrecision.YEAR, Certainty.ESTIMATED),  # trimmed
    ],
)
def test_parse_edtf(
    text: str, when: date | None, precision: DatePrecision, certainty: Certainty
) -> None:
    parsed = parse_edtf(text)
    assert (parsed.when, parsed.precision, parsed.certainty) == (when, precision, certainty)


def test_round_trip_is_stable_on_precision_and_date() -> None:
    # certainty is intentionally lossy (6 stances -> 3 qualifiers); date + precision are not.
    for when, precision in [
        (date(2026, 9, 18), DatePrecision.EXACT),
        (date(2026, 9, 1), DatePrecision.MONTH),
        (date(2026, 10, 1), DatePrecision.QUARTER),
        (date(2026, 1, 1), DatePrecision.YEAR),
        (None, DatePrecision.TBA),
    ]:
        back = parse_edtf(to_edtf(when, precision, Certainty.CONFIRMED))
        assert (back.when, back.precision) == (when, precision)


@pytest.mark.parametrize("bad", ["2026-13", "garbage", "2026-9", "2026/2027", "", "26-09", "2026-"])
def test_parse_rejects_unsupported(bad: str) -> None:
    with pytest.raises(ValueError, match=r"EDTF|empty"):
        parse_edtf(bad)
