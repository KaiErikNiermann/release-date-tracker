"""JustWatch — the structured 'where + how much + since when' source for film/TV.

TMDB's ``/watch/providers`` is JustWatch data, but TMDB strips the two facts that
actually answer *"is it out, where, and how early"*: the per-offer **price** and the
**date the offer went live** (``availableFromTime``). Querying JustWatch's public
GraphQL directly hands those back — per country, per platform, per monetization type
(buy / rent / flatrate / cinema) — which is exactly the Tier-1 retailer ground truth
(Apple TV, Amazon, Google Play, Fandango…) aggregated into one keyless call.

The headline use is *earliest digital + where*: fan the same query across a basket of
early-window countries and take the earliest ``availableFromTime`` over the buy/rent
offers — the soonest a title can be bought/rented anywhere, and which storefront, so a VPN
target falls out for free. That date is a *listing* date, so the pick is floored by each
market's cinema day (see :class:`TheatricalFloor`) or a pre-order would win it. The detailed
offer set also **validates** platform availability against ground truth instead of guessing a
streaming home from the distributor.

Contract mirrors :mod:`release_tracker.sources.ddg`: best-effort, **never raises** (a miss
or outage yields ``None`` / an empty offer list), and ``to_dict`` omits empty fields so the
happy path stays lean.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, cast

import httpx

from release_tracker.logging import get_logger
from release_tracker.models import MediaKind
from release_tracker.sources.base import post_text

log = get_logger("justwatch")

_ENDPOINT = "https://apis.justwatch.com/graphql"
# cap on concurrent country queries — see availability() (keeps the price field populated).
_MAX_CONCURRENCY = 4
# WEB is the canonical superset storefront list; per-OS platforms only ever subset it.
_GQL = """
query RdtOffers($q: String!, $country: Country!, $language: Language!,
                $priceLang: Language!, $types: [ObjectType!]) {
  popularTitles(country: $country, first: 4, filter: {searchQuery: $q, objectTypes: $types}) {
    edges { node {
      id
      objectId
      objectType
      content(country: $country, language: $language) { title originalReleaseYear }
      offers(country: $country, platform: WEB) {
        monetizationType
        presentationType
        retailPrice(language: $priceLang)
        currency
        availableFromTime
        package { clearName }
      }
    }}
  }
}
"""

# The season-scoped variant. `Show.seasons` takes **no** country argument (passing one is a
# validation error) — the country lives on each season's `content`/`offers`, so one request per
# country still returns every season. Selected as an inline fragment on the *search* result, so
# resolving the show and reading its seasons stays one document and N requests, not N+1.
#
# `upcomingReleases` is why a future season is worth asking about at all: it has no offers yet,
# and the platform's own announced date is sitting right there.
_GQL_SEASONS = """
query RdtSeasonOffers($q: String!, $country: Country!, $language: Language!,
                      $priceLang: Language!) {
  popularTitles(country: $country, first: 4, filter: {searchQuery: $q, objectTypes: [SHOW]}) {
    edges { node {
      id
      objectId
      objectType
      content(country: $country, language: $language) { title originalReleaseYear }
      ... on Show {
        totalSeasonCount
        seasons {
          id
          objectId
          content(country: $country, language: $language) {
            seasonNumber
            title
            fullPath
            upcomingReleases { releaseDate releaseType label package { clearName } }
          }
          offers(country: $country, platform: WEB) {
            monetizationType
            presentationType
            retailPrice(language: $priceLang)
            currency
            availableFromTime
            package { clearName }
          }
        }
      }
    }}
  }
}
"""

# JustWatch monetization types we treat as "buy/rent it digitally at home now" — the EST /
# PVOD window that defines the digital release date (flatrate = later subscription drop).
_VOD = frozenset({"buy", "rent"})
_OBJECT_TYPES: dict[MediaKind, list[str]] = {
    MediaKind.MOVIE: ["MOVIE"],
    MediaKind.TV: ["SHOW"],
}
# retailPrice is localized: querying a store in the wrong language yields a null price, so map
# each scanned country to its storefront language (falls back to the default for anything else).
_COUNTRY_LANG: dict[str, str] = {
    "DE": "de",
    "FR": "fr",
    "IT": "it",
    "ES": "es",
    "JP": "ja",
    "BR": "pt",
    "NL": "nl",
}


@dataclass(frozen=True, slots=True)
class TheatricalFloor:
    """The cinema dates a store offer has to postdate before it counts as a real VOD date.

    ``by_country`` is each market's own cinema day; ``earliest`` is the earliest anywhere and
    stands in for a country with no known theatrical date of its own (the weaker, keep-more-data
    bound, so an unmapped market never loses a legitimate offer).
    """

    by_country: Mapping[str, date]
    earliest: date | None = None

    def for_country(self, country: str) -> date | None:
        """The cinema day an offer in ``country`` must postdate, or None when nothing is known."""
        return self.by_country.get(country.upper(), self.earliest)


@dataclass(frozen=True, slots=True)
class Offer:
    """One JustWatch offer: a (country, platform, monetization) availability with price/date."""

    country: str  # ISO-2 the offer was found in
    monetization: str  # buy | rent | flatrate | cinema | free | ads
    platform: str  # storefront clear name, e.g. "Apple TV Store"
    presentation: str | None  # 4k | hd | sd | None
    price: float | None
    currency: str | None
    available_from: date | None  # date the offer went live (the VOD release date for buy/rent)

    def to_dict(self) -> dict[str, object]:
        return {
            "country": self.country,
            "monetization": self.monetization,
            "platform": self.platform,
            "presentation": self.presentation,
            "price": self.price,
            "currency": self.currency,
            "available_from": self.available_from.isoformat() if self.available_from else None,
        }


@dataclass(frozen=True, slots=True)
class UpcomingRelease:
    """A dated, platform-attributed availability that has not gone live yet.

    JustWatch's ``upcomingReleases``. The whole reason a *future* season is worth querying: it
    has no offers to read a date off, but the platform has already published one.
    """

    country: str
    when: date
    release_type: str  # digital | physical | ... (lowercased)
    label: str  # JustWatch's own confidence marker; "DATE" means an actual day
    platform: str | None

    @property
    def firm(self) -> bool:
        """True when JustWatch calls this a real date rather than a window or a placeholder."""
        return self.label.upper() == "DATE"

    def to_dict(self) -> dict[str, object]:
        return {
            "country": self.country,
            "when": self.when.isoformat(),
            "release_type": self.release_type,
            "label": self.label,
            "platform": self.platform,
        }


@dataclass(frozen=True, slots=True)
class JustWatchAvailability:
    """A title's offers across the scanned region basket, plus the derived earliest-VOD facts.

    ``season`` set means the offers are scoped to that season of a show rather than to the
    whole thing, which is a materially different answer: Yellowjackets carries Netflix on
    seasons 1-2 and not on 3, so the show-level reading is wrong for either one.
    """

    object_id: int
    title: str
    year: int | None
    offers: tuple[Offer, ...]
    earliest_vod: date | None
    earliest_vod_country: str | None
    earliest_vod_platform: str | None
    season: int | None = None
    upcoming: tuple[UpcomingRelease, ...] = ()

    @property
    def announced(self) -> UpcomingRelease | None:
        """The soonest firm announced release — the answer for a season with no offers yet."""
        firm = sorted((u for u in self.upcoming if u.firm), key=lambda u: u.when)
        return firm[0] if firm else None

    @property
    def countries(self) -> tuple[str, ...]:
        """The distinct regions an offer exists in (sorted)."""
        return tuple(sorted({o.country for o in self.offers}))

    @property
    def streaming_platforms(self) -> tuple[str, ...]:
        """Distinct flatrate (subscription) homes — ground-truth streaming, no prediction needed."""
        seen: dict[str, None] = {}
        for o in self.offers:
            if o.monetization == "flatrate":
                seen.setdefault(o.platform, None)
        return tuple(seen)

    def to_dict(self) -> dict[str, object]:
        extra: dict[str, object] = {}
        if self.season is not None:
            extra["season"] = self.season
        if self.upcoming:
            extra["upcoming"] = [u.to_dict() for u in self.upcoming]
        return {
            **extra,
            "object_id": self.object_id,
            "title": self.title,
            "year": self.year,
            "earliest_vod": self.earliest_vod.isoformat() if self.earliest_vod else None,
            "earliest_vod_country": self.earliest_vod_country,
            "earliest_vod_platform": self.earliest_vod_platform,
            "countries": list(self.countries),
            "streaming_platforms": list(self.streaming_platforms),
            "offers": [o.to_dict() for o in self.offers],
        }


def parse_price(value: object) -> float | None:
    """Decode JustWatch's ``retailPrice`` into a float. It's a *localized string* with the
    currency glyph baked in (e.g. ``$3.99``, ``3,99 EUR``) — strip it to a
    number, handling comma-decimal locales. The ISO currency is a separate field.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        return None
    num = re.sub(r"[^0-9.,]", "", value)
    if not num:
        return None
    # Decimal separator = the last '.'/',' iff 1-2 digits follow it (a cents group); a
    # separator with 3 trailing digits (or none) is thousands grouping. Locale-agnostic:
    # "$3.99", "3,99", "1.299,00", "1,500" all decode correctly.
    sep_at = max(num.rfind(","), num.rfind("."))
    if sep_at != -1 and len(num) - sep_at - 1 in (1, 2):
        sep = num[sep_at]
        num = num.replace("." if sep == "," else ",", "").replace(sep, ".")
    else:
        num = num.replace(",", "").replace(".", "")
    try:
        return float(num)
    except ValueError:
        return None


