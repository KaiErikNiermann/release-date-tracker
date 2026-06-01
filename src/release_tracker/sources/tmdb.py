"""TMDB puller — movies & TV.

The headline win: ``/movie/{id}/release_dates`` returns a per-country array where
each entry carries a release *type*, including a dedicated **Digital** type that
most movie DBs never expose. That is exactly the (channel, region, date) shape we
want, for free, the moment it is announced.

Free API key: https://www.themoviedb.org/settings/api
"""

from __future__ import annotations

import re
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
from release_tracker.sources.base import SourceResult, get_json

log = get_logger("tmdb")

BASE = "https://api.themoviedb.org/3"

# TMDB release_dates `type` integer -> our channel.
_TYPE_TO_CHANNEL: dict[int, ReleaseChannel] = {
    1: ReleaseChannel.PREMIERE,
    2: ReleaseChannel.THEATRICAL_LIMITED,
    3: ReleaseChannel.THEATRICAL,
    4: ReleaseChannel.DIGITAL,
    5: ReleaseChannel.PHYSICAL,
    6: ReleaseChannel.TV_BROADCAST,
}


class TmdbSource:
    name = "tmdb"

    def supports(self, kind: MediaKind) -> bool:
        return kind in (MediaKind.MOVIE, MediaKind.TV, MediaKind.ANIME)

    async def pull(
        self, client: httpx.AsyncClient, entity: Entity, settings: Settings
    ) -> SourceResult:
        if not settings.tmdb_api_key:
            log.warning("tmdb.skip", reason="no TMDB_API_KEY", entity=entity.title)
            return SourceResult()
        key = settings.tmdb_api_key
        if entity.kind is MediaKind.MOVIE:
            return await self._pull_movie(client, entity, key)
        return await self._pull_tv(client, entity, key)

    # -- movies ------------------------------------------------------------
    async def _pull_movie(
        self, client: httpx.AsyncClient, entity: Entity, key: str
    ) -> SourceResult:
        tmdb_id = entity.external_ids.get("tmdb")
        if tmdb_id is None:
            tmdb_id = await self._search(client, key, entity.title, "movie")
        if tmdb_id is None:
            return SourceResult()

        payload = cast(
            "dict[str, Any]",
            await get_json(
                client, f"{BASE}/movie/{tmdb_id}/release_dates", params={"api_key": key}
            ),
        )
        now = datetime.now(UTC)
        observations: list[ReleaseObservation] = []
        for block in cast("list[dict[str, Any]]", payload.get("results", [])):
            region = str(block.get("iso_3166_1", "WW"))
            for rd in cast("list[dict[str, Any]]", block.get("release_dates", [])):
                channel = _TYPE_TO_CHANNEL.get(int(rd.get("type", 0)))
                if channel is None:
                    continue
                rel = _parse_tmdb_date(rd.get("release_date"))
                if rel is None:
                    continue
                observations.append(
                    ReleaseObservation(
                        entity_id=entity.id,
                        channel=channel,
                        region=region,
                        release_date=rel,
                        precision=DatePrecision.EXACT,
                        certainty=Certainty.CONFIRMED,
                        source_tier=SourceTier.AGGREGATOR,
                        provider=self.name,
                        source_name="TMDB",
                        source_url=f"https://www.themoviedb.org/movie/{tmdb_id}",
                        source_quote=(str(rd.get("note")) or None) if rd.get("note") else None,
                        fetched_at=now,
                    )
                )
        log.info("tmdb.movie", entity=entity.title, tmdb_id=tmdb_id, observations=len(observations))
        return SourceResult(observations=observations, external_ids={"tmdb": str(tmdb_id)})

    # -- tv ----------------------------------------------------------------
    async def _pull_tv(self, client: httpx.AsyncClient, entity: Entity, key: str) -> SourceResult:
        # Watchlist titles encode the season ("The Boys: Season 5"), but TMDB
        # search only matches the *show*. Split the show name from the season so
        # we can search the show and then resolve that specific season's date.
        show_title, season_no = _split_season(entity.title)
        tmdb_id = entity.external_ids.get("tmdb")
        if tmdb_id is None:
            tmdb_id = await self._search(client, key, show_title, "tv")
        if tmdb_id is None:
            return SourceResult()

        now = datetime.now(UTC)
        observations: list[ReleaseObservation] = []
        url = f"https://www.themoviedb.org/tv/{tmdb_id}"
        air: date | None = None

        if season_no is not None:
            season = cast(
                "dict[str, Any]",
                await get_json(
                    client, f"{BASE}/tv/{tmdb_id}/season/{season_no}", params={"api_key": key}
                ),
            )
            air = _parse_tmdb_date(season.get("air_date"))

        if air is None:
            # whole-show fallback: next episode to air, else first air date
            detail = cast(
                "dict[str, Any]",
                await get_json(client, f"{BASE}/tv/{tmdb_id}", params={"api_key": key}),
            )
            nxt = detail.get("next_episode_to_air")
            air = _parse_tmdb_date(
                nxt.get("air_date") if isinstance(nxt, dict) else detail.get("first_air_date")
            )

        if air is not None:
            observations.append(
                ReleaseObservation(
                    entity_id=entity.id,
                    channel=ReleaseChannel.TV_BROADCAST,
                    region="WW",
                    release_date=air,
                    precision=DatePrecision.EXACT,
                    certainty=Certainty.CONFIRMED,
                    source_tier=SourceTier.AGGREGATOR,
                    provider=self.name,
                    source_name="TMDB",
                    source_url=url,
                    fetched_at=now,
                )
            )

        log.info(
            "tmdb.tv",
            entity=entity.title,
            tmdb_id=tmdb_id,
            season=season_no,
            observations=len(observations),
        )
        return SourceResult(observations=observations, external_ids={"tmdb": str(tmdb_id)})

    # -- search ------------------------------------------------------------
    async def _search(
        self, client: httpx.AsyncClient, key: str, title: str, media: str
    ) -> str | None:
        payload = cast(
            "dict[str, Any]",
            await get_json(
                client,
                f"{BASE}/search/{media}",
                params={"api_key": key, "query": title, "include_adult": "false"},
            ),
        )
        results = cast("list[dict[str, Any]]", payload.get("results", []))
        if not results:
            log.warning("tmdb.search.miss", title=title, media=media)
            return None
        return str(results[0]["id"])


_SEASON_RE = re.compile(r"^(?P<show>.+?)\s*[:\-]\s*Season\s+(?P<n>\d+)", re.IGNORECASE)
# trailing qualifiers that aren't part of the show's TMDB title
_QUALIFIER_RE = re.compile(
    r"\s*[:\-]\s*(Part\s+[\w\d]+|Finale|Final\s+Season|Vol(?:ume)?\.?\s*\d+).*$",
    re.IGNORECASE,
)


def _split_season(title: str) -> tuple[str, int | None]:
    """('The Boys: Season 5') -> ('The Boys', 5); strip Part/Finale qualifiers too."""
    if (m := _SEASON_RE.match(title)) is not None:
        return m.group("show").strip(), int(m.group("n"))
    return _QUALIFIER_RE.sub("", title).strip(), None


def _parse_tmdb_date(value: object) -> date | None:
    if not value or not isinstance(value, str):
        return None
    # TMDB dates are ISO, often full datetimes: "2026-03-12T00:00:00.000Z"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
