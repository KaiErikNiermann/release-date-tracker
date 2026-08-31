"""TMDB puller — movies & TV.

The headline win: ``/movie/{id}/release_dates`` returns a per-country array where
each entry carries a release *type*, including a dedicated **Digital** type that
most movie DBs never expose. That is exactly the (channel, region, date) shape we
want, for free, the moment it is announced.

Free API key: https://www.themoviedb.org/settings/api
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, cast

import httpx

from release_tracker.clock import utc_now
from release_tracker.config import Settings, secret
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
    PlatformOffer,
    SourceResult,
    get_json,
    pinned_id,
)
from release_tracker.titles import split_season

log = get_logger("tmdb")

BASE = "https://api.themoviedb.org/3"

# Phrased for a person, not a log line — it reaches the add screen and `rdt doctor`.
NO_KEY = "TMDB_API_KEY is not set"


@dataclass(slots=True, frozen=True)
class MovieMeta:
    """Extra movie detail used for speculation (distributor + fallback date)."""

    studios: tuple[str, ...]  # all production companies, in TMDB order
    primary_date: date | None
    status: str | None
    title: str | None


@dataclass(slots=True, frozen=True)
class FilmCredit:
    """One credit on a person's filmography — the artist-radar's canonical pipeline.

    A film/TV creator's primary output isn't a feed they post to; it's their body of
    work arriving on a release calendar. ``person_credits`` mines that from TMDB so the
    radar can surface a director/actor's latest release and upcoming slate the same way
    it surfaces a YouTuber's latest video.
    """

    title: str
    media: str  # "movie" | "tv"
    role: str  # "Director" / "Writer" / "Creator" / "Actor"
    when: date | None
    tmdb_id: str
    url: str


# TMDB release_dates `type` integer -> our channel.
_TYPE_TO_CHANNEL: dict[int, ReleaseChannel] = {
    1: ReleaseChannel.PREMIERE,
    2: ReleaseChannel.THEATRICAL_LIMITED,
    3: ReleaseChannel.THEATRICAL,
    4: ReleaseChannel.DIGITAL,
    5: ReleaseChannel.PHYSICAL,
    6: ReleaseChannel.TV_BROADCAST,
}


@dataclass(frozen=True, slots=True)
class SeasonRef:
    """One season of a show as TMDB lists it on the show detail."""

    number: int
    name: str
    air_date: date | None
    episodes: int

    @property
    def specials(self) -> bool:
        """Season 0 — a specials bucket, not a season anyone means by "season"."""
        return self.number == 0


class TmdbSource:
    name = "tmdb"

    def supports(self, kind: MediaKind) -> bool:
        return kind in (MediaKind.MOVIE, MediaKind.TV)

    def unavailable(self, settings: Settings) -> str | None:
        """Why this source cannot answer right now, or None if it can."""
        return None if settings.tmdb_api_key else NO_KEY

    async def pull(
        self, client: httpx.AsyncClient, entity: Entity, settings: Settings
    ) -> SourceResult:
        if not settings.tmdb_api_key:
            log.warning("tmdb.skip", reason=NO_KEY, entity=entity.title)
            return SourceResult(skipped=NO_KEY)
        key = secret(settings.tmdb_api_key)
        assert key is not None  # guarded directly above
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
        now = utc_now()
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
        # Prefer the explicit structured coord (the `--season` path); fall back to parsing the
        # title ("The Boys: Season 5") for back-compat. Either way we search the *show* and then
        # resolve that specific season's date.
        show_title, parsed_season = split_season(entity.title)
        season_no = entity.season if entity.season is not None else parsed_season
        tmdb_id, skip = pinned_id(entity.external_ids, "tmdb")
        if skip:
            return SourceResult()
        if tmdb_id is None:
            tmdb_id = await self._search(client, key, show_title, "tv")
        if tmdb_id is None:
            return SourceResult()

        now = utc_now()
        observations: list[ReleaseObservation] = []
        url = f"https://www.themoviedb.org/tv/{tmdb_id}"
        air: date | None = None

        if season_no is not None:
            # a specific season: use ITS air_date only. A future season with no date
            # yet is TBA — do NOT fall back to the show's first_air_date / next episode,
            # those belong to a *different* (already-aired) season and would stamp the
            # unaired season with a wrong, ancient "confirmed" date (e.g. S3 -> S1's date).
            # A 404 means TMDB hasn't created this season yet (very-early renewal, e.g.
            # Pluribus S2) — also TBA, not an error.
            try:
                season = cast(
                    "dict[str, Any]",
                    await get_json(
                        client, f"{BASE}/tv/{tmdb_id}/season/{season_no}", params={"api_key": key}
                    ),
                )
                air = _parse_tmdb_date(season.get("air_date"))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                log.info("tmdb.season_absent", tmdb_id=tmdb_id, season=season_no)
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
        key = secret(settings.tmdb_api_key)
        if not key:
            return []
        media = "tv" if kind is MediaKind.TV else "movie"
        payload = cast(
            "dict[str, Any]",
            await get_json(
                client,
                f"{BASE}/search/{media}",
                params={"api_key": key, "query": query, "include_adult": "false"},
            ),
        )
        out: list[Candidate] = []
        for r in cast("list[dict[str, Any]]", payload.get("results", []))[:limit]:
            name = str(r.get("title") or r.get("name") or "")
            date_str = str(r.get("release_date") or r.get("first_air_date") or "")
            rel_date = _parse_tmdb_date(date_str)
            year = (
                rel_date.year
                if rel_date
                else (int(date_str[:4]) if date_str[:4].isdigit() else None)
            )
            overview = str(r.get("overview") or "")
            pop = r.get("popularity")
            out.append(
                Candidate(
                    source=self.name,
                    id_key="tmdb",
                    canonical_id=str(r["id"]),
                    title=name,
                    year=year,
                    release_date=rel_date,
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

    async def search_person(self, client: httpx.AsyncClient, key: str, name: str) -> str | None:
        """Resolve a creator name to a TMDB person id (so the filmography can be mined)."""
        payload = cast(
            "dict[str, Any]",
            await get_json(
                client,
                f"{BASE}/search/person",
                params={"api_key": key, "query": name, "include_adult": "false"},
            ),
        )
        return pick_person_id(cast("list[dict[str, Any]]", payload.get("results", [])))

    async def person_credits(
        self, client: httpx.AsyncClient, key: str, person_id: str
    ) -> tuple[FilmCredit, ...]:
        """A person's creative filmography — director/writer/creator + top-billed cast.

        Mines ``/person/{id}/combined_credits`` (one keyed GET), keeping only meaningful
        creative roles and dropping talk-show / "Self" noise. One credit per work (the
        most senior role wins when someone both directs and writes), newest first.
        """
        payload = cast(
            "dict[str, Any]",
            await get_json(
                client, f"{BASE}/person/{person_id}/combined_credits", params={"api_key": key}
            ),
        )
        best: dict[tuple[str, str], FilmCredit] = {}
        for member in cast("list[dict[str, Any]]", payload.get("crew", [])):
            role = _FILMOGRAPHY_JOBS.get(str(member.get("job", "")))
            if role is not None:
                consider_credit(best, member, role)
        for member in cast("list[dict[str, Any]]", payload.get("cast", [])):
            order = member.get("order")
            character = str(member.get("character", "")).strip().lower()
            if not isinstance(order, int) or order > _MAX_CAST or is_self(character):
                continue
            consider_credit(best, member, "Actor")
        return tuple(sorted(best.values(), key=lambda fc: fc.when or date.min, reverse=True))

    async def _flatrate_providers(
        self,
        client: httpx.AsyncClient,
        key: str,
        media: str,
        tmdb_id: str,
        regions: tuple[str, ...],
    ) -> list[PlatformOffer]:
        """Subscription (flatrate) providers, each tagged with the market it was read from.

        One offer per (region, service): the same service in two markets is two facts, and
        collapsing them by name is what made a where-line assert availability everywhere.

        ``regions`` must already be real market codes — pass ``Settings.provider_regions``,
        never ``Settings.regions``, which can hold the ``ANY``/``*`` profile sentinel that
        keys nothing here and silently yields no providers at all.
        """
        offers: list[PlatformOffer] = []
        seen: set[tuple[str, str]] = set()
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
                # drop reseller add-ons — keep the base service. Case-insensitively: TMDB
                # spells them both ways ("Paramount+ Amazon Channel", "Paramount Plus Apple
                # TV channel"), and a cased test kept every lowercase one.
                if not name or "channel" in name.casefold() or (region, name) in seen:
                    continue
                seen.add((region, name))
                offers.append(PlatformOffer(name, region))
        return offers

    async def movie_platforms(
        self, client: httpx.AsyncClient, key: str, tmdb_id: str, regions: tuple[str, ...]
    ) -> tuple[PlatformOffer, ...]:
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

    async def tv_seasons(
        self, client: httpx.AsyncClient, key: str, tmdb_id: str
    ) -> tuple[SeasonRef, ...]:
        """Every season TMDB lists for a show, in order, specials last.

        Read off the show detail the platform lookup already fetches, so a picker row gets its
        air date and episode count for free. The numbering matters as much as the list: it is
        the same numbering ``_pull_tv`` later resolves against at ``/tv/{id}/season/{n}``, so
        anything offered here is a season the puller can actually fetch.
        """
        detail = cast(
            "dict[str, Any]",
            await get_json(client, f"{BASE}/tv/{tmdb_id}", params={"api_key": key}),
        )
        seasons = [
            SeasonRef(
                number=number,
                name=str(raw.get("name") or f"Season {number}").strip(),
                air_date=_parse_tmdb_date(raw.get("air_date")),
                episodes=int(raw.get("episode_count") or 0),
            )
            for raw in cast("list[dict[str, Any]]", detail.get("seasons") or [])
            if isinstance(number := raw.get("season_number"), int)
        ]
        return tuple(sorted(seasons, key=lambda s: (s.number == 0, s.number)))

    async def tv_platforms(
        self, client: httpx.AsyncClient, key: str, tmdb_id: str, regions: tuple[str, ...]
    ) -> tuple[PlatformOffer, ...]:
        """Likely streaming homes: origin networks + per-region flatrate providers.

        Networks carry no region. A show's home channel is a fact about the production, not
        an availability in any one market — stamping it with a country would be the same
        untruth as leaving a flatrate offer unstamped.
        """
        offers: list[PlatformOffer] = []
        seen: set[tuple[str, str | None]] = set()

        def add(offer: PlatformOffer) -> None:
            if offer.name and (offer.name, offer.region) not in seen:
                seen.add((offer.name, offer.region))
                offers.append(offer)

        detail = cast(
            "dict[str, Any]",
            await get_json(client, f"{BASE}/tv/{tmdb_id}", params={"api_key": key}),
        )
        for net in cast("list[dict[str, Any]]", detail.get("networks", [])):
            add(PlatformOffer(str(net.get("name", "")).strip()))
        for offer in await self._flatrate_providers(client, key, "tv", tmdb_id, regions):
            add(offer)
        return tuple(offers)


# job titles in TMDB crew -> our CreditRole (anything else is dropped)
_CREW_ROLES: dict[str, CreditRole] = {
    "Director": CreditRole.DIRECTOR,
    "Writer": CreditRole.WRITER,
    "Screenplay": CreditRole.WRITER,
    "Story": CreditRole.WRITER,
    "Original Music Composer": CreditRole.COMPOSER,
}
_MAX_CAST = 5

# TMDB crew `job` -> the filmography role label we keep (everything else is dropped as
# below-the-line noise). Creator catches a TV showrunner; Screenplay/Story fold to Writer.
_FILMOGRAPHY_JOBS: dict[str, str] = {
    "Director": "Director",
    "Creator": "Creator",
    "Writer": "Writer",
    "Screenplay": "Writer",
    "Story": "Writer",
}
# seniority for collapsing multiple credits on one work to a single line (director wins).
_ROLE_RANK: dict[str, int] = {"Director": 3, "Creator": 3, "Writer": 2, "Actor": 1}
_SELF_CHARACTERS = frozenset({"self", "himself", "herself", "themselves"})


# departments that mark a TMDB person as a creator we'd radar (vs a stray crew/extra match)
_CREATIVE_DEPTS = frozenset({"Directing", "Writing", "Production", "Creator", "Acting"})


def pick_person_id(results: list[dict[str, Any]]) -> str | None:
    """Best person match from a TMDB person search: most popular, creators preferred.

    TMDB returns rough relevance order; we re-rank so a creative-department match outranks
    an incidental one and, within that, the most popular (the person you most likely meant).
    """
    candidates = [r for r in results if r.get("id") is not None]
    if not candidates:
        return None

    def _score(r: dict[str, Any]) -> tuple[int, float]:
        creative = 1 if str(r.get("known_for_department", "")) in _CREATIVE_DEPTS else 0
        pop = r.get("popularity")
        return (creative, float(pop) if isinstance(pop, (int, float)) else 0.0)

    return str(max(candidates, key=_score)["id"])


def is_self(character: str) -> bool:
    """A talk-show / documentary 'as themselves' appearance, not a creative credit."""
    return character in _SELF_CHARACTERS or character.startswith("self ")


def consider_credit(
    best: dict[tuple[str, str], FilmCredit], raw: dict[str, Any], role: str
) -> None:
    """Keep the most senior role per (media, work) — crew director over a cast bit-part."""
    media = str(raw.get("media_type", ""))
    work_id = str(raw.get("id", "") or "")
    title = str(raw.get("title") or raw.get("name") or "").strip()
    if media not in ("movie", "tv") or not work_id or not title:
        return
    key = (media, work_id)
    prev = best.get(key)
    if prev is not None and _ROLE_RANK[prev.role] >= _ROLE_RANK[role]:
        return
    best[key] = FilmCredit(
        title=title,
        media=media,
        role=role,
        when=_parse_tmdb_date(raw.get("release_date") or raw.get("first_air_date")),
        tmdb_id=work_id,
        url=f"https://www.themoviedb.org/{media}/{work_id}",
    )


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
