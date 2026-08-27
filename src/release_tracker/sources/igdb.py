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

from release_tracker.cache import TrendCache
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
    pinned_id,
    post_json,
    post_text,
)
from release_tracker.trends import StudioTrend, compute_trend, narrow_coarse

log = get_logger("igdb")

# Phrased for a person, not a log line — these reach the add screen and `rdt doctor`.
NO_KEYS = "TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET are not set"
BAD_KEYS = "Twitch rejected these credentials"

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

# precisions coarse enough to benefit from a studio-timing bias (a known month/day
# is already as good as the trend prior, so it is left untouched).
_COARSE: tuple[DatePrecision, ...] = (DatePrecision.YEAR, DatePrecision.QUARTER)

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


def _slug_of(rows: list[dict[str, Any]]) -> str | None:
    """The game's slug from any release-date row that carries it (they all name one game)."""
    for row in rows:
        game = row.get("game")
        if isinstance(game, dict):
            slug = cast("dict[str, Any]", game).get("slug")
            if isinstance(slug, str) and slug:
                return slug
    return None


class IgdbSource:
    name = "igdb"

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_lock = asyncio.Lock()

    def supports(self, kind: MediaKind) -> bool:
        return kind is MediaKind.GAME

    def unavailable(self, settings: Settings) -> str | None:
        """Why this source cannot answer right now, or None if it can.

        Only the credential *gap* is knowable without a request; a rejected credential is
        found by `_ensure_token` and reported from there.
        """
        if not settings.twitch_client_id or not settings.twitch_client_secret:
            return NO_KEYS
        return None

    async def _ensure_token(
        self, client: httpx.AsyncClient, settings: Settings
    ) -> tuple[str, str] | None:
        cid, secret = settings.twitch_client_id, settings.twitch_client_secret
        if not cid or not secret:
            log.warning("igdb.skip", reason=NO_KEYS)
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
                token = payload.get("access_token")
                if not isinstance(token, str) or not token:
                    # Twitch answers bad credentials with 400 and a JSON body, which
                    # `post_json` passes through untouched — reading the key blind raised
                    # KeyError, which the search layer swallowed into "no matches". A typo
                    # in the secret looked exactly like the game not existing.
                    log.warning("igdb.auth_rejected", body=str(payload)[:200])
                    return None
                self._token = token
        return cid, self._token

    async def pull(
        self, client: httpx.AsyncClient, entity: Entity, settings: Settings
    ) -> SourceResult:
        auth = await self._ensure_token(client, settings)
        if auth is None:
            return SourceResult(skipped=self.unavailable(settings) or BAD_KEYS)
        cid, token = auth
        headers = {"Client-ID": cid, "Authorization": f"Bearer {token}"}

        game_id, skip = pinned_id(entity.external_ids, "igdb")
        if skip:
            return SourceResult()
        if game_id is None:
            game_id = await self._search(client, headers, entity.title)
        if game_id is None:
            return SourceResult()

        # game.slug: IGDB addresses its pages by slug, not by the numeric id we pin, and
        # Wikidata's P5794 stores the slug too — so pinning it both fixes the source_url
        # (`igdb_slug` was already read at row_to_observation but never written) and lets the
        # Wikidata id-hub join find the game.
        body = (
            "fields date,human,category,region,status,"
            "platform.name,game.name,game.slug; "
            f"where game = {game_id}; limit 50;"
        )
        rows = cast(
            "list[dict[str, Any]]",
            await post_text(client, RELEASE_DATES_URL, content=body, headers=headers),
        )
        now = datetime.now(UTC)
        observations = [
            obs
            for row in rows
            if (obs := row_to_observation(row, entity, game_id, now)) is not None
        ]
        if (
            refined := await self._trend_refinement(
                client, settings, str(game_id), observations, now
            )
        ) is not None:
            observations.append(refined)
        ids: dict[str, str] = {"igdb": str(game_id)}
        if (slug := _slug_of(rows)) is not None:
            ids["igdb_slug"] = slug
        log.info("igdb.game", entity=entity.title, igdb_id=game_id, observations=len(observations))
        return SourceResult(observations=observations, external_ids=ids)

    async def _trend_refinement(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        game_id: str,
        observations: list[ReleaseObservation],
        now: datetime,
    ) -> ReleaseObservation | None:
        """A studio-trend-narrowed estimate for a coarse primary date, persisted so the
        tracker pivots on the same refined date the live ``/rd`` lookup shows.

        ``None`` when the primary date is already precise or the publisher has no clear
        pattern. Carries ``provider=igdb`` so a re-pull's provider-scoped cleanup rotates
        it, but ``MODEL``/``PREDICTED`` so the resolver treats it as the soft estimate it is.
        """
        coarse = [
            o
            for o in observations
            if o.channel is ReleaseChannel.PRIMARY and o.release_date and o.precision in _COARSE
        ]
        if not coarse:
            return None
        basis = min(coarse, key=lambda o: o.release_date or date.max)
        assert basis.release_date is not None
        trend = await self.studio_trend(client, settings, game_id)
        if trend is None:
            return None
        est = narrow_coarse(basis.release_date, basis.precision, trend)
        if est is None:
            return None
        return ReleaseObservation(
            entity_id=basis.entity_id,
            channel=ReleaseChannel.PRIMARY,
            region="WW",
            release_date=est.when,
            precision=DatePrecision.EXACT,
            certainty=Certainty.PREDICTED,
            source_tier=SourceTier.MODEL,
            provider=self.name,
            source_name=f"Studio trend ({trend.studio_name})",
            source_quote=est.basis,
            confidence=est.confidence,
            fetched_at=now,
        )

    # -- studio-trend mining ----------------------------------------------
    async def _headers(
        self, client: httpx.AsyncClient, settings: Settings
    ) -> dict[str, str] | None:
        auth = await self._ensure_token(client, settings)
        if auth is None:
            return None
        cid, token = auth
        return {"Client-ID": cid, "Authorization": f"Bearer {token}"}

    async def game_publisher(
        self, client: httpx.AsyncClient, settings: Settings, game_id: str
    ) -> tuple[str, str] | None:
        """The publisher (id, name) for a game — falls back to developer, then first."""
        headers = await self._headers(client, settings)
        if headers is None:
            return None
        body = (
            "fields involved_companies.company.id,involved_companies.company.name,"
            "involved_companies.publisher,involved_companies.developer; "
            f"where id = {int(game_id)};"
        )
        rows = cast(
            "list[dict[str, Any]]",
            await post_text(client, GAMES_URL, content=body, headers=headers),
        )
        if not rows:
            return None
        inv = cast("list[dict[str, Any]]", rows[0].get("involved_companies", []))
        picked = (
            next((c for c in inv if c.get("publisher")), None)
            or next((c for c in inv if c.get("developer")), None)
            or (inv[0] if inv else None)
        )
        company = picked.get("company") if picked else None
        if not isinstance(company, dict) or "id" not in company:
            return None
        return str(company["id"]), str(company.get("name", ""))

    async def company_release_months(
        self, client: httpx.AsyncClient, settings: Settings, company_id: str, *, exclude: str
    ) -> tuple[int, ...]:
        """Release months of games this company published (past, this game excluded).

        Uses each game's single ``first_release_date`` (one sample per title, so
        multi-platform/region rows don't inflate the histogram). Coarse-dated games
        land their timestamp at the period start — mild noise the concentration
        threshold + ``MIN_SAMPLES`` are there to absorb.
        """
        headers = await self._headers(client, settings)
        if headers is None:
            return ()
        body = (
            "fields id,first_release_date; "
            f"where involved_companies.company = {int(company_id)} "
            "& involved_companies.publisher = true & first_release_date != null; "
            "sort first_release_date desc; limit 100;"
        )
        rows = cast(
            "list[dict[str, Any]]",
            await post_text(client, GAMES_URL, content=body, headers=headers),
        )
        today = datetime.now(UTC).date()
        months: list[int] = []
        for r in rows:
            if str(r.get("id")) == str(exclude):
                continue
            d = _ts_to_date(r.get("first_release_date"))
            if d is None or d > today:  # only realized releases inform the pattern
                continue
            months.append(d.month)
        return tuple(months)

    async def studio_trend(
        self, client: httpx.AsyncClient, settings: Settings, game_id: str
    ) -> StudioTrend | None:
        """The publisher's release-timing trend for a game, mined on demand and cached.

        Shared by the live ``/rd`` lookup and the persistence pull so both narrow a
        coarse game date toward the same studio pattern.
        """
        pub = await self.game_publisher(client, settings, game_id)
        if pub is None:
            return None
        company_id, name = pub
        studio_key = f"igdb:{company_id}"
        with TrendCache(settings.trend_cache_path) as cache:
            if (cached := cache.get(studio_key, MediaKind.GAME)) is not None:
                return cached
            months = await self.company_release_months(
                client, settings, company_id, exclude=game_id
            )
            trend = compute_trend(studio_key, name, MediaKind.GAME, months)
            cache.put(trend)
            return trend

    async def game_graph(
        self, client: httpx.AsyncClient, settings: Settings, game_id: str
    ) -> MediaGraph:
        """Who (dev/publisher orgs) / what (genres) / where (platforms) / series for a game."""
        headers = await self._headers(client, settings)
        if headers is None:
            return MediaGraph()
        body = (
            "fields involved_companies.company.id,involved_companies.company.name,"
            "involved_companies.developer,involved_companies.publisher,"
            "genres.name,platforms.name,collection.name,collection.id,summary; "
            f"where id = {int(game_id)};"
        )
        rows = cast(
            "list[dict[str, Any]]",
            await post_text(client, GAMES_URL, content=body, headers=headers),
        )
        if not rows:
            return MediaGraph()
        row = rows[0]
        credits: list[Credit] = []
        for ic in cast("list[dict[str, Any]]", row.get("involved_companies", [])):
            company = ic.get("company")
            if not isinstance(company, dict) or not company.get("name"):
                continue
            role = (
                CreditRole.DEVELOPER
                if ic.get("developer")
                else CreditRole.PUBLISHER
                if ic.get("publisher")
                else CreditRole.STUDIO
            )
            credits.append(Credit(NodeKind.ORG, role, str(company["name"]), str(company.get("id"))))
        collection = row.get("collection")
        series = (
            (str(collection["name"]), str(collection.get("id")))
            if isinstance(collection, dict) and collection.get("name")
            else None
        )
        return MediaGraph(
            credits=tuple(credits),
            genres=_names(row.get("genres")),
            platforms=_names(row.get("platforms")),
            series=series,
            summary=str(row["summary"]) if row.get("summary") else None,
        )

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
        auth = await self._ensure_token(client, settings)
        if auth is None:
            return []
        cid, token = auth
        headers = {"Client-ID": cid, "Authorization": f"Bearer {token}"}
        body = (
            f'search "{query}"; '
            f"fields id,name,slug,first_release_date,platforms.abbreviation; limit {limit};"
        )
        rows = cast(
            "list[dict[str, Any]]",
            await post_text(client, GAMES_URL, content=body, headers=headers),
        )
        out: list[Candidate] = []
        for r in rows:
            rel_date = _ts_to_date(r.get("first_release_date"))
            year = rel_date.year if rel_date else None
            plats = cast("list[dict[str, Any]]", r.get("platforms", []))
            abbrevs = ", ".join(
                str(p.get("abbreviation", "")) for p in plats if p.get("abbreviation")
            )
            out.append(
                Candidate(
                    source=self.name,
                    id_key="igdb",
                    canonical_id=str(r["id"]),
                    title=str(r.get("name", "")),
                    year=year,
                    release_date=rel_date,
                    extra=abbrevs,
                    url=f"https://www.igdb.com/games/{r.get('slug', r['id'])}",
                )
            )
        return out


