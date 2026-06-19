"""TMDB puller — movies & TV.

The headline win: ``/movie/{id}/release_dates`` returns a per-country array where
each entry carries a release *type*, including a dedicated **Digital** type that
most movie DBs never expose. That is exactly the (channel, region, date) shape we
want, for free, the moment it is announced.

Free API key: https://www.themoviedb.org/settings/api
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, cast

import httpx

from release_tracker.config import Settings
from release_tracker.logging import get_logger
from release_tracker.models import (
    Certainty,
    CreditRole,
    DatePrecision,
    Entity,
    MediaKind,
    NodeKind,
    ReleaseChannel,
    ReleaseObservation,
    SourceTier,
)
from release_tracker.sources.base import (
    Candidate,
    Credit,
    MediaGraph,
    SourceResult,
    get_json,
    pinned_id,
)
from release_tracker.titles import split_season

log = get_logger("tmdb")

BASE = "https://api.themoviedb.org/3"


@dataclass(slots=True, frozen=True)
class MovieMeta:
    """Extra movie detail used for speculation (distributor + fallback date)."""

    studios: tuple[str, ...]  # all production companies, in TMDB order
    primary_date: date | None
    status: str | None
    title: str | None


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
        return kind in (MediaKind.MOVIE, MediaKind.TV)

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
        tmdb_id, skip = pinned_id(entity.external_ids, "tmdb")
        if skip:
            return SourceResult()
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
        show_title, season_no = split_season(entity.title)
        tmdb_id, skip = pinned_id(entity.external_ids, "tmdb")
        if skip:
            return SourceResult()
        if tmdb_id is None:
            tmdb_id = await self._search(client, key, show_title, "tv")
        if tmdb_id is None:
            return SourceResult()

        now = datetime.now(UTC)
        observations: list[ReleaseObservation] = []
        url = f"https://www.themoviedb.org/tv/{tmdb_id}"
        air: date | None = None

        if season_no is not None:
            # a specific season: use ITS air_date only. A future season with no date
            # yet is TBA — do NOT fall back to the show's first_air_date / next episode,
            # those belong to a *different* (already-aired) season and would stamp the
            # unaired season with a wrong, ancient "confirmed" date (e.g. S3 -> S1's date).
            season = cast(
                "dict[str, Any]",
                await get_json(
                    client, f"{BASE}/tv/{tmdb_id}/season/{season_no}", params={"api_key": key}
                ),
            )
            air = _parse_tmdb_date(season.get("air_date"))
        else:
            # whole show (no season pinned): next episode to air, else first air date
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
        if not settings.tmdb_api_key:
            return []
        media = "tv" if kind is MediaKind.TV else "movie"
        payload = cast(
            "dict[str, Any]",
            await get_json(
                client,
                f"{BASE}/search/{media}",
                params={"api_key": settings.tmdb_api_key, "query": query, "include_adult": "false"},
            ),
        )
        out: list[Candidate] = []
        for r in cast("list[dict[str, Any]]", payload.get("results", []))[:limit]:
            name = str(r.get("title") or r.get("name") or "")
            date_str = str(r.get("release_date") or r.get("first_air_date") or "")
            year = int(date_str[:4]) if date_str[:4].isdigit() else None
            overview = str(r.get("overview") or "")
            pop = r.get("popularity")
            out.append(
                Candidate(
                    source=self.name,
                    id_key="tmdb",
                    canonical_id=str(r["id"]),
                    title=name,
                    year=year,
                    extra=(overview[:70] + "…") if len(overview) > 70 else overview,
                    url=f"https://www.themoviedb.org/{media}/{r['id']}",
                    popularity=float(pop) if isinstance(pop, (int, float)) else 0.0,
                )
            )
        return out

    # -- detail helpers (speculation / streaming) -------------------------
    async def movie_meta(self, client: httpx.AsyncClient, key: str, tmdb_id: str) -> MovieMeta:
        """Distributor + primary release date — inputs for the digital-date guess."""
        detail = cast(
            "dict[str, Any]",
            await get_json(client, f"{BASE}/movie/{tmdb_id}", params={"api_key": key}),
        )
        comps = cast("list[dict[str, Any]]", detail.get("production_companies", []))
        studios = tuple(str(c["name"]) for c in comps if c.get("name"))
        status = str(detail["status"]) if detail.get("status") else None
        title = str(detail["title"]) if detail.get("title") else None
        return MovieMeta(
            studios=studios,
            primary_date=_parse_tmdb_date(detail.get("release_date")),
            status=status,
            title=title,
        )

    async def _flatrate_providers(
        self,
        client: httpx.AsyncClient,
        key: str,
        media: str,
        tmdb_id: str,
        regions: tuple[str, ...],
    ) -> list[str]:
        """Subscription (flatrate) providers per region — TMDB's JustWatch feed."""
        names: list[str] = []
        providers = cast(
            "dict[str, Any]",
            await get_json(
                client, f"{BASE}/{media}/{tmdb_id}/watch/providers", params={"api_key": key}
            ),
        )
        results = cast("dict[str, Any]", providers.get("results", {}))
        for region in regions:
            block = cast("dict[str, Any]", results.get(region, {}))
            for prov in cast("list[dict[str, Any]]", block.get("flatrate", [])):
                name = str(prov.get("provider_name", "")).strip()
                # drop reseller add-ons ("HBO Max Amazon Channel") — keep the base service
                if name and "Channel" not in name and name not in names:
                    names.append(name)
        return names

    async def movie_platforms(
        self, client: httpx.AsyncClient, key: str, tmdb_id: str, regions: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Where the film actually streams now (empty until it lands somewhere)."""
        return tuple(await self._flatrate_providers(client, key, "movie", tmdb_id, regions))

    async def movie_graph(self, client: httpx.AsyncClient, key: str, tmdb_id: str) -> MediaGraph:
        """Who/what/series for a film, in one call (detail + appended credits)."""
        detail = cast(
            "dict[str, Any]",
            await get_json(
                client,
                f"{BASE}/movie/{tmdb_id}",
                params={"api_key": key, "append_to_response": "credits"},
            ),
        )
        credits = cast("dict[str, Any]", detail.get("credits", {}))
        people = _crew_people(credits) + _cast_people(credits)
        orgs = _company_orgs(detail.get("production_companies"), CreditRole.STUDIO)
        collection = detail.get("belongs_to_collection")
        series = (
            (str(collection["name"]), str(collection.get("id")))
            if isinstance(collection, dict) and collection.get("name")
            else None
        )
        genres = _genre_names(detail.get("genres"))
        return MediaGraph(
            credits=tuple(people + orgs),
            genres=genres,
            series=series,
            summary=str(detail["overview"]) if detail.get("overview") else None,
            is_anime=is_anime(detail, genres),
        )

    async def tv_graph(self, client: httpx.AsyncClient, key: str, tmdb_id: str) -> MediaGraph:
        """Who/what for a show: creators + networks + genres (+ top cast)."""
        detail = cast(
            "dict[str, Any]",
            await get_json(
                client,
                f"{BASE}/tv/{tmdb_id}",
                params={"api_key": key, "append_to_response": "credits"},
            ),
        )
        creators = [
            Credit(NodeKind.PERSON, CreditRole.CREATOR, str(c["name"]), str(c.get("id")))
            for c in cast("list[dict[str, Any]]", detail.get("created_by", []))
            if c.get("name")
        ]
        networks = _company_orgs(detail.get("networks"), CreditRole.NETWORK)
        cast_people = _cast_people(cast("dict[str, Any]", detail.get("credits", {})))
        # the show itself is the series a tracked "Show: Season N" belongs to.
        show_name = str(detail["name"]) if detail.get("name") else None
        genres = _genre_names(detail.get("genres"))
        return MediaGraph(
            credits=tuple(creators + networks + cast_people),
            genres=genres,
            series=(show_name, str(tmdb_id)) if show_name else None,
            summary=str(detail["overview"]) if detail.get("overview") else None,
            is_anime=is_anime(detail, genres),
        )

    async def tv_platforms(
        self, client: httpx.AsyncClient, key: str, tmdb_id: str, regions: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Likely streaming homes: origin networks + per-region flatrate providers."""
        names: list[str] = []

        def add(name: str) -> None:
            name = name.strip()
            if name and name not in names:
                names.append(name)

        detail = cast(
            "dict[str, Any]",
            await get_json(client, f"{BASE}/tv/{tmdb_id}", params={"api_key": key}),
        )
        for net in cast("list[dict[str, Any]]", detail.get("networks", [])):
            add(str(net.get("name", "")))
        for name in await self._flatrate_providers(client, key, "tv", tmdb_id, regions):
            add(name)
        return tuple(names)


# job titles in TMDB crew -> our CreditRole (anything else is dropped)
_CREW_ROLES: dict[str, CreditRole] = {
    "Director": CreditRole.DIRECTOR,
    "Writer": CreditRole.WRITER,
    "Screenplay": CreditRole.WRITER,
    "Story": CreditRole.WRITER,
    "Original Music Composer": CreditRole.COMPOSER,
}
_MAX_CAST = 5


def _crew_people(credits: dict[str, Any]) -> list[Credit]:
    out: list[Credit] = []
    seen: set[tuple[str, str]] = set()
    for member in cast("list[dict[str, Any]]", credits.get("crew", [])):
        role = _CREW_ROLES.get(str(member.get("job", "")))
        name = str(member.get("name", ""))
        if role is None or not name or (key := (role.value, name)) in seen:
            continue
        seen.add(key)
        out.append(Credit(NodeKind.PERSON, role, name, str(member.get("id"))))
    return out


def _cast_people(credits: dict[str, Any]) -> list[Credit]:
    return [
        Credit(NodeKind.PERSON, CreditRole.CAST, str(c["name"]), str(c.get("id")))
        for c in cast("list[dict[str, Any]]", credits.get("cast", []))[:_MAX_CAST]
        if c.get("name")
    ]


def _company_orgs(companies: object, role: CreditRole) -> list[Credit]:
    return [
        Credit(NodeKind.ORG, role, str(c["name"]), str(c.get("id")))
        for c in cast("list[Any]", companies or [])
        if isinstance(c, dict) and c.get("name")
    ]


def _genre_names(genres: object) -> tuple[str, ...]:
    return tuple(
        str(g["name"])
        for g in cast("list[Any]", genres or [])
        if isinstance(g, dict) and g.get("name")
    )


def is_anime(detail: dict[str, Any], genres: tuple[str, ...]) -> bool:
    """Japanese-origin animation => anime. Keyless heuristic: Animation genre AND a JP
    signal (origin/production country JP, or Japanese original language). Country, not
    language, is the load-bearing test — JP/US co-pros (e.g. Lazarus) air in English.
    """
    if "Animation" not in genres:
        return False
    countries = set(cast("list[str]", detail.get("origin_country") or []))
    countries.update(
        str(c["iso_3166_1"])
        for c in cast("list[Any]", detail.get("production_countries") or [])
        if isinstance(c, dict) and c.get("iso_3166_1")
    )
    return "JP" in countries or detail.get("original_language") == "ja"


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
