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


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


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
