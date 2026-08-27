"""Tests for the Wikidata source (pure claim parsing; the fetch is best-effort)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from release_tracker.models import Certainty, DatePrecision, Entity, MediaKind, ReleaseChannel
from release_tracker.sources.wikidata import (
    parse_candidates,
    parse_external_ids,
    parse_observations,
    parse_region,
    parse_time,
)

_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
_GREGORIAN = "http://www.wikidata.org/entity/Q1985727"


def _entity() -> Entity:
    return Entity.create("Sony Xperia 1 VIII", MediaKind.TECH, external_ids={"wikidata": "Q1"})


def _time(raw: str, precision: int, calendar: str = _GREGORIAN) -> dict[str, Any]:
    return {"time": raw, "precision": precision, "calendarmodel": calendar}


def _statement(
    raw: str, precision: int, *, region_qid: str | None = None, rank: str = "normal"
) -> dict[str, Any]:
    """One P577 statement in Wikidata's real shape."""
    out: dict[str, Any] = {
        "mainsnak": {
            "snaktype": "value",
            "datavalue": {"type": "time", "value": _time(raw, precision)},
        },
        "rank": rank,
    }
    if region_qid is not None:
        out["qualifiers"] = {
            "P291": [
                {
                    "snaktype": "value",
                    "datavalue": {
                        "type": "wikibase-entityid",
                        "value": {"entity-type": "item", "id": region_qid},
                    },
                }
            ]
        }
    return out


# --- time parsing ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "precision", "expected"),
    [
        # the leading + is not ISO-8601; date.fromisoformat rejects it outright
        ("+2026-06-26T00:00:00Z", 11, (date(2026, 6, 26), DatePrecision.EXACT)),
        # coarse precisions zero-fill the unknown components — the other fromisoformat trap
        ("+2026-06-00T00:00:00Z", 10, (date(2026, 6, 1), DatePrecision.MONTH)),
        ("+2026-00-00T00:00:00Z", 9, (date(2026, 1, 1), DatePrecision.YEAR)),
        # finer than a day: a release date is a day, not an instant
        ("+2026-06-26T13:45:00Z", 14, (date(2026, 6, 26), DatePrecision.EXACT)),
    ],
)
def test_parse_time_handles_wikidata_spellings(
    raw: str, precision: int, expected: tuple[date, DatePrecision]
) -> None:
    assert parse_time(_time(raw, precision)) == expected


def test_parse_time_materialises_the_way_edtf_does() -> None:
    """A year must become Jan 1, not Dec 31 — the observation id hashes the ISO date, so a
    different materialisation would fork a duplicate row against a hand-authored EDTF one."""
    from release_tracker.dates_edtf import parse_edtf

    parsed = parse_time(_time("+2026-00-00T00:00:00Z", 9))
    assert parsed is not None
    assert parsed[0] == parse_edtf("2026").when


@pytest.mark.parametrize(
    ("raw", "precision", "why"),
    [
        ("+2026-00-00T00:00:00Z", 8, "a decade is not a release date"),
        ("-0500-00-00T00:00:00Z", 9, "BCE"),
        ("+0000-00-00T00:00:00Z", 9, "year zero"),
    ],
)
def test_parse_time_rejects_unusable(raw: str, precision: int, why: str) -> None:
    assert parse_time(_time(raw, precision)) is None, why


def test_parse_time_skips_the_julian_calendar() -> None:
    """Julian would be 13 days out; dropping beats silently shifting a date."""
    julian = _time("+1752-09-02T00:00:00Z", 11, "http://www.wikidata.org/entity/Q1985786")
    assert parse_time(julian) is None


# --- statement-level guards --------------------------------------------------


def test_a_somevalue_snak_carries_no_datavalue_and_is_skipped() -> None:
    """P577 has `somevalue` statements in the wild; they have no `datavalue` key at all."""
    claims = {"P577": [{"mainsnak": {"snaktype": "somevalue"}, "rank": "normal"}]}
    assert parse_observations(claims, _entity(), "Q1", _NOW) == []


def test_a_deprecated_statement_is_dropped() -> None:
    claims = {
        "P577": [
            _statement("+2026-06-26T00:00:00Z", 11, rank="deprecated"),
            _statement("+2027-01-05T00:00:00Z", 11),
        ]
    }
    obs = parse_observations(claims, _entity(), "Q1", _NOW)
    assert [o.release_date for o in obs] == [date(2027, 1, 5)]


def test_a_preferred_statement_outranks_a_normal_one_on_confidence() -> None:
    preferred = {"P577": [_statement("+2026-06-26T00:00:00Z", 11, rank="preferred")]}
    normal = {"P577": [_statement("+2026-06-26T00:00:00Z", 11)]}
    assert (
        parse_observations(preferred, _entity(), "Q1", _NOW)[0].confidence
        > parse_observations(normal, _entity(), "Q1", _NOW)[0].confidence
    )


# --- region ------------------------------------------------------------------


def test_an_unqualified_statement_is_worldwide() -> None:
    assert parse_region(_statement("+2026-06-26T00:00:00Z", 11)) == "WW"


def test_the_place_of_publication_qualifier_becomes_a_market() -> None:
    """The real shape on the Xperia 1 VIII: P291 -> Q865 (Taiwan)."""
    assert parse_region(_statement("+2026-05-26T00:00:00Z", 11, region_qid="Q865")) == "TW"


def test_an_unmapped_country_degrades_to_worldwide_rather_than_dropping_the_date() -> None:
    assert parse_region(_statement("+2026-05-26T00:00:00Z", 11, region_qid="Q99999")) == "WW"