def row_to_observation(
    row: dict[str, Any], entity: Entity, game_id: str | int, now: datetime
) -> ReleaseObservation | None:
    """One IGDB ``release_dates`` row -> an observation (``None`` if it carries no date).

    Derives certainty from precision (only an exact, announced day is *confirmed*; a
    coarse "2026"/"Q3 2026" is an ``ESTIMATED`` window) and normalises a "TBD" row that
    still carries a placeholder timestamp down to a coarse YEAR at the period start.
    """
    precision = _CATEGORY_TO_PRECISION.get(int(row.get("category", 7)), DatePrecision.TBA)
    rel = _ts_to_date(row.get("date"))
    if rel is None and precision is not DatePrecision.TBA:
        return None
    if rel is not None and precision is DatePrecision.TBA:
        # IGDB sometimes ships a "TBD" row with a placeholder year-end timestamp — the
        # year is the only trustworthy part, so anchor it at the period start as a coarse
        # YEAR (narrowable) rather than a spuriously precise Dec-31.
        precision, rel = DatePrecision.YEAR, date(rel.year, 1, 1)
    certainty = Certainty.CONFIRMED if precision is DatePrecision.EXACT else Certainty.ESTIMATED
    platform = row.get("platform")
    platform_name = (
        str(platform["name"]) if isinstance(platform, dict) and "name" in platform else None
    )
    slug = entity.external_ids.get("igdb_slug", str(game_id))
    return ReleaseObservation(
        entity_id=entity.id,
        channel=ReleaseChannel.PRIMARY,
        region=_REGION.get(int(row.get("region", 8)), "WW"),
        release_date=rel,
        precision=precision,
        certainty=certainty,
        source_tier=SourceTier.AGGREGATOR,
        provider=IgdbSource.name,
        source_name=f"IGDB ({platform_name})" if platform_name else "IGDB",
        source_url=f"https://www.igdb.com/games/{slug}",
        source_quote=str(row.get("human")) if row.get("human") else None,
        fetched_at=now,
    )


def _ts_to_date(value: object) -> date | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(int(value), tz=UTC).date()


def _names(items: object) -> tuple[str, ...]:
    """Extract the ``name`` field from an IGDB expanded-list (genres/platforms)."""
    return tuple(
        str(item["name"])
        for item in cast("list[Any]", items or [])
        if isinstance(item, dict) and item.get("name")
    )
