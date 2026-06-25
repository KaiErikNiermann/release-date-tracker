"""JustWatch — the structured 'where + how much + since when' source for film/TV.

TMDB's ``/watch/providers`` is JustWatch data, but TMDB strips the two facts that
actually answer *"is it out, where, and how early"*: the per-offer **price** and the
**date the offer went live** (``availableFromTime``). Querying JustWatch's public
GraphQL directly hands those back — per country, per platform, per monetization type
(buy / rent / flatrate / cinema) — which is exactly the Tier-1 retailer ground truth
(Apple TV, Amazon, Google Play, Fandango…) aggregated into one keyless call.

The headline use is *earliest digital + where*: fan the same query across a basket of
early-window countries and take ``min(availableFromTime)`` over the buy/rent offers — the
soonest a title can be bought/rented anywhere, and which storefront, so a VPN target falls
out for free. The detailed offer set also **validates** platform availability against
ground truth instead of guessing a streaming home from the distributor.

Contract mirrors :mod:`release_tracker.sources.ddg`: best-effort, **never raises** (a miss
or outage yields ``None`` / an empty offer list), and ``to_dict`` omits empty fields so the
happy path stays lean.
"""

from __future__ import annotations

import asyncio
import json
import re
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
class JustWatchAvailability:
    """A title's offers across the scanned region basket, plus the derived earliest-VOD facts."""

    object_id: int
    title: str
    year: int | None
    offers: tuple[Offer, ...]
    earliest_vod: date | None
    earliest_vod_country: str | None
    earliest_vod_platform: str | None

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
        return {
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


def pick_node(
    edges: list[Any], title: str, year: int | None
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Choose the best title node: prefer an exact-ish title whose year matches (±1)."""
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
        title_score = (
            2 if cand_title == want else (1 if want in cand_title or cand_title in want else 0)
        )
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


def earliest_vod(offers: tuple[Offer, ...]) -> tuple[date | None, str | None, str | None]:
    """The soonest dated buy/rent offer: (date, country, platform) — the VPN-target answer."""
    dated = [o for o in offers if o.monetization in _VOD and o.available_from is not None]
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
) -> JustWatchAvailability | None:
    """Fan the offer query across a country basket; collect offers + the earliest VOD date.

    Best-effort: countries that miss or error simply contribute nothing. Returns ``None`` when
    no offer surfaces anywhere (the title isn't on any store yet) so the caller omits the field.
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
    earliest, ec, ep = earliest_vod(deduped)
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
