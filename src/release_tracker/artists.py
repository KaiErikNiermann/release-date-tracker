"""Artist-radar orchestration: follow creators, fetch their latest content, report it.

Artists are PERSON/ORG graph nodes; following one sets ``Node.followed`` and attaches
stratified links (free/paid/auxiliary). Where a fetcher exists (YouTube/Bluesky/Reddit)
we surface their latest drop; otherwise the link is just documented. ``ArtistReport`` is
the ``--json`` contract the `/rd-artist` skill consumes (mirrors ``lookup.RdReport``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx

from release_tracker import views
from release_tracker.config import Settings
from release_tracker.db import Database
from release_tracker.logging import get_logger
from release_tracker.models import ArtistLink, LinkTier, Node, NodeKind, SocialPlatform
from release_tracker.social import fetcher_for

log = get_logger("artists")


@dataclass(frozen=True, slots=True)
class LinkSpec:
    """A parsed ``platform:tier:url`` CLI token."""

    platform: SocialPlatform
    tier: LinkTier
    url: str
    handle: str | None = None


_TIER_ALIAS = {"aux": LinkTier.AUXILIARY, "auxiliary": LinkTier.AUXILIARY}


def parse_link_spec(token: str) -> LinkSpec:
    """Parse ``youtube:free:https://youtube.com/@x`` (url may contain colons)."""
    parts = token.split(":", 2)
    if len(parts) != 3 or not parts[2]:
        msg = f"expected platform:tier:url, got '{token}'"
        raise ValueError(msg)
    try:
        platform = SocialPlatform(parts[0].strip().lower())
    except ValueError:
        platform = SocialPlatform.OTHER
    tier_raw = parts[1].strip().lower()
    tier = _TIER_ALIAS.get(tier_raw) or LinkTier(tier_raw)  # raises on a bad tier (intended)
    return LinkSpec(platform, tier, parts[2].strip())


def find_artist(db: Database, ref: str) -> Node | None:
    """Resolve an artist reference to an existing PERSON/ORG node (prefers exact name)."""
    nodes = [n for n in db.find_nodes(ref) if n.node_kind in (NodeKind.PERSON, NodeKind.ORG)]
    if not nodes:
        return None
    lowered = ref.lower()
    return next((n for n in nodes if n.name.lower() == lowered), nodes[0])


async def add_artist(
    client: httpx.AsyncClient,
    db: Database,
    name: str,
    *,
    kind: NodeKind = NodeKind.PERSON,
    links: list[LinkSpec],
    fetch: bool = True,
) -> Node:
    """Follow an artist (reusing an existing graph node if one matches) + attach links."""
    node = find_artist(db, name) or Node.create(kind, name, owned=True, followed=True)
    db.upsert_node(node.model_copy(update={"owned": True, "followed": True}))
    node = db.get_node(node.id) or node
    for spec in links:
        db.upsert_artist_link(
            ArtistLink(
                node_id=node.id,
                platform=spec.platform,
                tier=spec.tier,
                url=spec.url,
                handle=spec.handle,
            )
        )
    if fetch:
        await refresh_artist(client, db, node)
    return node


async def refresh_artist(client: httpx.AsyncClient, db: Database, node: Node) -> int:
    """Re-fetch the latest content for each fetchable link. Returns how many updated."""
    updated = 0
    for link in db.iter_artist_links(node.id):
        fetcher = fetcher_for(link.platform)
        if fetcher is None:
            continue
        try:
            activity = await fetcher.fetch_latest(client, link)
        except Exception as exc:  # a flaky social endpoint must not abort the others
            log.warning("artists.fetch_error", platform=link.platform.value, error=str(exc))
            continue
        if activity is not None:
            db.update_link_activity(
                node.id,
                link.platform,
                title=activity.title,
                url=activity.url,
                posted_at=activity.posted_at,
                fetched_at=datetime.now(UTC),
            )
            updated += 1
    return updated


# --- the /rd-artist JSON contract -----------------------------------------
@dataclass(frozen=True, slots=True)
class ArtistReport:
    """Everything the /rd-artist skill needs to render one creator."""

    query: str
    found: bool
    name: str | None = None
    node_id: str | None = None
    kind: str | None = None
    links: tuple[ArtistLink, ...] = ()
    tracked_works: tuple[views.CreditedWork, ...] = ()
    notes: tuple[str, ...] = ()
    _freshness: tuple[str | None, ...] = ()  # parallel to links

    def to_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "found": self.found,
            "name": self.name,
            "node_id": self.node_id,
            "kind": self.kind,
            "links": [
                {
                    "platform": link.platform.value,
                    "tier": link.tier.value,
                    "url": link.url,
                    "handle": link.handle,
                    "latest": (
                        {
                            "title": link.latest_title,
                            "url": link.latest_url,
                            "posted_at": link.last_post_at.isoformat()
                            if link.last_post_at
                            else None,
                        }
                        if link.latest_url
                        else None
                    ),
                    "freshness": fresh,
                }
                for link, fresh in zip(self.links, self._freshness, strict=False)
            ],
            "tracked_works": [
                {"title": w.entity.title, "kind": w.entity.kind.value, "role": w.role.value}
                for w in self.tracked_works
            ],
            "notes": list(self.notes),
        }


def build_report(
    db: Database, node: Node | None, query: str, settings: Settings, today: date
) -> ArtistReport:
    if node is None:
        return ArtistReport(query=query, found=False, notes=("No such followed artist.",))
    links = tuple(db.iter_artist_links(node.id))
    freshness = tuple(views.freshness(link.fetched_at, today, settings) for link in links)
    return ArtistReport(
        query=query,
        found=True,
        name=node.name,
        node_id=node.id,
        kind=node.node_kind.value,
        links=links,
        tracked_works=tuple(views.works_by_node(db, node)),
        _freshness=freshness,
    )
