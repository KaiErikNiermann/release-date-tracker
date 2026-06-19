"""Tests for the social activity fetchers — pure parsers on fixtures (no network)."""

from __future__ import annotations

from datetime import date

from release_tracker.models import ArtistLink, LinkTier, SocialPlatform
from release_tracker.social import (
    bluesky_actor,
    fetcher_for,
    latest_from_bluesky,
    latest_from_feed,
    reddit_user,
    youtube_channel_id,
)

_YT_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Acme Channel</title>
  <entry>
    <title>My Latest Video</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=abc123"/>
    <published>2026-06-10T12:00:00+00:00</published>
  </entry>
  <entry>
    <title>An Older Video</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=old"/>
    <published>2026-01-01T12:00:00+00:00</published>
  </entry>
</feed>
"""

_BSKY = {
    "feed": [
        {
            "reason": {"$type": "app.bsky.feed.defs#reasonRepost"},
            "post": {"uri": "at://x/y/repost"},
        },
        {
            "post": {
                "uri": "at://did:plc:abc/app.bsky.feed.post/3kpost",
                "author": {"handle": "artist.bsky.social"},
                "record": {"text": "new drawing is up!", "createdAt": "2026-06-12T08:00:00Z"},
            }
        },
    ]
}


def test_latest_from_feed_picks_newest_entry() -> None:
    act = latest_from_feed(_YT_ATOM)
    assert act is not None
    assert act.title == "My Latest Video"
    assert act.url == "https://www.youtube.com/watch?v=abc123"
    assert act.posted_at == date(2026, 6, 10)


def test_latest_from_feed_empty() -> None:
    assert latest_from_feed("<feed xmlns='http://www.w3.org/2005/Atom'></feed>") is None


def test_latest_from_bluesky_skips_reposts() -> None:
    act = latest_from_bluesky(_BSKY, "artist.bsky.social")
    assert act is not None
    assert act.title == "new drawing is up!"
    assert act.url == "https://bsky.app/profile/artist.bsky.social/post/3kpost"
    assert act.posted_at == date(2026, 6, 12)


def _link(url: str, platform: SocialPlatform) -> ArtistLink:
    return ArtistLink(node_id="n", platform=platform, tier=LinkTier.AUXILIARY, url=url)


def test_identity_extraction_from_urls() -> None:
    # /channel/UC... resolves with no network call
    assert youtube_channel_id("https://www.youtube.com/channel/UCabc123", None) == "UCabc123"
    assert youtube_channel_id("https://www.youtube.com/@handle", None) is None  # needs a fetch
    assert (
        bluesky_actor(_link("https://bsky.app/profile/foo.bsky.social", SocialPlatform.BLUESKY))
        == "foo.bsky.social"
    )
    assert reddit_user(_link("https://www.reddit.com/user/spez", SocialPlatform.REDDIT)) == "spez"


def test_registry_only_has_implemented_fetchers() -> None:
    assert fetcher_for(SocialPlatform.YOUTUBE) is not None
    assert fetcher_for(SocialPlatform.BLUESKY) is not None
    assert fetcher_for(SocialPlatform.REDDIT) is not None
    assert fetcher_for(SocialPlatform.PATREON) is None  # link-only
    assert fetcher_for(SocialPlatform.SPOTIFY) is None
