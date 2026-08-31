"""Source protocol and shared HTTP helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from release_tracker.config import Settings
from release_tracker.models import CreditRole, Entity, MediaKind, NodeKind, ReleaseObservation

USER_AGENT = "release-date-tracker/0.1 (+https://github.com/KaiErikNiermann/release-date-tracker)"
DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


@dataclass(slots=True, frozen=True)
class Credit:
    """One who/where/what fact a source extracted for a work (a graph edge-to-be)."""

    node_kind: NodeKind  # PERSON or ORG
    role: CreditRole
    name: str
    source_id: str | None = None  # source-native id, collapses the node across works


def prominence(count: float, *, ceiling: float = 10_000.0) -> float:
    """Squash an unbounded engagement count into 0..1, log-scaled.

    Log because the interesting distinction is *some* audience versus none — 5000 ratings
    against 1000 says far less than 1000 against 0, which is exactly the gap between a real
    release and a fan entry nobody has touched.
    """
    if count <= 0:
        return 0.0
    return min(1.0, math.log10(1.0 + count) / math.log10(1.0 + ceiling))


@dataclass(slots=True, frozen=True)
class PlatformOffer:
    """One consumption platform a work is on, scoped to the market it was read from.

    ``region`` is the ISO-2 the offer was found in, or ``None`` when the fact isn't a market
    fact at all — a show's origin network and a game's hardware are properties of the thing,
    not of anywhere you happen to be. ``predicted`` marks our own guess rather than a source's
    answer, so the write side can file it at MODEL tier without a parallel container.
    """

    name: str
    region: str | None = None
    predicted: bool = False


@dataclass(slots=True, frozen=True)
class MediaGraph:
    """Source-extracted who/what/series for a work. ``where`` (platforms) is filled
    for games here (hardware/storefronts); movies/TV resolve it via watch-providers."""

    credits: tuple[Credit, ...] = ()
    genres: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    series: tuple[str, str | None] | None = None  # (name, source_id)
    summary: str | None = None  # plot/overview — grounding for LLM theme extraction
    is_anime: bool = False  # Japanese animation -> an "anime" origin tag (orthogonal to format)


@dataclass(slots=True)
class SourceResult:
    """What a single source produced for one entity.

    ``skipped`` is the difference between "I looked and there is nothing" and "I was never
    able to look" — an unconfigured source returns empty either way, and a caller that
    cannot tell them apart will happily delete the rows this source wrote last time. It
    carries the reason so the difference can also be shown to a person.
    """

    observations: list[ReleaseObservation] = field(default_factory=list[ReleaseObservation])
    external_ids: dict[str, str] = field(default_factory=dict[str, str])
    skipped: str | None = None
    # What this source wants said about *this* pull, in the reader's words. A third thing
    # again: `skipped` is "I was never able to look", an exception is "something broke", and
    # this is "I looked, I answered, and something about the answer needs attributing".
    # Printed verbatim, never parsed.
    notes: tuple[str, ...] = ()


@dataclass(slots=True)
class Candidate:
    """A canonical match proposal for manual resolution.

    ``id_key``/``canonical_id`` is exactly what gets pinned into an entity's
    ``external_ids`` so the pullers use it directly instead of searching.
    """

    source: str
    id_key: str  # e.g. "tmdb", "igdb", "steam_appid"
    canonical_id: str
    title: str
    year: int | None = None
    # the full release/air/first-release date when the search response carried one — used to
    # pick "the latest match" at day granularity (``year`` alone can't separate two same-year
    # candidates). ``year`` stays the display/scoring field; this is None when only a year is known.
    release_date: date | None = None
    extra: str = ""  # disambiguators: media type, platforms, slug...
    url: str | None = None
    score: float = 0.0  # filled by the matching layer: how well the *title* matches
    # 0..1 prominence, normalised by the source so figures from different APIs are
    # comparable. Breaks ties between candidates the title cannot separate — which is the
    # whole Silksong problem: "Hollow Knight Silksong" and "Hollow Knight: Silksong"
    # normalise to the same string and score identically, so without this the API's own
    # result order decides, and it puts the fan demake first.
    popularity: float = 0.0
    # Why this hit might not be the thing the reader meant, in plain words ("a mod of another
    # game", "no ratings or hype"). Rendered beside the row; never hides it — a caveat is
    # something to notice, not a verdict.
    caveats: tuple[str, ...] = ()


@runtime_checkable
class Source(Protocol):
    """A Tier-0 puller. Stateless; given an entity, yields sourced observations."""

    name: str

    def supports(self, kind: MediaKind) -> bool: ...

    def unavailable(self, settings: Settings) -> str | None:
        """Why this source cannot answer right now, or None if it can.

        Pure and cheap — it reads configuration, never the network — so callers can ask
        before doing any work and tell a user what is missing instead of returning a
        silently empty result.
        """
        ...

    async def pull(
        self, client: httpx.AsyncClient, entity: Entity, settings: Settings
    ) -> SourceResult: ...

    async def search_candidates(
        self,
        client: httpx.AsyncClient,
        query: str,
        kind: MediaKind,
        settings: Settings,
        *,
        limit: int = 6,
    ) -> list[Candidate]: ...


# Pin one of these as an external id (e.g. steam_appid=none) to tell a source the
# item doesn't exist there, so it stops blind-searching and injecting wrong rows.
SKIP_IDS = frozenset({"none", "skip", "-", "x", "na"})


def pinned_id(external_ids: dict[str, str], key: str) -> tuple[str | None, bool]:
    """Resolve a pinned external id. Returns (id_to_use, should_skip_source)."""
    value = external_ids.get(key)
    if value is None:
        return None, False
    if value.strip().lower() in SKIP_IDS:
        return None, True
    return value, False


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )


@retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
    stop=stop_after_attempt(4),
    reraise=True,
)
async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> object:
    """GET returning parsed JSON, retrying on transient errors / 5xx / 429."""
    resp = await client.get(url, params=params, headers=headers)
    if resp.status_code in (429, 500, 502, 503, 504):
        resp.raise_for_status()
    return resp.json()


@retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
    stop=stop_after_attempt(4),
    reraise=True,
)
async def get_json_absentable(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> object | None:
    """GET returning ``None`` when the resource is genuinely absent, parsed JSON otherwise.

    :func:`get_json` raises only for 429/5xx, so a 404 comes back as whatever the API put in
    the *body* — for TMDB, ``{"success": false, "status_code": 34, …}``, which reads to every
    caller as a successful response that merely has no fields. That is why asking for a season
    a show does not have and asking for one with no air date yet were the same code path.

    Absent and empty are different answers, the same way ``SourceResult.skipped`` separates
    "never asked" from "found nothing". This reads the status rather than sniffing the body:
    a body-shape guess would start discarding real payloads that happen to carry a ``success``
    key.
    """
    resp = await client.get(url, params=params, headers=headers)
    if resp.status_code in (429, 500, 502, 503, 504):
        resp.raise_for_status()
    if resp.status_code == 404:
        return None
    return resp.json()


@retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
    stop=stop_after_attempt(4),
    reraise=True,
)
async def get_text(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> str:
    """GET returning the raw response text (RSS/Atom feeds, HTML), retrying transient errors."""
    resp = await client.get(url, params=params, headers=headers)
    if resp.status_code in (429, 500, 502, 503, 504):
        resp.raise_for_status()
    return resp.text


@retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
    stop=stop_after_attempt(4),
    reraise=True,
)
async def post_text(
    client: httpx.AsyncClient,
    url: str,
    *,
    content: str,
    headers: dict[str, str] | None = None,
) -> object:
    """POST a raw body (e.g. IGDB apicalypse), returning parsed JSON."""
    resp = await client.post(url, content=content, headers=headers)
    if resp.status_code in (429, 500, 502, 503, 504):
        resp.raise_for_status()
    return resp.json()


@retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
    stop=stop_after_attempt(4),
    reraise=True,
)
async def post_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> object:
    """POST with query params (e.g. OAuth token endpoints), returning parsed JSON."""
    resp = await client.post(url, params=params, headers=headers)
    if resp.status_code in (429, 500, 502, 503, 504):
        resp.raise_for_status()
    return resp.json()
