"""Artist-radar: fetch a followed creator's latest content from free endpoints.

Conservative by design — only platforms with a clean, keyless public endpoint get a
fetcher (YouTube channel RSS, Bluesky public AT-Proto API, Reddit user RSS best-effort).
Every other platform is link-only: stored + documented, never fetched.

The network-touching fetchers are thin; the parsing is split into pure helpers
(``_latest_from_feed`` / ``_latest_from_bluesky``) so they're unit-testable on fixtures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol, cast, runtime_checkable

import feedparser
import httpx

from release_tracker.logging import get_logger
from release_tracker.models import ArtistLink, SocialPlatform
from release_tracker.sources.base import get_json, get_text

log = get_logger("social")

_YT_RSS = "https://www.youtube.com/feeds/videos.xml"
_YT_CHANNEL_RE = re.compile(r"youtube\.com/channel/(UC[\w-]+)")
# the channel's *own* id — the bare "channelId" can be a related/recommended channel,
# so prefer externalId / the canonical /channel/ link.
_YT_EXTERNALID_RE = re.compile(r'"externalId":"(UC[\w-]+)"')
_BSKY_FEED = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
# YouTube serves a stripped page (and an EU consent wall) to non-browser agents.
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _BROWSER_UA}
_YT_PAGE_HEADERS = {"User-Agent": _BROWSER_UA, "Cookie": "SOCS=CAI"}


@dataclass(frozen=True, slots=True)
class Activity:
    """The newest piece of content surfaced for a creator on one platform."""

    title: str
    url: str
    posted_at: date | None


@runtime_checkable
class ActivityFetcher(Protocol):
    platform: SocialPlatform

    async def fetch_latest(
        self, client: httpx.AsyncClient, link: ArtistLink
    ) -> Activity | None: ...


# --- pure parsers (testable on fixtures) ----------------------------------
def _struct_to_date(parsed: Any) -> date | None:
    try:
        return date(parsed.tm_year, parsed.tm_mon, parsed.tm_mday)
    except (AttributeError, TypeError, ValueError):
        return None


def latest_from_feed(content: str) -> Activity | None:
    """Newest entry of an RSS/Atom feed (YouTube, Reddit) -> Activity."""
    # feedparser ships no annotations, so `parse` is Unknown under a newer pyright rather
    # than merely untyped. Naming the boundary Any keeps the cast below the only place
    # that claims to know the shape.
    parsed: Any = feedparser.parse(content)
    entries = cast("list[Any]", parsed.entries)
    if not entries:
        return None
    entry = entries[0]
    title = str(getattr(entry, "title", "") or "untitled").strip()
    url = str(getattr(entry, "link", "") or "")
    when = _struct_to_date(getattr(entry, "published_parsed", None)) or _struct_to_date(
        getattr(entry, "updated_parsed", None)
    )
    return Activity(title=title, url=url, posted_at=when) if url else None


def latest_from_bluesky(payload: dict[str, Any], handle: str) -> Activity | None:
    """Newest original post from a getAuthorFeed response (skips reposts)."""
    for item in cast("list[dict[str, Any]]", payload.get("feed", [])):
        if item.get("reason") is not None:  # a repost, not their own content
            continue
        post = cast("dict[str, Any]", item.get("post", {}))
        record = cast("dict[str, Any]", post.get("record", {}))
        uri = str(post.get("uri", ""))
        if not uri:
            continue
        rkey = uri.rsplit("/", 1)[-1]
        author = cast("dict[str, Any]", post.get("author", {}))
        who = str(author.get("handle") or handle)
        text = str(record.get("text", "")).strip()
        when = _parse_iso_date(record.get("createdAt"))
        return Activity(
            title=(text[:90] or "post"),
            url=f"https://bsky.app/profile/{who}/post/{rkey}",
            posted_at=when,
        )
    return None


def _parse_iso_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


# --- URL -> platform identity (pure, no network) --------------------------
def youtube_channel_id(url: str, handle: str | None) -> str | None:
    """Channel id without a network call (``/channel/UC…`` url or a ``UC…`` handle)."""
    if (m := _YT_CHANNEL_RE.search(url)) is not None:
        return m.group(1)
    if handle and handle.startswith("UC"):
        return handle
    return None


def bluesky_actor(link: ArtistLink) -> str:
    if link.handle:
        return link.handle.lstrip("@")
    m = re.search(r"bsky\.app/profile/([^/?]+)", link.url)
    return m.group(1) if m else ""


def reddit_user(link: ArtistLink) -> str:
    if link.handle:
        return link.handle.removeprefix("u/").lstrip("@")
    m = re.search(r"reddit\.com/u(?:ser)?/([^/?]+)", link.url)
    return m.group(1) if m else ""


# --- fetchers -------------------------------------------------------------
class YouTubeFetcher:
    platform = SocialPlatform.YOUTUBE

    async def fetch_latest(self, client: httpx.AsyncClient, link: ArtistLink) -> Activity | None:
        channel_id = youtube_channel_id(link.url, link.handle)
        if channel_id is None:
            # resolve @handle / /c/ / /user/ by scraping the channel page once
            html = await get_text(client, link.url, headers=_YT_PAGE_HEADERS)
            m = _YT_EXTERNALID_RE.search(html) or _YT_CHANNEL_RE.search(html)
            channel_id = m.group(1) if m else None
        if channel_id is None:
            return None
        content = await get_text(client, _YT_RSS, params={"channel_id": channel_id})
        return latest_from_feed(content)


class BlueskyFetcher:
    platform = SocialPlatform.BLUESKY

    async def fetch_latest(self, client: httpx.AsyncClient, link: ArtistLink) -> Activity | None:
        actor = bluesky_actor(link)
        if not actor:
            return None
        payload = cast(
            "dict[str, Any]",
            await get_json(client, _BSKY_FEED, params={"actor": actor, "limit": "10"}),
        )
        return latest_from_bluesky(payload, actor)


class RedditFetcher:
    platform = SocialPlatform.REDDIT

    async def fetch_latest(self, client: httpx.AsyncClient, link: ArtistLink) -> Activity | None:
        user = reddit_user(link)
        if not user:
            return None
        try:  # Reddit frequently 403s unauthenticated clients — best-effort only
            content = await get_text(
                client, f"https://www.reddit.com/user/{user}/submitted.rss", headers=_HEADERS
            )
        except httpx.HTTPError as exc:
            log.info("social.reddit_unavailable", user=user, error=str(exc))
            return None
        return latest_from_feed(content)


_FETCHERS: dict[SocialPlatform, ActivityFetcher] = {
    f.platform: f for f in (YouTubeFetcher(), BlueskyFetcher(), RedditFetcher())
}


def fetcher_for(platform: SocialPlatform) -> ActivityFetcher | None:
    """The activity fetcher for a platform, or None when it's link-only (documented)."""
    return _FETCHERS.get(platform)
