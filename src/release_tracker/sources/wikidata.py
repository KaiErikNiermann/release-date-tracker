"""Wikidata — the only structured date source for tech, and a hub for other sites' ids.

Consumer tech has no TMDB. The specialist databases that *do* cover it decline automated
extraction: GSMArena's ``robots.txt`` blocks ``ClaudeBot``/``anthropic-ai`` outright and
its RSL licence (``gsmarena.com/license.xml``) permits ``ai-summarize``/``search-index``
while prohibiting ``ai-inference``; TechPowerUp prohibits text-and-data-mining in prose.
So we never fetch them — but Wikidata records *their identifiers* (``P4723`` GSMArena,
``P13418``/``P13844`` TechPowerUp), which lets us deep-link straight to the right device
page. That is what :mod:`release_tracker.sources.links` turns into a card section, and it
is the shape their licence actually asks for: attribution, not extraction.

Coverage is thin and the design leans on that rather than hiding it — 253 smartphone models
carry a ``P577`` release date and only 15 shipped in 2025 or later. Two consequences are
load-bearing:

* **``pull`` never blind-searches.** TMDB/Steam do, because their coverage is near-total so
  the top hit is usually right. ``wbsearchentities`` matches *every* item in Wikidata —
  people, concepts, ships — so for an unpinned gadget the blind-hit rate is dominated by
  wrong items, and a wrong retail date is worse than no date. The QID arrives from an
  explicit candidate pick or ``rdt resolve pin``, or it doesn't arrive.
* **A miss is the expected case**, not an error — ``matching.requires_canonical_for_capture``
  keeps tech tracking as a bare entity when Wikidata has never heard of it.

Parsing is split pure/IO the way :mod:`release_tracker.sources.ddg` is, so the claim shapes
are unit-testable without network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final, cast

import httpx

from release_tracker.clock import utc_now
from release_tracker.config import Settings
from release_tracker.logging import get_logger
from release_tracker.models import (
    Certainty,
    DatePrecision,
    Entity,
    MediaKind,
    ReleaseChannel,
    ReleaseObservation,
    SourceTier,
)
from release_tracker.sources.base import Candidate, SourceResult, get_json, pinned_id

log = get_logger("wikidata")

_API = "https://www.wikidata.org/w/api.php"
_ENTITY_DATA = "https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
_ITEM_URL = "https://www.wikidata.org/wiki/{qid}"

# Interpolated into a URL path, so validated rather than trusted: a hand-typed
# `rdt resolve pin ... wikidata=<junk>` must not become an arbitrary request.
_QID_RE: Final = re.compile(r"^Q\d+$")

_P_PUBLICATION_DATE: Final = "P577"
_P_BRAND: Final = "P1716"
_P_INSTANCE_OF: Final = "P31"
_P_PLACE_OF_PUBLICATION: Final = "P291"
_P_COUNTRY: Final = "P17"

# Proleptic Gregorian. Julian (Q1985786) would be 13 days out; no consumer device is dated
# that way, but a vintage-hardware item could be, and silently shifting a date is worse than
# dropping one.
_GREGORIAN: Final = "http://www.wikidata.org/entity/Q1985727"

# Two unqualified release dates this close together are one uncertain fact, not two facts —
# they get collapsed into a window. Further apart is a genuine relaunch and stays separate.
_WINDOW_DAYS: Final = 90

# Ids harvested for *linking only*. Deliberately excludes tmdb/igdb/steam_appid: those are
# pull keys, and `capture.entity_for` looks every key of `report.canonical` up via
# `find_entity_by_external_id` — a stray P4947 on a gadget would fold the tech capture into
# an existing movie entity. The media ids belong to the id-hub step, added deliberately.
_LINK_ID_PROPERTIES: Final[dict[str, str]] = {
    "P4723": "gsmarena",
    "P13418": "techpowerup_gpu",
    "P13844": "techpowerup_cpu",
    "P345": "imdb",
    "P1712": "metacritic",
    "P856": "official_website",
}

# Release-market qualifier QIDs -> ISO 3166-1 alpha-2, in the house style of `tech.py`'s
# policy tables. Resolving each QID's P297 properly would cost an extra fetch per distinct
# qualifier, for a field that only needs the markets people actually track. An unmapped
# country degrades to WW rather than dropping the date.
_COUNTRY_QIDS: Final[dict[str, str]] = {
    "Q30": "US",
    "Q16": "CA",
    "Q145": "GB",
    "Q27": "IE",
    "Q408": "AU",
    "Q664": "NZ",
    "Q183": "DE",
    "Q142": "FR",
    "Q38": "IT",
    "Q29": "ES",
    "Q55": "NL",
    "Q31": "BE",
    "Q39": "CH",
    "Q40": "AT",
    "Q34": "SE",
    "Q20": "NO",
    "Q35": "DK",
    "Q33": "FI",
    "Q36": "PL",
    "Q45": "PT",
    "Q17": "JP",
    "Q884": "KR",
    "Q148": "CN",
    "Q865": "TW",
    "Q8646": "HK",
    "Q334": "SG",
    "Q668": "IN",
    "Q155": "BR",
    "Q96": "MX",
    "Q43": "TR",
    "Q258": "ZA",
}

_WORLDWIDE: Final = "WW"

_SPARQL = "https://query.wikidata.org/sparql"

# Kind -> (Wikidata property, the key we already pin). The join runs in *our* direction: we
# hold the id, so the item is found by exact statement match rather than by name, which means
# no title-similarity guessing and no false positives.
#
# Tried in order; the first id we actually hold wins. Games need two because P5794 stores
# IGDB's *slug* ("cyberpunk-2077") while the id we pin from a pull is numeric — they never
# compare, so the slug is pinned alongside it and the Steam appid covers whatever predates
# that.
_REVERSE_JOIN: Final[dict[MediaKind, tuple[tuple[str, str], ...]]] = {
    MediaKind.MOVIE: (("P4947", "tmdb"),),
    MediaKind.TV: (("P4983", "tmdb"),),
    MediaKind.GAME: (("P1733", "steam_appid"), ("P5794", "igdb_slug")),
}

# SPARQL variable -> our external_ids key. These are all places a person goes to read a date
# or a status; none of them is a puller, so every one lands on the card as a plain link.
_LINK_VARS: Final[dict[str, tuple[str, str]]] = {
    "imdb": ("P345", "imdb"),
    "metacritic": ("P1712", "metacritic"),
    "rottentomatoes": ("P1258", "rottentomatoes"),
    "official": ("P856", "official_website"),
}

# An external id is a bare token; anything else is not one and must not reach a query string.
_ID_VALUE_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,127}$")

# `+2026-06-26T00:00:00Z`. Coarse precisions zero-fill the unknown components
# (`+2026-00-00T...` for a year), which `date.fromisoformat` rejects outright.
_TIME_RE: Final = re.compile(r"^([+-])(\d{4,})-(\d{2})-(\d{2})T")


def _statements(claims: dict[str, Any], pid: str) -> list[dict[str, Any]]:
    """The non-deprecated statements for one property, newest shape tolerated."""
    raw = claims.get(pid)
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in cast("list[Any]", raw):
        if isinstance(item, dict) and item.get("rank") != "deprecated":
            out.append(cast("dict[str, Any]", item))
    return out


def _datavalue(snak: object, want_type: str) -> Any | None:
    """The ``datavalue.value`` of a snak, or None unless it is a real value of ``want_type``.

    ``snaktype`` is ``value`` / ``somevalue`` / ``novalue``, and the last two carry **no**
    ``datavalue`` key at all — P577 does have ``somevalue`` statements in the wild, so this
    guard is what stops the parser raising on them. The ``value`` shape is discriminated by
    ``datavalue.type``, never by its Python type: ``string`` is a bare ``str`` while
    ``wikibase-entityid`` and ``time`` are dicts.
    """
    if not isinstance(snak, dict):
        return None
    snak_d = cast("dict[str, Any]", snak)
    if snak_d.get("snaktype") != "value":
        return None
    dv = snak_d.get("datavalue")
    if not isinstance(dv, dict):
        return None
    dv_d = cast("dict[str, Any]", dv)
    return dv_d.get("value") if dv_d.get("type") == want_type else None


def parse_time(value: dict[str, Any]) -> tuple[date, DatePrecision] | None:
    """A Wikidata time value as ``(date, precision)``, or None if it isn't usable.

    Three shapes bite here. The leading ``+`` and the zero-filled components at coarse
    precision (``+2026-00-00T00:00:00Z`` for a year) both make ``date.fromisoformat`` raise,
    and the materialised day must match what :mod:`release_tracker.dates_edtf` produces for
    the same precision — year to Jan 1, month to the 1st. ``ReleaseObservation.id`` hashes
    the ISO date, so a year materialised as Dec 31 here would fork a duplicate row against a
    hand-authored EDTF one saying the same thing.
    """
    raw = value.get("time")
    if not isinstance(raw, str):
        return None
    if (model := value.get("calendarmodel")) is not None and model != _GREGORIAN:
        return None
    match = _TIME_RE.match(raw)
    if match is None or match.group(1) == "-":  # BCE is not a release date
        return None
    precision_raw = value.get("precision")
    if not isinstance(precision_raw, int):
        return None
    year, month, day = int(match.group(2)), int(match.group(3)), int(match.group(4))
    if year == 0:
        return None
    # 14..12 are hour/minute/second — a release date is a day, not an instant.
    if precision_raw >= 11:
        precision = DatePrecision.EXACT
    elif precision_raw == 10:
        precision = DatePrecision.MONTH
    elif precision_raw == 9:
        precision = DatePrecision.YEAR
    else:
        return None  # decade or coarser is not a release date
    # Never synthesise QUARTER: Wikidata has no quarter code, and `resolve.PRECISION_RANK`
    # puts QUARTER above YEAR, so a fabricated one would outrank an honestly-coarse claim.
    if precision is DatePrecision.YEAR:
        return date(year, 1, 1), precision
    if precision is DatePrecision.MONTH:
        return date(year, max(month, 1), 1), precision
    return date(year, max(month, 1), max(day, 1)), precision


def parse_region(statement: dict[str, Any]) -> str:
    """The market a release statement is scoped to, as ISO-2, or ``WW`` when unqualified.

    Region is the whole point for tech: ``tech.py``'s thesis is that launch and price are
    per-country and can't be VPN-dodged, and ``resolve`` groups estimates by region, so a
    ``(retail, JP)`` claim and a ``(retail, US)`` one survive as separate slots and the user
    sees *their* market's date.
    """
    qualifiers = statement.get("qualifiers")
    if not isinstance(qualifiers, dict):
        return _WORLDWIDE
    quals = cast("dict[str, Any]", qualifiers)
    for pid in (_P_PLACE_OF_PUBLICATION, _P_COUNTRY):
        for snak in cast("list[Any]", quals.get(pid) or []):
            entity_id = _datavalue(snak, "wikibase-entityid")
            if isinstance(entity_id, dict):
                qid = cast("dict[str, Any]", entity_id).get("id")
                if isinstance(qid, str) and (iso := _COUNTRY_QIDS.get(qid)) is not None:
                    return iso
    return _WORLDWIDE


def parse_external_ids(claims: dict[str, Any]) -> dict[str, str]:
    """The sibling-site ids on an item — what makes the deep links possible."""
    out: dict[str, str] = {}
    for pid, key in _LINK_ID_PROPERTIES.items():
        for statement in _statements(claims, pid):
            value = _datavalue(statement.get("mainsnak"), "string")
            if isinstance(value, str) and value:
                out[key] = value
                break
    return out


def _dated(statement: dict[str, Any]) -> tuple[date, DatePrecision, str, str] | None:
    """One release statement reduced to ``(date, precision, region, rank)``."""
    value = _datavalue(statement.get("mainsnak"), "time")
    if not isinstance(value, dict):
        return None
    parsed = parse_time(cast("dict[str, Any]", value))
    if parsed is None:
        return None
    when, precision = parsed
    rank = statement.get("rank")
    return when, precision, parse_region(statement), rank if isinstance(rank, str) else "normal"


def _observation(
    entity: Entity,
    url: str,
    when: date,
    precision: DatePrecision,
    region: str,
    rank: str,
    now: datetime,
    *,
    end: date | None = None,
) -> ReleaseObservation:
    """One retail-release row.

    Everything is ``RETAIL``: P577 asserts publication, and Wikidata models an announcement
    as a *different* property (P6949), so reading an earlier date as ``PREORDER`` would be
    inventing a claim the source never made.
    """
    return ReleaseObservation(
        entity_id=entity.id,
        channel=ReleaseChannel.RETAIL,
        region=region,
        release_date=when,
        date_end=end,
        precision=precision,
        # A dated Wikidata statement is a sourced fact, not a guess; a bare year is too
        # coarse to read as an announcement.
        certainty=(Certainty.ESTIMATED if precision is DatePrecision.YEAR else Certainty.CONFIRMED),
        source_tier=SourceTier.AGGREGATOR,  # crowd-sourced, not a first-party store
        provider="wikidata",
        source_name="Wikidata",
        source_url=url,
        source_quote=when.isoformat() if end is None else f"{when.isoformat()}/{end.isoformat()}",
        confidence=0.85 if rank == "preferred" else 0.7,
        fetched_at=now,
    )


# Coarse sorts high, so `max` over a window's ends picks the less precise one.
_PRECISION_ORDER: Final[dict[DatePrecision, int]] = {
    DatePrecision.EXACT: 0,
    DatePrecision.MONTH: 1,
    DatePrecision.QUARTER: 2,
    DatePrecision.YEAR: 3,
    DatePrecision.TBA: 4,
}


def parse_observations(
    claims: dict[str, Any], entity: Entity, qid: str, now: datetime
) -> list[ReleaseObservation]:
    """Release rows for one item — one per market, with the unqualified ones collapsed.

    ``P577`` is multi-valued and the extra values are usually *markets*, which land on
    distinct region slots and want to stay separate. Several **unqualified** dates are a
    different thing: they share ``(RETAIL, WW)``, so ``resolve.best_estimates`` would keep
    exactly one and the tie-break between two same-rank exact dates is effectively a coin
    flip — the user would never learn a second date existed. Close together they are one
    uncertain fact, so they become a window; far apart they are a relaunch and stay apart.
    """
    url = _ITEM_URL.format(qid=qid)
    dated = [d for s in _statements(claims, _P_PUBLICATION_DATE) if (d := _dated(s)) is not None]
    if not dated:
        return []

    out: list[ReleaseObservation] = []
    worldwide = sorted(d for d in dated if d[2] == _WORLDWIDE)
    for when, precision, region, rank in dated:
        if region != _WORLDWIDE:
            out.append(_observation(entity, url, when, precision, region, rank, now))

    if not worldwide:
        return out
    first, last = worldwide[0], worldwide[-1]
    if len(worldwide) > 1 and (last[0] - first[0]).days <= _WINDOW_DAYS:
        out.append(
            _observation(
                entity,
                url,
                first[0],
                # the coarser of the two ends describes the window honestly
                max(first[1], last[1], key=_PRECISION_ORDER.__getitem__),
                _WORLDWIDE,
                first[3],
                now,
                end=last[0],
            )
        )
        return out
    out.extend(
        _observation(entity, url, when, precision, region, rank, now)
        for when, precision, region, rank in worldwide
    )
    return out


def link_query(pid: str, value: str) -> str:
    """The one-shot join: find the item carrying our id, and read its sibling links off it.

    One round trip rather than two (search for the QID, then fetch the entity), which matters
    because ``lookup.report_for_candidate`` pulls its sources serially — this sits on the hot
    path of every film lookup.
    """
    optionals = "\n".join(
        f"  OPTIONAL {{ ?item wdt:{p} ?{var} }}" for var, (p, _) in _LINK_VARS.items()
    )
    selects = " ".join(f"?{var}" for var in _LINK_VARS)
    return (
        f'SELECT ?item {selects} WHERE {{\n  ?item wdt:{pid} "{value}" .\n{optionals}\n}} LIMIT 1'
    )


def parse_link_bindings(payload: dict[str, Any]) -> dict[str, str]:
    """A SPARQL result row as ``{our_id_key: value}``, including the QID we resolved."""
    results = payload.get("results")
    if not isinstance(results, dict):
        return {}
    rows = cast("dict[str, Any]", results).get("bindings")
    if not isinstance(rows, list) or not rows:
        return {}
    first = cast("list[Any]", rows)[0]
    if not isinstance(first, dict):
        return {}
    row = cast("dict[str, Any]", first)

    def value_of(var: str) -> str | None:
        cell = row.get(var)
        if not isinstance(cell, dict):
            return None
        raw = cast("dict[str, Any]", cell).get("value")
        return raw.strip() if isinstance(raw, str) and raw.strip() else None

    out: dict[str, str] = {}
    if (item := value_of("item")) is not None:
        qid = item.rsplit("/", 1)[-1]
        if _QID_RE.fullmatch(qid):
            # cached so the next pull skips the join entirely
            out["wikidata"] = qid
    for var, (_, key) in _LINK_VARS.items():
        if (found := value_of(var)) is not None:
            out[key] = found
    return out


def parse_candidates(payload: dict[str, Any], limit: int) -> list[Candidate]:
    """Search hits as candidates. Wikidata search carries no date, so ``year`` stays None."""
    hits = payload.get("search")
    if not isinstance(hits, list):
        return []
    out: list[Candidate] = []
    for hit in cast("list[Any]", hits)[:limit]:
        if not isinstance(hit, dict):
            continue
        item = cast("dict[str, Any]", hit)
        qid = item.get("id")
        if not isinstance(qid, str) or _QID_RE.fullmatch(qid) is None:
            continue
        label = item.get("label")
        out.append(
            Candidate(
                source="wikidata",
                id_key="wikidata",
                canonical_id=qid,
                title=str(label) if isinstance(label, str) else qid,
                # `description` is absent on plenty of items; it is the disambiguator the
                # add screen shows ("RTX 50 series graphics card model adapted for ...").
                extra=str(item.get("description") or ""),
                url=_ITEM_URL.format(qid=qid),
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class Lineage:
    """What Wikidata knows about the item a speculative entry descends from.

    Only the facts that survive a generation. Brand comes from P1716 and never P176: the
    Steam Deck's P176 is *Quanta Computer*, its contract manufacturer, which is true and
    useless — nobody searches for a Quanta handheld.
    """

    qid: str
    label: str
    released: date | None = None
    brand: str | None = None
    instance_of: str | None = None


def lineage_query(qid: str) -> str:
    """SPARQL for one item's release date, brand and class, with labels resolved server-side.

    One request instead of the four an API walk would take (item, then a label lookup per
    referenced QID), which matters because the add screen runs this while the user waits.
    """
    return (
        "SELECT ?date ?brandLabel ?classLabel WHERE {\n"
        f"  OPTIONAL {{ wd:{qid} wdt:{_P_PUBLICATION_DATE} ?date . }}\n"
        f"  OPTIONAL {{ wd:{qid} wdt:{_P_BRAND} ?brand . }}\n"
        f"  OPTIONAL {{ wd:{qid} wdt:{_P_INSTANCE_OF} ?class . }}\n"
        '  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }\n'
        "} LIMIT 1"
    )


def _binding(row: dict[str, Any], name: str) -> str | None:
    """One SPARQL binding's value, or None when the OPTIONAL didn't match."""
    cell = row.get(name)
    if not isinstance(cell, dict):
        return None
    value = cast("dict[str, Any]", cell).get("value")
    return value if isinstance(value, str) and value else None


def parse_lineage(payload: dict[str, Any], qid: str, label: str) -> Lineage:
    """Fold the single result row into a Lineage; every field is optional by design."""
    results = payload.get("results")
    rows = cast("dict[str, Any]", results).get("bindings") if isinstance(results, dict) else None
    row = (
        cast("dict[str, Any]", cast("list[Any]", rows)[0])
        if isinstance(rows, list) and rows and isinstance(cast("list[Any]", rows)[0], dict)
        else {}
    )
    released: date | None = None
    if (raw := _binding(row, "date")) is not None:
        try:
            released = date.fromisoformat(raw[:10])
        except ValueError:  # a BCE or otherwise unrepresentable date — not worth a failure
            released = None
    return Lineage(
        qid=qid,
        label=label,
        released=released,
        brand=_binding(row, "brandLabel"),
        instance_of=_binding(row, "classLabel"),
    )


async def find_lineage(client: httpx.AsyncClient, stem: str) -> Lineage | None:
    """Resolve a product-family stem to the item a successor would descend from.

    Best-effort throughout: this only ever *prefills* a draft the user is about to review,
    so a miss costs an empty field, never a failure. Returns None when Wikidata has never
    heard of the family, which is the common case for new hardware.
    """
    try:
        payload = cast(
            "dict[str, Any]",
            await get_json(
                client,
                _API,
                params={
                    "action": "wbsearchentities",
                    "search": stem,
                    "type": "item",
                    "language": "en",
                    "uselang": "en",
                    "format": "json",
                    "limit": "1",
                },
            ),
        )
    except Exception as exc:
        log.warning("wikidata.lineage_search_error", stem=stem, error=str(exc))
        return None
    hits = parse_candidates(payload, limit=1)
    if not hits:
        return None
    qid, label = hits[0].canonical_id, hits[0].title
    if _QID_RE.fullmatch(qid) is None:  # never interpolate an unvalidated id into SPARQL
        return None
    try:
        claims = cast(
            "dict[str, Any]",
            await get_json(
                client,
                _SPARQL,
                params={"query": lineage_query(qid), "format": "json"},
                headers={"Accept": "application/sparql-results+json"},
            ),
        )
    except Exception as exc:  # the identity alone is still worth having
        log.warning("wikidata.lineage_claims_error", qid=qid, error=str(exc))
        return Lineage(qid=qid, label=label)
    found = parse_lineage(claims, qid, label)
    log.info("wikidata.lineage", stem=stem, qid=qid, brand=found.brand, released=found.released)
    return found


class WikidataSource:
    name = "wikidata"

    def supports(self, kind: MediaKind) -> bool:
        """Tech for dates; the rest for identifiers only.

        The two jobs are different and stay different. Tech has no other structured source,
        so here Wikidata is the date source. Film/TV/games already have TMDB and IGDB, which
        are strictly better at dates — Wikidata must never compete with them on that. What it
        alone can do is say *where else this work lives*, which is what the card's Sources
        section is made of.
        """
        return kind is MediaKind.TECH or kind in _REVERSE_JOIN

    def unavailable(self, settings: Settings) -> str | None:
        """Never — this source needs no credentials."""
        del settings
        return None

    async def pull(
        self, client: httpx.AsyncClient, entity: Entity, settings: Settings
    ) -> SourceResult:
        del settings  # region comes from the claims themselves, not the user's profile
        if entity.kind is not MediaKind.TECH:
            return await self._pull_links(client, entity)
        return await self._pull_tech(client, entity)

    async def _pull_links(self, client: httpx.AsyncClient, entity: Entity) -> SourceResult:
        """Sibling-site ids for a work we already have a canonical id for. Never any dates.

        The join is exact: we look for the item carrying *our* id, so there is no title
        matching and therefore no false positive. A miss just means Wikidata has no item.
        """
        pid, value = "", None
        for candidate_pid, key in _REVERSE_JOIN.get(entity.kind, ()):
            found, skip = pinned_id(entity.external_ids, key)
            if skip or found is None or _ID_VALUE_RE.fullmatch(found) is None:
                continue
            pid, value = candidate_pid, found
            break
        if value is None:
            return SourceResult()
        try:
            payload = cast(
                "dict[str, Any]",
                await get_json(
                    client,
                    _SPARQL,
                    params={"query": link_query(pid, value), "format": "json"},
                    headers={"Accept": "application/sparql-results+json"},
                ),
            )
        except Exception as exc:  # best-effort: a link miss must never fail a date pull
            log.warning("wikidata.link_error", entity=entity.title, error=str(exc))
            return SourceResult()
        external_ids = parse_link_bindings(payload)
        log.info(
            "wikidata.links", entity=entity.title, join=f"{pid}={value}", ids=sorted(external_ids)
        )
        return SourceResult(external_ids=external_ids)

    async def _pull_tech(self, client: httpx.AsyncClient, entity: Entity) -> SourceResult:
        qid, skip = pinned_id(entity.external_ids, "wikidata")
        # No blind search on a miss — see the module docstring. An unpinned gadget stays
        # unpinned rather than getting some unrelated item's date.
        if skip or qid is None or _QID_RE.fullmatch(qid) is None:
            return SourceResult()
        try:
            payload = cast("dict[str, Any]", await get_json(client, _ENTITY_DATA.format(qid=qid)))
        except Exception as exc:
            log.warning("wikidata.fetch_error", entity=entity.title, qid=qid, error=str(exc))
            return SourceResult()
        entities = payload.get("entities")
        if not isinstance(entities, dict):
            return SourceResult()
        item = cast("dict[str, Any]", entities).get(qid)
        if not isinstance(item, dict):
            return SourceResult()
        raw_claims = cast("dict[str, Any]", item).get("claims")
        claims = cast("dict[str, Any]", raw_claims) if isinstance(raw_claims, dict) else {}

        observations = parse_observations(claims, entity, qid, utc_now())
        external_ids = parse_external_ids(claims)
        log.info(
            "wikidata.item",
            entity=entity.title,
            qid=qid,
            observations=len(observations),
            ids=sorted(external_ids),
        )
        return SourceResult(observations=observations, external_ids=external_ids)

    async def search_candidates(
        self,
        client: httpx.AsyncClient,
        query: str,
        kind: MediaKind,
        settings: Settings,
        *,
        limit: int = 6,
    ) -> list[Candidate]:
        del settings
        # `supports` is now true for film/TV/games as well, but only for their *ids* — they
        # have TMDB and IGDB to disambiguate with, and Wikidata results here would be noise
        # at score 1.0 (it label-matches everything, so "Dune" returns a sand dune).
        if kind is not MediaKind.TECH:
            return []
        try:
            payload = cast(
                "dict[str, Any]",
                await get_json(
                    client,
                    _API,
                    params={
                        "action": "wbsearchentities",
                        "search": query,
                        "type": "item",
                        "language": "en",
                        "uselang": "en",
                        "format": "json",
                        "limit": str(limit),
                    },
                ),
            )
        except Exception as exc:  # a search miss must never break a lookup
            log.warning("wikidata.search_error", query=query, error=str(exc))
            return []
        return parse_candidates(payload, limit)