def test_per_market_dates_stay_separate_rows() -> None:
    """Region is in resolve's grouping key, so these must survive as distinct slots — that
    is what lets a user in DE see the DE date rather than whichever came first."""
    claims = {
        "P577": [
            _statement("+2026-06-26T00:00:00Z", 11, region_qid="Q30"),
            _statement("+2026-05-26T00:00:00Z", 11, region_qid="Q865"),
        ]
    }
    obs = parse_observations(claims, _entity(), "Q1", _NOW)
    assert {(o.region, o.release_date) for o in obs} == {
        ("US", date(2026, 6, 26)),
        ("TW", date(2026, 5, 26)),
    }
    assert {o.channel for o in obs} == {ReleaseChannel.RETAIL}


# --- the multi-valued P577 case ----------------------------------------------


def test_two_close_worldwide_dates_collapse_into_one_window() -> None:
    """Both would land on (RETAIL, WW), so best_estimates would keep one and the tie-break
    between two same-rank exact dates is a coin flip — the second date would vanish."""
    claims = {
        "P577": [
            _statement("+2026-06-26T00:00:00Z", 11),
            _statement("+2026-05-26T00:00:00Z", 11),
        ]
    }
    obs = parse_observations(claims, _entity(), "Q1", _NOW)
    assert len(obs) == 1
    assert obs[0].release_date == date(2026, 5, 26)
    assert obs[0].date_end == date(2026, 6, 26)


def test_a_window_takes_the_coarser_of_its_two_ends() -> None:
    claims = {
        "P577": [
            _statement("+2026-05-26T00:00:00Z", 11),
            _statement("+2026-06-00T00:00:00Z", 10),
        ]
    }
    obs = parse_observations(claims, _entity(), "Q1", _NOW)
    assert obs[0].precision is DatePrecision.MONTH


def test_far_apart_worldwide_dates_are_a_relaunch_and_stay_separate() -> None:
    claims = {
        "P577": [
            _statement("+2020-06-26T00:00:00Z", 11),
            _statement("+2026-06-26T00:00:00Z", 11),
        ]
    }
    obs = parse_observations(claims, _entity(), "Q1", _NOW)
    assert sorted(o.release_date for o in obs if o.release_date is not None) == [
        date(2020, 6, 26),
        date(2026, 6, 26),
    ]
    assert all(o.date_end is None for o in obs)


def test_a_bare_year_is_estimated_but_a_day_is_confirmed() -> None:
    """A dated statement is a sourced fact; a bare year is too coarse to read as one."""
    exact = {"P577": [_statement("+2026-06-26T00:00:00Z", 11)]}
    year = {"P577": [_statement("+2026-00-00T00:00:00Z", 9)]}
    assert parse_observations(exact, _entity(), "Q1", _NOW)[0].certainty is Certainty.CONFIRMED
    assert parse_observations(year, _entity(), "Q1", _NOW)[0].certainty is Certainty.ESTIMATED


# --- external ids ------------------------------------------------------------


def _id_claim(value: str) -> list[dict[str, Any]]:
    return [
        {
            "mainsnak": {"snaktype": "value", "datavalue": {"type": "string", "value": value}},
            "rank": "normal",
        }
    ]


def test_sibling_site_ids_are_harvested_for_deep_linking() -> None:
    claims = {"P4723": _id_claim("14660"), "P13418": _id_claim("4216")}
    assert parse_external_ids(claims) == {"gsmarena": "14660", "techpowerup_gpu": "4216"}


def test_pull_keys_are_never_harvested() -> None:
    """`capture.entity_for` looks every canonical key up with find_entity_by_external_id, so a
    stray tmdb id on a gadget would fold the tech capture into an existing movie entity."""
    claims = {
        "P4947": _id_claim("693134"),  # TMDB movie
        "P5794": _id_claim("cyberpunk-2077"),  # IGDB
        "P1733": _id_claim("1091500"),  # Steam appid
        "P4723": _id_claim("14660"),
    }
    harvested = parse_external_ids(claims)
    assert harvested == {"gsmarena": "14660"}
    assert not {"tmdb", "igdb", "steam_appid"} & set(harvested)


# --- candidates --------------------------------------------------------------


def test_candidates_carry_the_description_as_the_disambiguator() -> None:
    """Three of five real tech queries return several items; the description is what tells
    an RTX 5090 from an RTX 5090D in the add screen."""
    payload = {
        "search": [
            {"id": "Q131692949", "label": "GeForce RTX 5090", "description": "2025 flagship"},
            {"id": "Q131912282", "label": "GeForce RTX 5090D", "description": "adapted model"},
        ]
    }
    cands = parse_candidates(payload, 6)
    assert [c.canonical_id for c in cands] == ["Q131692949", "Q131912282"]
    assert [c.extra for c in cands] == ["2025 flagship", "adapted model"]
    assert cands[0].url == "https://www.wikidata.org/wiki/Q131692949"
    assert all(c.year is None for c in cands)  # search carries no date


def test_candidates_tolerate_a_missing_description() -> None:
    assert parse_candidates({"search": [{"id": "Q1", "label": "Thing"}]}, 6)[0].extra == ""


def test_candidates_on_a_miss_are_empty() -> None:
    """ "Poco X7" really does return nothing — the miss is the expected case, not an error."""
    assert parse_candidates({"search": []}, 6) == []
    assert parse_candidates({}, 6) == []
