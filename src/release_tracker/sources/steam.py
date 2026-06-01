"""Steam puller — game release date + price per region.

``appdetails`` returns a free-text release date ("12 Mar, 2026", "Q1 2026",
"Coming Soon") plus ``price_overview`` in minor units for the requested country.
We resolve dates locally with ``dateparser`` (no LLM) and fall back to coarse
precision when only a quarter/year is given.

These are unofficial storefront endpoints but widely used and unauthenticated.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any, cast

import dateparser
import httpx

from release_tracker.config import Settings
from release_tracker.logging import get_logger
from release_tracker.models import (
    Certainty,
    DatePrecision,
    Entity,
    MediaKind,
    Money,
    ReleaseChannel,
    ReleaseObservation,
    SourceTier,
)
from release_tracker.sources.base import Candidate, SourceResult, get_json, pinned_id

log = get_logger("steam")

SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
DETAILS_URL = "https://store.steampowered.com/api/appdetails"

_QUARTER_RE = re.compile(r"\bQ([1-4])\s*'?(\d{4})\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"^\s*(\d{4})\s*$")
_MONTH_YEAR_RE = re.compile(r"^[A-Za-z]+\s+\d{4}$")


class SteamSource:
    name = "steam"

    def supports(self, kind: MediaKind) -> bool:
        return kind is MediaKind.GAME

    async def pull(
        self, client: httpx.AsyncClient, entity: Entity, settings: Settings
    ) -> SourceResult:
        appid, skip = pinned_id(entity.external_ids, "steam_appid")
        if skip:
            return SourceResult()
        if appid is None:
            appid = await self._search(client, entity.title, settings.regions[0])
        if appid is None:
            return SourceResult()

        now = datetime.now(UTC)
        observations: list[ReleaseObservation] = []
        # one details call per region to get localized price; release date is the
        # same string but we record it once (region-agnostic) plus per-region price.
        release_recorded = False
        for region in settings.regions:
            data = await self._details(client, appid, region)
            if data is None:
                continue
            url = f"https://store.steampowered.com/app/{appid}"

            if not release_recorded:
                rd = cast("dict[str, Any]", data.get("release_date", {}))
                parsed = _parse_release_date(str(rd.get("date", "")))
                coming_soon = bool(rd.get("coming_soon"))
                if parsed is not None:
                    rel, precision = parsed
                    observations.append(
                        ReleaseObservation(
                            entity_id=entity.id,
                            channel=ReleaseChannel.STEAM,
                            region="WW",
                            release_date=rel,
                            precision=precision,
                            certainty=Certainty.ESTIMATED if coming_soon else Certainty.CONFIRMED,
                            source_tier=SourceTier.FIRST_PARTY_STORE,
                            provider=self.name,
                            source_name="Steam",
                            source_url=url,
                            source_quote=str(rd.get("date")) or None,
                            fetched_at=now,
                        )
                    )
                    release_recorded = True

            price = cast("dict[str, Any] | None", data.get("price_overview"))
            if price:
                observations.append(
                    ReleaseObservation(
                        entity_id=entity.id,
                        channel=ReleaseChannel.STEAM,
                        region=region,
                        release_date=None,
                        precision=DatePrecision.TBA,
                        price=Money(
                            amount_minor=int(price["final"]),
                            currency=str(price["currency"]),
                        ),
                        certainty=Certainty.CONFIRMED,
                        source_tier=SourceTier.FIRST_PARTY_STORE,
                        provider=self.name,
                        source_name="Steam",
                        source_url=url,
                        fetched_at=now,
                    )
                )

        log.info("steam.game", entity=entity.title, appid=appid, observations=len(observations))
        return SourceResult(observations=observations, external_ids={"steam_appid": str(appid)})

    async def _search(self, client: httpx.AsyncClient, title: str, region: str) -> str | None:
        payload = cast(
            "dict[str, Any]",
            await get_json(
                client, SEARCH_URL, params={"term": title, "cc": region.lower(), "l": "en"}
            ),
        )
        items = cast("list[dict[str, Any]]", payload.get("items", []))
        if not items:
            log.warning("steam.search.miss", title=title)
            return None
        return str(items[0]["id"])

    # -- candidates (manual resolution) -----------------------------------
    async def search_candidates(
        self,
        client: httpx.AsyncClient,
        query: str,
        kind: MediaKind,
        settings: Settings,
        *,
        limit: int = 6,
    ) -> list[Candidate]:
        region = settings.regions[0] if settings.regions else "US"
        payload = cast(
            "dict[str, Any]",
            await get_json(
                client, SEARCH_URL, params={"term": query, "cc": region.lower(), "l": "en"}
            ),
        )
        out: list[Candidate] = []
        for item in cast("list[dict[str, Any]]", payload.get("items", []))[:limit]:
            out.append(
                Candidate(
                    source=self.name,
                    id_key="steam_appid",
                    canonical_id=str(item["id"]),
                    title=str(item.get("name", "")),
                    year=None,  # storesearch omits release year
                    extra=str(item.get("type", "")),
                    url=f"https://store.steampowered.com/app/{item['id']}",
                )
            )
        return out

    async def _details(
        self, client: httpx.AsyncClient, appid: str, region: str
    ) -> dict[str, Any] | None:
        payload = cast(
            "dict[str, Any]",
            await get_json(
                client,
                DETAILS_URL,
                params={
                    "appids": appid,
                    "cc": region.lower(),
                    "l": "en",
                    "filters": "price_overview,release_date,basic",
                },
            ),
        )
        entry = cast("dict[str, Any] | None", payload.get(str(appid)))
        if not entry or not entry.get("success"):
            return None
        return cast("dict[str, Any]", entry.get("data", {}))


def _parse_release_date(text: str) -> tuple[date | None, DatePrecision] | None:
    """Resolve Steam's free-text date string to (date, precision).

    Handles exact dates, "Month YYYY", "Qn YYYY", "YYYY" and "Coming Soon"/"TBA".
    """
    text = text.strip()
    if not text or text.lower() in {"coming soon", "to be announced", "tba"}:
        return None

    if (m := _QUARTER_RE.search(text)) is not None:
        quarter, year = int(m.group(1)), int(m.group(2))
        month = (quarter - 1) * 3 + 1
        return date(year, month, 1), DatePrecision.QUARTER

    if (m := _YEAR_RE.match(text)) is not None:
        return date(int(m.group(1)), 1, 1), DatePrecision.YEAR

    parsed = dateparser.parse(text, settings={"PREFER_DAY_OF_MONTH": "first"})
    if parsed is None:
        return None
    precision = DatePrecision.MONTH if _MONTH_YEAR_RE.match(text) else DatePrecision.EXACT
    return parsed.date(), precision