def parse_from_time(value: object) -> date | None:
    """Decode JustWatch's ``availableFromTime`` ISO datetime (``…Z``) to a date, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def parse_offers(node: dict[str, Any], country: str) -> list[Offer]:
    """Pure: flatten one title node's raw offers into typed :class:`Offer` rows for a country."""
    out: list[Offer] = []
    for raw in cast("list[Any]", node.get("offers") or []):
        if not isinstance(raw, dict):
            continue
        o = cast("dict[str, Any]", raw)
        pkg = o.get("package")
        platform = str(pkg.get("clearName", "")).strip() if isinstance(pkg, dict) else ""
        if not platform:
            continue
        pres = o.get("presentationType")
        out.append(
            Offer(
                country=country,
                monetization=str(o.get("monetizationType", "")).strip().lower(),
                platform=platform,
                presentation=str(pres).strip().lower().lstrip("_") if pres else None,
                price=parse_price(o.get("retailPrice")),
                currency=(str(o.get("currency")).strip() or None) if o.get("currency") else None,
                available_from=parse_from_time(o.get("availableFromTime")),
            )
        )
    return out


def parse_upcoming(raw: object, country: str) -> list[UpcomingRelease]:
    """Pure: flatten a season's ``upcomingReleases`` into typed rows for a country."""
    out: list[UpcomingRelease] = []
    for item in cast("list[Any]", raw or []):
        if not isinstance(item, dict):
            continue
        entry = cast("dict[str, Any]", item)
        when = parse_from_time(entry.get("releaseDate"))
        if when is None:
            continue  # an undated "upcoming" says nothing we can act on
        pkg = entry.get("package")
        platform = str(pkg.get("clearName", "")).strip() or None if isinstance(pkg, dict) else None
        out.append(
            UpcomingRelease(
                country=country.upper(),
                when=when,
                release_type=str(entry.get("releaseType") or "").strip().lower(),
                label=str(entry.get("label") or "").strip(),
                platform=platform,
            )
        )
    return out


