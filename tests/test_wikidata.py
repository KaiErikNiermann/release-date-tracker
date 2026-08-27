"""Tests for the Wikidata source (pure claim parsing; the fetch is best-effort)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, cast

import pytest

from release_tracker.models import Certainty, DatePrecision, Entity, MediaKind, ReleaseChannel
from release_tracker.sources.wikidata import (
    Lineage,
    WikidataSource,
    lineage_query,
    link_query,
    parse_candidates,
    parse_external_ids,
    parse_lineage,
    parse_link_bindings,
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


# --- the identifier hub for film / TV / games --------------------------------


def _binding(**vals: str) -> dict[str, Any]:
    return {"results": {"bindings": [{k: {"value": v} for k, v in vals.items()}]}}


def test_the_join_reads_sibling_links_off_the_resolved_item() -> None:
    payload = _binding(
        item="http://www.wikidata.org/entity/Q109228991",
        imdb="tt15239678",
        metacritic="movie/dune-part-two",
        rottentomatoes="m/dune_part_two",
        official="https://www.dunemovie.net/",
    )
    assert parse_link_bindings(payload) == {
        "wikidata": "Q109228991",
        "imdb": "tt15239678",
        "metacritic": "movie/dune-part-two",
        "rottentomatoes": "m/dune_part_two",
        "official_website": "https://www.dunemovie.net/",
    }


def test_the_qid_is_kept_so_the_next_pull_skips_the_join() -> None:
    ids = parse_link_bindings(_binding(item="http://www.wikidata.org/entity/Q1", imdb="tt1"))
    assert ids["wikidata"] == "Q1"


def test_absent_optionals_are_omitted_not_blanked() -> None:
    assert parse_link_bindings(_binding(item="http://www.wikidata.org/entity/Q1")) == {
        "wikidata": "Q1"
    }


def test_a_join_that_matches_nothing_is_empty() -> None:
    """The common shape for anything Wikidata has no item for — a miss, not an error."""
    assert parse_link_bindings({"results": {"bindings": []}}) == {}
    assert parse_link_bindings({}) == {}


def test_the_query_pins_the_item_by_our_own_id() -> None:
    """Exact statement match, so there is no title similarity involved and therefore no way
    to bind the wrong work."""
    q = link_query("P4947", "693134")
    assert '?item wdt:P4947 "693134"' in q
    assert "wdt:P345" in q and "wdt:P1258" in q
    assert q.rstrip().endswith("LIMIT 1")


def test_supports_splits_dates_from_identifiers() -> None:
    """Tech is the only kind Wikidata may date. Film/TV/games have TMDB and IGDB, which are
    strictly better at it — here Wikidata only ever says where else the work lives."""
    src = WikidataSource()
    assert src.supports(MediaKind.TECH) is True
    for kind in (MediaKind.MOVIE, MediaKind.TV, MediaKind.GAME):
        assert src.supports(kind) is True
    assert src.supports(MediaKind.BOOK) is False


@pytest.mark.asyncio
async def test_only_tech_gets_candidates() -> None:
    """`supports` is true for films now, but their disambiguation stays with TMDB — Wikidata
    label-matches everything at score 1.0, so its candidates here would be pure noise."""
    src = WikidataSource()
    from release_tracker.config import Settings

    got = await src.search_candidates(
        cast("Any", None), "Dune", MediaKind.MOVIE, Settings(), limit=3
    )
    assert got == []


@pytest.mark.asyncio
async def test_a_film_pull_never_produces_a_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """The load-bearing half of the split: Wikidata must not compete with TMDB on dates."""
    from release_tracker.config import Settings
    from release_tracker.sources import wikidata as mod

    async def _fake(*_a: Any, **_k: Any) -> dict[str, Any]:
        return _binding(item="http://www.wikidata.org/entity/Q1", imdb="tt1")

    monkeypatch.setattr(mod, "get_json", _fake)
    film = Entity.create("Dune", MediaKind.MOVIE, external_ids={"tmdb": "693134"})
    result = await WikidataSource().pull(cast("Any", None), film, Settings())
    assert result.observations == []
    assert result.external_ids == {"wikidata": "Q1", "imdb": "tt1"}


@pytest.mark.asyncio
async def test_a_game_joins_on_the_steam_appid_not_the_igdb_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P5794 stores IGDB's slug and we pin its numeric id, so they never compare. Joining on
    the numeric id would silently match nothing forever."""
    from release_tracker.config import Settings
    from release_tracker.sources import wikidata as mod

    seen: list[str] = []

    async def _fake(_client: Any, _url: str, **kw: Any) -> dict[str, Any]:
        seen.append(str(kw.get("params", {}).get("query", "")))
        return _binding(item="http://www.wikidata.org/entity/Q3182559")

    monkeypatch.setattr(mod, "get_json", _fake)
    game = Entity.create(
        "Cyberpunk 2077", MediaKind.GAME, external_ids={"igdb": "1877", "steam_appid": "1091500"}
    )
    await WikidataSource().pull(cast("Any", None), game, Settings())
    assert 'wdt:P1733 "1091500"' in seen[0]
    assert "1877" not in seen[0]


