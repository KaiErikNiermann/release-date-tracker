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
from typing import Any, Final, cast

import httpx

from release_tracker.cache import TrendCache
from release_tracker.clock import utc_now, utc_today
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
    Stance,
)
from release_tracker.sources.base import (
    Candidate,
    Credit,
    MediaGraph,
    SourceResult,
    pinned_id,
    post_json,
    post_text,
    prominence,
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

# IGDB's game-level status (`game_statuses`). Absent is the common case and means nothing
# unusual — a plain released game reads None rather than 0 — so this is an exception flag,
# which is the same fail-open shape `seasons.stance_of` uses for TMDB's status words.
#
# Only the values that answer "is this coming at all" become a stance. Alpha/Beta/Early
# Access/Offline/Delisted all describe a game that *did* ship, so they are worth saying and
# have no business moving it out of the upcoming queue.
GAME_STANCE: Final[dict[int, Stance]] = {
    0: Stance.RELEASED,
    6: Stance.SHELVED,  # Cancelled
    7: Stance.UNCERTAIN,  # Rumored — announced by nobody official
}
_GAME_STATUS_NOTE: Final[dict[int, str]] = {
    2: "IGDB marks this “Alpha”",
    3: "IGDB marks this “Beta”",
    4: "IGDB marks this “Early Access”",
    5: "IGDB marks this “Offline” — it shipped, and its servers are gone",
    6: "IGDB marks this “Cancelled”",
    7: "IGDB marks this “Rumored” — no official announcement backs it",
    8: "IGDB marks this “Delisted” — it shipped and is no longer sold",
}

# `release_dates.status` is a *different* enum (`release_date_statuses`) scoped to one
# platform and region. 5 is "cancelled for this platform", which makes the row's date the one
# it would have landed on — not one it did. Recording that as a release invents a date for a
# game that has none: Prey 2 lists three such rows and produced three 2014 observations.
#
# (4 "Offline" is the same shape pointing the other way — its date is when the game went
# away, not when it arrived — but that is a question about shutdowns, not cancellations.)
_RELEASE_CANCELLED: Final[int] = 5

# IGDB's `company_statuses`. Only a studio that is *gone* is worth mentioning: renamed and
# merged companies still ship games under a new banner.
#
# Trustworthy when it fires and silent otherwise — Telltale, Visceral and Arkane Austin all
# carry their real shutdown dates, while Maxis (shut in 2015) still reads active. So it can
# support a sentence and must never move a stance: a studio closing does not cancel a game,
# and plenty of games outlive the team that started them.
_COMPANY_DEFUNCT: Final[int] = 1

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


# The Twitch app token is a property of the *credentials*, not of any one source instance, and
# is good for ~60 days. `sources_for()` hands out a shared instance, but several paths build
# their own (enrich's game_graph, studio_trend), so a per-instance cache made each of them pay a
# fresh ~0.4s OAuth round trip — two per capture of a game. Cached per client id at module scope
# so every instance shares one.
_TOKENS: dict[str, str] = {}
_TOKEN_LOCK = asyncio.Lock()


def forget_tokens() -> None:
    """Drop cached app tokens (credentials changed, and for tests)."""
    _TOKENS.clear()


def _as_count(value: object) -> float:
    """An IGDB engagement count, or 0 — the field is absent far more often than it is zero."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


# IGDB's own `game_types`, and what each means for someone who typed a bare title. Only the
# ones that are a *derivative of another game* are named: a remake, remaster or expanded game
# is a primary work people search for by name, so they carry no caveat.
_DERIVATIVE_TYPES: Final[dict[int, str]] = {
    1: "downloadable content for another game",
    2: "an expansion of another game",
    3: "a bundle, not the game itself",
    4: "a standalone expansion",
    5: "a mod of another game",
    6: "one episode of another game",
    7: "one season of another game",
    11: "a port of another game",
    12: "a fork of another game",
    13: "an add-on pack",
    14: "an update to another game",
}


def status_notes(
    status: int | None,
    rows: list[dict[str, Any]],
    observations: list[ReleaseObservation],
    today: date,
) -> tuple[str, ...]:
    """What IGDB's status words are worth saying, in IGDB's own words.

    The game-level word carries the claim; the per-platform rows corroborate it. That split
    matters because the game-level field has holes — Hytale was cancelled in 2025 and still
    reads "Early Access" — so a cancellation is worth more when both agree, and the reader
    is the one who should see that rather than a confidence number that hides it.
    """
    out: list[str] = []
    if status is not None and (said := _GAME_STATUS_NOTE.get(status)):
        out.append(said)
    cancelled = [r for r in rows if r.get("status") == _RELEASE_CANCELLED]
    if cancelled:
        where = ", ".join(
            sorted({str(p["name"]) for r in cancelled if isinstance(p := r.get("platform"), dict)})
        )
        every = " (every release it lists)" if len(cancelled) == len(rows) else ""
        out.append(f"IGDB marks its release cancelled{every}" + (f" — {where}" if where else ""))
    # Only worth raising while something is still being waited on. A studio closing says
    # nothing about a game you can already buy — Telltale shut down and The Walking Dead's
    # final season still shipped — so this is context for a wait, not a verdict on a release.
    shipped = any(o.release_date is not None and o.release_date <= today for o in observations)
    if not shipped and (gone := _defunct_developer(rows)) is not None:
        name, when = gone
        out.append(
            f"IGDB marks its developer {name} defunct"
            + (f" since {when.isoformat()}" if when else "")
        )
    return tuple(out)


def _defunct_developer(rows: list[dict[str, Any]]) -> tuple[str, date | None] | None:
    """The first credited developer IGDB says no longer exists, if any."""
    for row in rows:
        game = row.get("game")
        if not isinstance(game, dict):
            continue
        involved = cast("dict[str, Any]", game).get("involved_companies")
        for entry in cast("list[Any]", involved or []):
            if not isinstance(entry, dict) or not entry.get("developer"):
                continue
            company = cast("dict[str, Any]", entry).get("company")
            if not isinstance(company, dict):
                continue
            firm = cast("dict[str, Any]", company)
            if firm.get("status") == _COMPANY_DEFUNCT and firm.get("name"):
                return str(firm["name"]), _ts_to_date(firm.get("change_date"))
    return None


def caveats_for(row: dict[str, Any], ratings: float, hypes: float) -> tuple[str, ...]:
    """Why a hit might not be what the reader meant. Facts IGDB asserts, never guesses.

    Deliberately not inferred from platforms: "Game Boy Color only" reads as suspicious for a
    2025 release and as perfectly ordinary for a retro one, and no rule tells those apart.
    """
    out: list[str] = []
    kind = row.get("game_type")
    if isinstance(kind, int) and (what := _DERIVATIVE_TYPES.get(kind)):
        out.append(what)
    elif row.get("parent_game") or row.get("version_parent"):
        out.append("an edition of another game")
    if not ratings and not hypes:
        out.append("no ratings or hype")
    return tuple(out)


class IgdbSource:
    name = "igdb"

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
        cid = secret(settings.twitch_client_id)
        client_secret = secret(settings.twitch_client_secret)
        if not cid or not client_secret:
            log.warning("igdb.skip", reason=NO_KEYS)
            return None
        # lock so concurrent game pulls fetch the app token exactly once
        async with _TOKEN_LOCK:
            if cid not in _TOKENS:
                payload = cast(
                    "dict[str, Any]",
                    # Twitch OAuth requires POST (params on the query string)
                    await post_json(
                        client,
                        TOKEN_URL,
                        params={
                            "client_id": cid,
                            "client_secret": client_secret,
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
                _TOKENS[cid] = token
        return cid, _TOKENS[cid]

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
        # game.game_status rides along on the query we already make: the game-level status
        # (Cancelled / Rumored / Early Access) costs nothing extra because this request
        # already expands `game`.
        body = (
            "fields date,human,category,region,status,"
            "platform.name,game.name,game.slug,game.game_status,"
            "game.involved_companies.developer,"
            "game.involved_companies.company.name,"
            "game.involved_companies.company.status,"
            "game.involved_companies.company.change_date; "
            f"where game = {game_id}; limit 50;"
        )
        rows = cast(
            "list[dict[str, Any]]",
            await post_text(client, RELEASE_DATES_URL, content=body, headers=headers),
        )
        status = await self._game_status(client, headers, game_id, rows)
        now = utc_now()
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
        stance = GAME_STANCE.get(status) if status is not None else None
        log.info(
            "igdb.game",
            entity=entity.title,
            igdb_id=game_id,
            observations=len(observations),
            status=status,
            stance=stance.value if stance else None,
        )
        return SourceResult(
            observations=observations,
            external_ids=ids,
            notes=status_notes(status, rows, observations, utc_today()),
            stance=stance,
        )

    async def _game_status(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        game_id: str | int,
        rows: list[dict[str, Any]],
    ) -> int | None:
        """IGDB's game-level status, expanded off the rows we already have.

        The extra request is paid only when there are no rows to expand it from — which is
        exactly the interesting case, since a game nobody has scheduled is the shape of one
        that may never arrive (Half-Life 3 lists no release dates and reads "Rumored"). A
        healthy catalog full of dated games pays nothing, the way `_pull_tv` only fetches a
        show's shape on the 404.
        """
        for row in rows:
            game = row.get("game")
            if isinstance(game, dict):
                got = cast("dict[str, Any]", game).get("game_status")
                if isinstance(got, int):
                    return got
        if rows:
            return None
        found = cast(
            "list[dict[str, Any]]",
            await post_text(
                client,
                GAMES_URL,
                content=f"fields game_status; where id = {game_id};",
                headers=headers,
            ),
        )
        got = found[0].get("game_status") if found else None
        return got if isinstance(got, int) else None

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
        today = utc_today()
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
            "fields id,name,slug,first_release_date,platforms.abbreviation,"
            # Ranking signals, free on a request we already make. `game_type` and
            # `parent_game` say whether this is the game or something built on it;
            # `total_rating_count`/`hypes` say whether anyone has heard of it.
            f"game_type,parent_game,version_parent,total_rating_count,hypes; limit {limit};"
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
            ratings = _as_count(r.get("total_rating_count"))
            hypes = _as_count(r.get("hypes"))
            out.append(
                Candidate(
                    source=self.name,
                    id_key="igdb",
                    canonical_id=str(r["id"]),
                    title=str(r.get("name", "")),
                    year=year,
                    release_date=rel_date,
                    extra=abbrevs,
                    # An unreleased game has no ratings but plenty of hype, and a released one
                    # the reverse, so neither alone ranks both — take whichever is louder.
                    popularity=max(prominence(ratings), prominence(hypes, ceiling=1_000.0)),
                    caveats=caveats_for(r, ratings, hypes),
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

    A row cancelled for its platform is not a release and yields nothing: its date is the
    one the game would have had. The stance says why, so the reader is told rather than
    left with a title that simply has no dates.
    """
    if row.get("status") == _RELEASE_CANCELLED:
        return None
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