def parse_season(
    node: dict[str, Any], country: str
) -> tuple[int, list[Offer], list[UpcomingRelease]] | None:
    """Pure: one season node -> its number, offers and announced releases. None if unnumbered.

    A season we cannot number is a season we cannot attribute, and guessing from list order
    would silently shift every offer by one wherever JustWatch omits a season.
    """
    raw_content = node.get("content")
    if not isinstance(raw_content, dict):
        return None
    content = cast("dict[str, Any]", raw_content)
    number = content.get("seasonNumber")
    if not isinstance(number, int):
        return None
    return (
        number,
        parse_offers(node, country.upper()),
        parse_upcoming(content.get("upcomingReleases"), country),
    )


def title_match_score(want: str, cand: str) -> int:
    """Title-overlap strength: ``2`` exact, ``1`` a prefix abbreviation/subtitle, ``0`` unrelated.

    A partial counts only when the shorter title is a *word-aligned prefix* of the longer one — an
    abbreviation or subtitle ("Wicked" ↔ "Wicked: Part I", "Nosferatu" ↔ "Nosferatu the Vampyre").
    A shared *trailing* word does not match, so "Ghosts" never claims "Anything but Ghosts".
    """
    want, cand = want.casefold().strip(), cand.casefold().strip()
    if not want or not cand:
        return 0
    if want == cand:
        return 2
    short, long = sorted((want, cand), key=len)
    boundary = len(short) == len(long) or not long[len(short)].isalnum()
    return 1 if long.startswith(short) and boundary else 0


