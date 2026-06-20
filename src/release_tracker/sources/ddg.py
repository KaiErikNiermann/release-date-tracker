"""DuckDuckGo Instant Answer — a keyless web-context fallback.

When the structured sources (TMDB / IGDB / Steam) turn up nothing for a query, this
attaches a lean, Wikipedia-grounded blurb + a few related links as raw ``web_info`` — the
same context a human (or the LLM) would otherwise web-search for by hand. It is *not*
parsed into structured fields (no date/price extraction — that's deliberately out of
scope); it's read-only context for filling a gap or spotting a discrepancy.

Why the Instant Answer API and not the AI/SERP: ``api.duckduckgo.com`` is documented,
unauthed, unmetered JSON. DDG's AI answer is gated behind an obfuscated JS anti-bot
challenge (not curl-replayable) and the SERP scrape needs a brittle scraped token — the
Instant Answer endpoint hands over the *valuable* part of both (the grounded abstract +
related links) with none of the fragility.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import httpx

from release_tracker.logging import get_logger
from release_tracker.sources.base import get_json

log = get_logger("ddg")

_ENDPOINT = "https://api.duckduckgo.com/"
_MAX_RELATED = 5


@dataclass(frozen=True, slots=True)
class WebInfo:
    """A lean web-context blurb for a query — Wikipedia abstract + a few related links."""

    heading: str
    abstract: str
    source: str
    url: str | None
    related: tuple[tuple[str, str], ...]  # (text, url), capped to _MAX_RELATED

    def to_dict(self) -> dict[str, object]:
        return {
            "heading": self.heading,
            "abstract": self.abstract,
            "source": self.source,
            "url": self.url,
            "related": [{"text": text, "url": url} for text, url in self.related],
        }


def _related_links(topics: list[Any], limit: int) -> tuple[tuple[str, str], ...]:
    """Flatten RelatedTopics (which nest one level under 'Topics') to (text, url) pairs."""
    out: list[tuple[str, str]] = []
    for item in topics:
        if not isinstance(item, dict):
            continue
        if nested := cast("list[Any]", item.get("Topics")):
            out.extend(_related_links(nested, limit - len(out)))
        else:
            text = str(item.get("Text", "")).strip()
            url = str(item.get("FirstURL", "")).strip()
            if text and url:
                out.append((text, url))
        if len(out) >= limit:
            break
    return tuple(out[:limit])


def parse_instant_answer(
    payload: dict[str, Any], *, max_related: int = _MAX_RELATED
) -> WebInfo | None:
    """Decode a DDG Instant Answer payload into a lean WebInfo, or None if it's empty.

    Pure — returns None when there's no abstract *and* no related links (a miss), so the
    caller simply omits the field rather than attaching noise.
    """
    abstract = str(payload.get("AbstractText", "")).strip()
    related = _related_links(cast("list[Any]", payload.get("RelatedTopics", [])), max_related)
    if not abstract and not related:
        return None
    heading = str(payload.get("Heading", "")).strip()
    return WebInfo(
        heading=heading,
        abstract=abstract,
        source=str(payload.get("AbstractSource", "")).strip() or "DuckDuckGo",
        url=str(payload.get("AbstractURL", "")).strip() or None,
        related=related,
    )


async def instant_answer(
    client: httpx.AsyncClient, query: str, *, max_related: int = _MAX_RELATED
) -> WebInfo | None:
    """Best-effort web-context for a query. Never raises — a miss/outage yields None."""
    try:
        payload = await get_json(
            client,
            _ENDPOINT,
            params={
                "q": query,
                "format": "json",
                "no_html": "1",
                "skip_disambig": "1",
                "t": "release-date-tracker",
            },
        )
    except Exception as exc:  # a keyless best-effort fallback must never break a lookup
        log.warning("ddg.fetch_error", query=query, error=str(exc))
        return None
    if not isinstance(payload, dict):
        return None
    return parse_instant_answer(cast("dict[str, Any]", payload), max_related=max_related)
