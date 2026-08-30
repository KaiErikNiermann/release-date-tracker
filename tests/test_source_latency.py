"""Round trips a source makes on the interactive add path.

Behavioural, not timed: they count requests, because the cost being guarded against is a
round trip that did not need to happen — a serial year-probe, a re-fetched OAuth token.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import SecretStr

from release_tracker.config import Settings
from release_tracker.models import Entity, MediaKind
from release_tracker.sources import igdb as igdb_mod
from release_tracker.sources.whentostream import hints


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


# --- When To Stream: probe the year candidates concurrently, answer in offset order ---------
_ARTICLE = (
    '<meta property="og:title" content="Nowhere - When To Stream">'
    "<p>PVOD Release Date : August 15, 2026</p>"
)


async def test_year_probe_stops_at_the_canonical_year() -> None:
    """A hit on offset 0 must not wait on (or even read) the neighbour years."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text=_ARTICLE)

    async with _client(handler) as c:
        got = await hints(c, "Nowhere", kind=MediaKind.MOVIE, year=2026)
    assert got is not None
    assert got.url.endswith("/nowhere-2026/")  # the canonical year wins, not a neighbour


async def test_year_probe_prefers_the_canonical_year_over_a_neighbour() -> None:
    """Offset order is the answer even when a neighbour year also parses."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("-2025/"):
            return httpx.Response(200, text=_ARTICLE)
        if str(request.url).endswith("-2026/"):
            return httpx.Response(200, text=_ARTICLE.replace("August 15", "September 9"))
        return httpx.Response(404, text="")

    async with _client(handler) as c:
        got = await hints(c, "Nowhere", kind=MediaKind.MOVIE, year=2026)
    assert got is not None
    assert got.url.endswith("/nowhere-2026/")


async def test_year_probe_reports_a_miss_after_trying_every_offset() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(404, text="")

    async with _client(handler) as c:
        assert await hints(c, "Nowhere", kind=MediaKind.MOVIE, year=2026) is None
    assert {u.rsplit("-", 1)[-1] for u in seen} == {"2026/", "2025/", "2027/"}


# --- IGDB: one app token shared by every source instance ------------------------------------
async def test_igdb_token_is_shared_across_source_instances() -> None:
    """enrich and studio_trend build their own IgdbSource; none should re-authenticate."""
    tokens = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal tokens
        if "oauth2/token" in str(request.url):
            tokens += 1
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(200, json=[])

    settings = Settings().model_copy(
        update={"twitch_client_id": SecretStr("id"), "twitch_client_secret": SecretStr("sec")}
    )
    entity = Entity.create("A Game", MediaKind.GAME, external_ids={"igdb": "1"})
    async with _client(handler) as c:
        await igdb_mod.IgdbSource().pull(c, entity, settings)
        await igdb_mod.IgdbSource().pull(c, entity, settings)  # a *different* instance
    assert tokens == 1


async def test_forget_tokens_forces_reauthentication() -> None:
    tokens = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal tokens
        if "oauth2/token" in str(request.url):
            tokens += 1
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(200, json=[])

    settings = Settings().model_copy(
        update={"twitch_client_id": SecretStr("id"), "twitch_client_secret": SecretStr("sec")}
    )
    entity = Entity.create("A Game", MediaKind.GAME, external_ids={"igdb": "1"})
    async with _client(handler) as c:
        await igdb_mod.IgdbSource().pull(c, entity, settings)
        igdb_mod.forget_tokens()
        await igdb_mod.IgdbSource().pull(c, entity, settings)
    assert tokens == 2
