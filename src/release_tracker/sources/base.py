"""Source protocol and shared HTTP helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from release_tracker.config import Settings
from release_tracker.models import Entity, MediaKind, ReleaseObservation

USER_AGENT = "release-date-tracker/0.1 (+https://github.com/local/release-date-tracker)"
DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


@dataclass(slots=True)
class SourceResult:
    """What a single source produced for one entity."""

    observations: list[ReleaseObservation] = field(default_factory=list[ReleaseObservation])
    external_ids: dict[str, str] = field(default_factory=dict[str, str])


@runtime_checkable
class Source(Protocol):
    """A Tier-0 puller. Stateless; given an entity, yields sourced observations."""

    name: str

    def supports(self, kind: MediaKind) -> bool: ...

    async def pull(
        self, client: httpx.AsyncClient, entity: Entity, settings: Settings
    ) -> SourceResult: ...


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
