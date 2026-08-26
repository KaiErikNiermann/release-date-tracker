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


@pytest.mark.parametrize(
    ("text", "canonical"),
    [
        ("2026-Q3", "2026-35"),
        ("2026 Q3", "2026-35"),
        ("2026q3", "2026-35"),
        ("2026-q1", "2026-33"),
        ("2026-Q4~", "2026-36~"),
        ("xxxx", "XXXX"),
    ],
)
def test_human_spellings_normalize_onto_canonical_edtf(text: str, canonical: str) -> None:
    """`2026-35` for Q3 is the one bit of EDTF with no mnemonic — accept the obvious form."""
    parsed = parse_edtf(text)
    assert to_edtf(parsed.when, parsed.precision, parsed.certainty) == canonical


def test_a_double_dot_range_is_an_interval() -> None:
    """`..` is the range separator every other tool uses; EDTF's is `/`."""
    parsed = parse_edtf("2026-06..2026-08")
    assert (parsed.when, parsed.end) == (date(2026, 6, 1), date(2026, 8, 1))
    assert parse_edtf("2026-06..2026-08") == parse_edtf("2026-06/2026-08")


def test_normalizing_does_not_widen_what_is_rejected() -> None:
    for bad in ["2026-Q5", "2026-Q", "Q3", "2026-13", "2026 Q3 Q4"]:
        with pytest.raises(ValueError, match=r"EDTF|empty|interval"):
            parse_edtf(bad)


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


@pytest.mark.parametrize("bad", ["2026-13", "garbage", "2026-9", "2029/2027", "", "26-09", "2026-"])
def test_parse_rejects_unsupported(bad: str) -> None:
    # note 2029/2027 is a *reversed* interval (end before start) -> rejected
    with pytest.raises(ValueError, match=r"EDTF|empty|interval"):
        parse_edtf(bad)


def test_to_edtf_emits_interval() -> None:
    assert (
        to_edtf(
            date(2027, 1, 1),
            DatePrecision.YEAR,
            Certainty.ESTIMATED,
            end=date(2029, 1, 1),
            end_precision=DatePrecision.YEAR,
        )
        == "2027~/2029~"
    )
    # confirmed window has no qualifier on either bound
    assert to_edtf(date(2026, 3, 1), DatePrecision.MONTH, end=date(2026, 9, 1)) == "2026-03/2026-09"


def test_parse_interval_into_bounds() -> None:
    parsed = parse_edtf("2027~/2029~")
    assert parsed.when == date(2027, 1, 1)
    assert parsed.end == date(2029, 1, 1)
    assert parsed.precision is DatePrecision.YEAR and parsed.end_precision is DatePrecision.YEAR
    assert parsed.certainty is Certainty.ESTIMATED  # start-bound qualifier governs


def test_interval_round_trips_on_bounds_and_precision() -> None:
    edtf = "2027~/2029~"
    back = parse_edtf(edtf)
    assert to_edtf(back.when, back.precision, back.certainty, end=back.end) == edtf
