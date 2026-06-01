"""IGDB puller — games.

IGDB's ``release_dates`` carries platform, region and a *date category* that
encodes precision (exact / month / quarter / year / TBD). Auth is a Twitch
client-credentials token.

Setup: register a Twitch app, then use its client id/secret.
https://api-docs.igdb.com/#getting-started
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any, cast

import httpx

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
from release_tracker.sources.base import SourceResult, post_json, post_text

log = get_logger("igdb")

TOKEN_URL = "https://id.twitch.tv/oauth2/token"  # noqa: S105 - public OAuth endpoint
GAMES_URL = "https://api.igdb.com/v4/games"
RELEASE_DATES_URL = "https://api.igdb.com/v4/release_dates"

# IGDB date `category` -> precision. The unix timestamp still points at the
# start of the period for coarse precisions.
_CATEGORY_TO_PRECISION: dict[int, DatePrecision] = {
    0: DatePrecision.EXACT,  # YYYYMMMMDD
    1: DatePrecision.MONTH,  # YYYYMMMM
    2: DatePrecision.YEAR,  # YYYY
    3: DatePrecision.QUARTER,  # YYYYQ1
    4: DatePrecision.QUARTER,  # YYYYQ2
    5: DatePrecision.QUARTER,  # YYYYQ3
    6: DatePrecision.QUARTER,  # YYYYQ4
    7: DatePrecision.TBA,  # TBD
}

# IGDB region enum -> a region code we use elsewhere.
_REGION: dict[int, str] = {
    1: "EU",
    2: "US",  # north america
    3: "AU",
    4: "NZ",
    5: "JP",
    6: "CN",
    7: "AS",
    8: "WW",  # worldwide
    9: "WW",  # korea -> fall back; refine later
}


class IgdbSource:
    name = "igdb"

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_lock = asyncio.Lock()

    def supports(self, kind: MediaKind) -> bool:
        return kind is MediaKind.GAME

    async def _ensure_token(
        self, client: httpx.AsyncClient, settings: Settings
    ) -> tuple[str, str] | None:
        cid, secret = settings.twitch_client_id, settings.twitch_client_secret
        if not cid or not secret:
            log.warning("igdb.skip", reason="no TWITCH_CLIENT_ID/SECRET")
            return None
        # lock so concurrent game pulls fetch the app token exactly once
        async with self._token_lock:
            if self._token is None:
                payload = cast(
                    "dict[str, Any]",
                    # Twitch OAuth requires POST (params on the query string)
                    await post_json(
                        client,
                        TOKEN_URL,
                        params={
                            "client_id": cid,
                            "client_secret": secret,
                            "grant_type": "client_credentials",
                        },
                    ),
                )
                self._token = str(payload["access_token"])
        return cid, self._token

    async def pull(
        self, client: httpx.AsyncClient, entity: Entity, settings: Settings
    ) -> SourceResult:
        auth = await self._ensure_token(client, settings)
        if auth is None:
            return SourceResult()
        cid, token = auth
        headers = {"Client-ID": cid, "Authorization": f"Bearer {token}"}

        game_id = entity.external_ids.get("igdb")
        if game_id is None:
            game_id = await self._search(client, headers, entity.title)
        if game_id is None:
            return SourceResult()

        body = (
            "fields date,human,category,region,status,"
            "platform.name,game.name; "
            f"where game = {game_id}; limit 50;"
        )
        rows = cast(
            "list[dict[str, Any]]",
            await post_text(client, RELEASE_DATES_URL, content=body, headers=headers),
        )
        now = datetime.now(UTC)
        observations: list[ReleaseObservation] = []
        for row in rows:
            precision = _CATEGORY_TO_PRECISION.get(int(row.get("category", 7)), DatePrecision.TBA)
            rel = _ts_to_date(row.get("date"))
            if rel is None and precision is not DatePrecision.TBA:
                continue
            platform = row.get("platform")
            platform_name = (
                str(platform["name"]) if isinstance(platform, dict) and "name" in platform else None
            )
            slug = entity.external_ids.get("igdb_slug", str(game_id))
            observations.append(
                ReleaseObservation(
                    entity_id=entity.id,
                    channel=ReleaseChannel.PRIMARY,
                    region=_REGION.get(int(row.get("region", 8)), "WW"),
                    release_date=rel,
                    precision=precision,
                    certainty=Certainty.CONFIRMED,
                    source_tier=SourceTier.AGGREGATOR,
                    provider=self.name,
                    source_name=f"IGDB ({platform_name})" if platform_name else "IGDB",
                    source_url=f"https://www.igdb.com/games/{slug}",
                    source_quote=str(row.get("human")) if row.get("human") else None,
                    fetched_at=now,
                )
            )
        log.info("igdb.game", entity=entity.title, igdb_id=game_id, observations=len(observations))
        return SourceResult(observations=observations, external_ids={"igdb": str(game_id)})

    async def _search(
        self, client: httpx.AsyncClient, headers: dict[str, str], title: str
    ) -> str | None:
        body = f'search "{title}"; fields id,name,slug; limit 5;'
        rows = cast(
            "list[dict[str, Any]]",
            await post_text(client, GAMES_URL, content=body, headers=headers),
        )
        if not rows:
            log.warning("igdb.search.miss", title=title)
            return None
        return str(rows[0]["id"])


def _ts_to_date(value: object) -> date | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(int(value), tz=UTC).date()