@pytest.mark.asyncio
async def test_a_game_with_no_steam_appid_makes_no_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from release_tracker.config import Settings
    from release_tracker.sources import wikidata as mod

    called = False

    async def _fake(*_a: Any, **_k: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(mod, "get_json", _fake)
    game = Entity.create("Some Game", MediaKind.GAME, external_ids={"igdb": "1877"})
    assert await WikidataSource().pull(cast("Any", None), game, Settings()) is not None
    assert called is False


@pytest.mark.asyncio
async def test_a_junk_pinned_id_never_reaches_the_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An external id is a bare token. A hand-pinned value that isn't one — `rdt resolve pin`
    takes free text — must not be interpolated into a query at all."""
    from release_tracker.config import Settings
    from release_tracker.sources import wikidata as mod

    called = False

    async def _fake(*_a: Any, **_k: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(mod, "get_json", _fake)
    film = Entity.create("X", MediaKind.MOVIE, external_ids={"tmdb": '1" . ?x ?y ?z . #'})
    assert await WikidataSource().pull(cast("Any", None), film, Settings()) is not None
    assert called is False


@pytest.mark.asyncio
async def test_a_game_falls_back_to_the_igdb_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tracked games predate Steam-appid pinning, so the slug is the one that has to
    work. P5794 stores exactly that, which is why the puller now pins it."""
    from release_tracker.config import Settings
    from release_tracker.sources import wikidata as mod

    seen: list[str] = []

    async def _fake(_client: Any, _url: str, **kw: Any) -> dict[str, Any]:
        seen.append(str(kw.get("params", {}).get("query", "")))
        return _binding(item="http://www.wikidata.org/entity/Q3182559")

    monkeypatch.setattr(mod, "get_json", _fake)
    game = Entity.create(
        "Cyberpunk 2077",
        MediaKind.GAME,
        external_ids={"igdb": "1877", "igdb_slug": "cyberpunk-2077"},
    )
    result = await WikidataSource().pull(cast("Any", None), game, Settings())
    assert 'wdt:P5794 "cyberpunk-2077"' in seen[0]
    assert result.external_ids["wikidata"] == "Q3182559"


# --- lineage: the family a speculative entry descends from --------------------------------
def _lineage_rows(**cells: str) -> dict[str, Any]:
    return {"results": {"bindings": [{k: {"value": v} for k, v in cells.items()}]}}


def test_lineage_query_only_ever_names_a_validated_qid() -> None:
    """It is interpolated into SPARQL, so the caller validates first — this pins the shape
    that validation protects."""
    sparql = lineage_query("Q107542665")
    assert "wd:Q107542665" in sparql
    assert "?brandLabel" in sparql and "?classLabel" in sparql
    assert 'wikibase:language "en"' in sparql


def test_parse_lineage_reads_the_row() -> None:
    found = parse_lineage(
        _lineage_rows(
            date="2022-02-25T00:00:00Z",
            brandLabel="Valve",
            classLabel="handheld gaming PC model series",
        ),
        "Q107542665",
        "Steam Deck",
    )
    assert found == Lineage(
        qid="Q107542665",
        label="Steam Deck",
        released=date(2022, 2, 25),
        brand="Valve",
        instance_of="handheld gaming PC model series",
    )


def test_parse_lineage_survives_every_optional_missing() -> None:
    """Each claim is an OPTIONAL, and plenty of items carry none of them. The identity is
    the only part that has to be there."""
    found = parse_lineage({"results": {"bindings": [{}]}}, "Q1", "Thing")
    assert found == Lineage(qid="Q1", label="Thing")


def test_parse_lineage_drops_a_date_it_cannot_represent() -> None:
    """A BCE or otherwise unrepresentable date is worth losing, not raising over —
    the lineage is a prefill, and no field on it is load-bearing."""
    found = parse_lineage(_lineage_rows(date="-0500-01-01T00:00:00Z"), "Q1", "Thing")
    assert found.released is None


def test_parse_lineage_tolerates_an_empty_result() -> None:
    assert parse_lineage({}, "Q1", "Thing") == Lineage(qid="Q1", label="Thing")