def pick_node(
    edges: list[Any], title: str, year: int | None
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Choose the best title node: prefer an exact-ish title whose year matches (±1).

    A node with *no* name overlap is never selected — JustWatch's fuzzy ``popularTitles`` can return
    an unrelated popular title (e.g. "Absolutely Anything" for "Anything but Ghosts"), and when the
    film's year is unknown the year filter can't reject it, so the title floor is the only guard.
    """
    best: tuple[tuple[int, int], dict[str, Any], dict[str, Any]] | None = None
    want = title.casefold().strip()
    for raw_edge in edges:
        if not isinstance(raw_edge, dict):
            continue
        raw_node = cast("dict[str, Any]", raw_edge).get("node")
        if not isinstance(raw_node, dict):
            continue
        node = cast("dict[str, Any]", raw_node)
        raw_content = node.get("content")
        if not isinstance(raw_content, dict):
            continue
        content = cast("dict[str, Any]", raw_content)
        cand_title = str(content.get("title", "")).casefold().strip()
        cand_year: object = content.get("originalReleaseYear")
        title_score = title_match_score(want, cand_title)
        if title_score == 0:  # no real name overlap → wrong title, never select it
            continue
        year_ok = year is None or not isinstance(cand_year, int) or abs(cand_year - year) <= 1
        if not year_ok:
            continue
        key = (title_score, 1 if isinstance(cand_year, int) else 0)
        if best is None or key > best[0]:
            best = (key, node, content)
    if best is None:
        return None
    return best[1], best[2]


async def _query_country(
    client: httpx.AsyncClient,
    title: str,
    country: str,
    kind: MediaKind,
    *,
    year: int | None,
    language: str,
) -> tuple[int, str, int | None, list[Offer]] | None:
    """One country's offers for a title. Never raises — a miss/outage yields None."""
    payload = json.dumps(
        {
            "query": _GQL,
            "variables": {
                "q": title,
                "country": country.upper(),
                "language": language,  # stable (English) so titles match our query
                "priceLang": _COUNTRY_LANG.get(country.upper(), language),  # localized → real price
                "types": _OBJECT_TYPES.get(kind),
            },
        }
    )
    try:
        data = await post_text(
            client, _ENDPOINT, content=payload, headers={"Content-Type": "application/json"}
        )
    except Exception as exc:  # a keyless best-effort source must never break a lookup
        log.warning("justwatch.fetch_error", country=country, title=title, error=str(exc))
        return None
    if not isinstance(data, dict) or data.get("errors"):
        log.warning("justwatch.graphql_error", country=country, title=title)
        return None
    block = cast("dict[str, Any]", data)
    data_node = cast("dict[str, Any]", block.get("data") or {})
    titles = cast("dict[str, Any]", data_node.get("popularTitles") or {})
    edges = cast("list[Any]", titles.get("edges") or [])
    picked = pick_node(edges, title, year)
    if picked is None:
        return None
    node, content = picked
    obj_id = node.get("objectId")
    if not isinstance(obj_id, int):
        return None
    cand_year: object = content.get("originalReleaseYear")
    return (
        obj_id,
        str(content.get("title", title)),
        cand_year if isinstance(cand_year, int) else None,
        parse_offers(node, country.upper()),
    )


async def _query_country_season(
    client: httpx.AsyncClient,
    title: str,
    country: str,
    *,
    season: int,
    year: int | None,
    language: str,
) -> tuple[int, str, int | None, list[Offer], list[UpcomingRelease]] | None:
    """One country's offers for *one season* of a show. Never raises — a miss yields None."""
    payload = json.dumps(
        {
            "query": _GQL_SEASONS,
            "variables": {
                "q": title,
                "country": country.upper(),
                "language": language,
                "priceLang": _COUNTRY_LANG.get(country.upper(), language),
            },
        }
    )
    try:
        data = await post_text(
            client, _ENDPOINT, content=payload, headers={"Content-Type": "application/json"}
        )
    except Exception as exc:  # a keyless best-effort source must never break a lookup
        log.warning("justwatch.season_fetch_error", country=country, title=title, error=str(exc))
        return None
    if not isinstance(data, dict) or data.get("errors"):
        log.warning("justwatch.season_graphql_error", country=country, title=title)
        return None
    block = cast("dict[str, Any]", data)
    data_node = cast("dict[str, Any]", block.get("data") or {})
    titles = cast("dict[str, Any]", data_node.get("popularTitles") or {})
    picked = pick_node(cast("list[Any]", titles.get("edges") or []), title, year)
    if picked is None:
        return None
    node, content = picked
    obj_id = node.get("objectId")
    if not isinstance(obj_id, int):
        return None
    for raw in cast("list[Any]", node.get("seasons") or []):
        if not isinstance(raw, dict):
            continue
        parsed = parse_season(cast("dict[str, Any]", raw), country)
        if parsed is None or parsed[0] != season:
            continue
        cand_year: object = content.get("originalReleaseYear")
        return (
            obj_id,
            str(content.get("title", title)),
            cand_year if isinstance(cand_year, int) else None,
            parsed[1],
            parsed[2],
        )
    # The show matched but JustWatch does not carry this season. Honest miss, not an error:
    # their numbering can lag ours, and reporting the show's offers instead is the exact
    # wrong-season answer this path exists to avoid.
    log.info("justwatch.season_absent", title=title, country=country, season=season)
    return None


async def season_availability(
    client: httpx.AsyncClient,
    title: str,
    *,
    season: int,
    countries: tuple[str, ...],
    year: int | None = None,
    language: str = "en",
) -> JustWatchAvailability | None:
    """One season's offers + announced releases across the basket. Never raises.

    Season-scoped *at the source*, which is the whole point: a show-level read reports the
    series' earliest VOD (usually S1's) and its full set of streaming homes, and neither
    answers "where and when can I watch *this* season".

    No :class:`TheatricalFloor` is taken. The pre-order artifact it guards against is a
    cinema phenomenon — a storefront standing a listing up on the local release day — and a
    TV season has no cinema day for one to be dated to.
    """
    if not countries:
        return None
    sem = asyncio.Semaphore(_MAX_CONCURRENCY)

    async def _bounded(
        country: str,
    ) -> tuple[int, str, int | None, list[Offer], list[UpcomingRelease]] | None:
        async with sem:
            return await _query_country_season(
                client, title, country, season=season, year=year, language=language
            )

    results = await asyncio.gather(*(_bounded(c) for c in countries), return_exceptions=True)
    offers: list[Offer] = []
    upcoming: list[UpcomingRelease] = []
    obj_id: int | None = None
    matched_title, matched_year = title, year
    for res in results:
        if not isinstance(res, tuple):  # None, or an exception that slipped through gather
            continue
        cid, ctitle, cyear, country_offers, country_upcoming = res
        if country_offers or country_upcoming:
            obj_id = obj_id or cid
            matched_title, matched_year = ctitle, cyear
            offers.extend(country_offers)
            upcoming.extend(country_upcoming)
    # An announced date with no offers yet is the *expected* shape for a future season, so a
    # bare offer check would throw away the only thing worth having.
    if obj_id is None or not (offers or upcoming):
        return None
    deduped = tuple(dedupe(offers))
    earliest, ec, ep = earliest_vod(deduped)
    return JustWatchAvailability(
        object_id=obj_id,
        title=matched_title,
        year=matched_year,
        offers=deduped,
        earliest_vod=earliest,
        earliest_vod_country=ec,
        earliest_vod_platform=ep,
        season=season,
        upcoming=tuple(dedupe_upcoming(upcoming)),
    )


def dedupe_upcoming(rows: list[UpcomingRelease]) -> list[UpcomingRelease]:
    """Collapse the same announcement seen in several countries' payloads."""
    best: dict[tuple[str, str, str, str | None], UpcomingRelease] = {}
    for u in rows:
        best.setdefault((u.country, u.release_type, u.when.isoformat(), u.platform), u)
    return sorted(best.values(), key=lambda u: (u.when, u.country, u.platform or ""))


def is_listing_artifact(offer: Offer, floor: TheatricalFloor) -> bool:
    """True when an offer's date is its store *listing* going up, not the buy/rent going live.

    ``availableFromTime`` dates the listing, and a storefront stands the listing up as a pre-order
    on the film's local cinema day — Apple reports that day verbatim (Toy Story 5: ES 2026-06-17,
    AU 2026-06-18, US 2026-06-19, each its market's theatrical date exactly), a full two months
    before the real VOD window its Amazon offers show.
    """
    cinema = floor.for_country(offer.country)
    when = offer.available_from
    return cinema is not None and when is not None and when <= cinema


def earliest_vod(
    offers: tuple[Offer, ...], floor: TheatricalFloor | None = None
) -> tuple[date | None, str | None, str | None]:
    """The soonest dated buy/rent offer: (date, country, platform) — the VPN-target answer.

    A bare ``min`` here reports the *cinema* release as the digital one, because every storefront
    that pre-orders from theatrical day contributes an offer dated to it. Passing ``floor`` drops
    those (see :func:`is_listing_artifact`); the cost is a genuine day-and-date VOD release, which
    is indistinguishable from the artifact on this field alone and which TMDB's Digital type still
    carries.
    """
    dated = [o for o in offers if o.monetization in _VOD and o.available_from is not None]
    if floor is not None:
        dated = [o for o in dated if not is_listing_artifact(o, floor)]
    if not dated:
        return None, None, None
    best = min(dated, key=lambda o: cast("date", o.available_from))
    return best.available_from, best.country, best.platform


async def availability(
    client: httpx.AsyncClient,
    title: str,
    kind: MediaKind,
    *,
    countries: tuple[str, ...],
    year: int | None = None,
    language: str = "en",
    floor: TheatricalFloor | None = None,
) -> JustWatchAvailability | None:
    """Fan the offer query across a country basket; collect offers + the earliest VOD date.

    Best-effort: countries that miss or error simply contribute nothing. Returns ``None`` when
    no offer surfaces anywhere (the title isn't on any store yet) so the caller omits the field.

    ``floor`` carries the per-market cinema dates; without it the earliest-VOD pick is a bare
    ``min`` and a pre-order listing dated to theatrical day will win it. Pass one whenever the
    caller holds theatrical observations.
    """
    if kind not in _OBJECT_TYPES or not countries:
        return None

    # JustWatch nulls the (localized) price field under heavy concurrent load while still
    # returning offer presence + dates — a small semaphore keeps prices populated and is a
    # politer rate to an unofficial endpoint, at a trivial latency cost over the basket.
    sem = asyncio.Semaphore(_MAX_CONCURRENCY)

    async def _bounded(country: str) -> tuple[int, str, int | None, list[Offer]] | None:
        async with sem:
            return await _query_country(client, title, country, kind, year=year, language=language)

    results = await asyncio.gather(*(_bounded(c) for c in countries), return_exceptions=True)
    offers: list[Offer] = []
    obj_id: int | None = None
    matched_title = title
    matched_year = year
    for res in results:
        if not isinstance(res, tuple):  # None or a raised exception slipped through gather
            continue
        cid, ctitle, cyear, country_offers = res
        if country_offers:
            obj_id = obj_id or cid
            matched_title, matched_year = ctitle, cyear
            offers.extend(country_offers)
    if obj_id is None or not offers:
        return None
    deduped = tuple(dedupe(offers))
    earliest, ec, ep = earliest_vod(deduped, floor)
    return JustWatchAvailability(
        object_id=obj_id,
        title=matched_title,
        year=matched_year,
        offers=deduped,
        earliest_vod=earliest,
        earliest_vod_country=ec,
        earliest_vod_platform=ep,
    )


def dedupe(offers: list[Offer]) -> list[Offer]:
    """Collapse duplicate (country, platform, monetization) offers, keeping the dated/cheapest."""
    best: dict[tuple[str, str, str], Offer] = {}
    for o in offers:
        key = (o.country, o.platform, o.monetization)
        cur = best.get(key)
        if cur is None or _offer_rank(o) < _offer_rank(cur):
            best[key] = o
    return sorted(best.values(), key=lambda o: (o.country, o.monetization, o.platform))


def _offer_rank(o: Offer) -> tuple[int, float]:
    """Lower is better: prefer a dated offer, then the cheaper price."""
    return (0 if o.available_from else 1, o.price if o.price is not None else 1e9)
