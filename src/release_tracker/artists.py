"""Artist-radar orchestration: follow creators, fetch their latest content, report it.

Artists are PERSON/ORG graph nodes; following one sets ``Node.followed`` and attaches
stratified links (free/paid/auxiliary). Where a fetcher exists (YouTube/Bluesky/Reddit)
we surface their latest drop; otherwise the link is just documented. ``ArtistReport`` is
the ``--json`` contract the `/rd-artist` skill consumes (mirrors ``lookup.RdReport``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date

import httpx

from release_tracker import views
from release_tracker.clock import utc_now, utc_today
from release_tracker.config import Settings, secret
from release_tracker.db import Database
from release_tracker.logging import get_logger
from release_tracker.models import (
    ArtistLink,
    Edge,
    LinkTier,
    Node,
    NodeKind,
    RelationKind,
    SocialPlatform,
    SourceTier,
)
from release_tracker.social import Activity, fetcher_for
from release_tracker.sources.tmdb import FilmCredit, TmdbSource

log = get_logger("artists")

# a PERSON node canonically keyed on TMDB (person:tmdb:7467) — the filmography pivot.
_TMDB_PERSON_ID = re.compile(r"^person:tmdb:(\d+)$")


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


def _resolve_or_create(db: Database, ref: str, kind: NodeKind) -> Node:
    """Find an existing node of the given kind by ref, else create an owned one."""
    matches = [n for n in db.find_nodes(ref) if n.node_kind is kind]
    if matches:
        lowered = ref.lower()
        return next((n for n in matches if n.name.lower() == lowered), matches[0])
    node = Node.create(kind, ref, owned=True)
    db.upsert_node(node)
    return db.get_node(node.id) or node


def add_membership(db: Database, person_ref: str, group_ref: str) -> tuple[Node, Node]:
    """Record that a person is a member of a group/studio/band (person -> org edge).

    Creates either node if missing; membership is a structural fact independent of
    whether you *follow* the person — `rdt artist follow` is separate.
    """
    person = _resolve_or_create(db, person_ref, NodeKind.PERSON)
    group = _resolve_or_create(db, group_ref, NodeKind.ORG)
    db.upsert_edge(
        Edge(
            src_id=person.id,
            dst_id=group.id,
            relation=RelationKind.MEMBER_OF,
            source_provider="user",
            source_tier=SourceTier.OFFICIAL,
            confidence=1.0,
            owned=True,
        )
    )
    return person, group


# --- filmography: a film/TV creator's canonical pipeline ------------------
def _tmdb_person_id(node: Node) -> str | None:
    """The TMDB person id a node pivots on, if it's canonically keyed there."""
    match = _TMDB_PERSON_ID.match(node.id)
    return match.group(1) if match else None


def _filmography_link(node: Node) -> ArtistLink | None:
    """Auto-derive the FREE filmography pipeline from a person's canonical TMDB id.

    No web search needed — unlike a feed, a film/TV creator's primary output is their
    body of work, which we already know the canonical key for the moment we resolved them.
    """
    person_id = _tmdb_person_id(node)
    if person_id is None or node.node_kind is not NodeKind.PERSON:
        return None
    return ArtistLink(
        node_id=node.id,
        platform=SocialPlatform.FILMOGRAPHY,
        tier=LinkTier.FREE,
        url=f"https://www.themoviedb.org/person/{person_id}",
        handle=person_id,
    )


def ensure_filmography_link(db: Database, node: Node) -> None:
    """Attach the derived filmography pipeline if the person has one and it's missing.

    Idempotent — runs on both follow and refresh, so a film/TV creator followed before
    this pipeline existed gets backfilled the next time their radar is touched.
    """
    link = _filmography_link(node)
    if link is None:
        return
    if not any(existing.platform is link.platform for existing in db.iter_artist_links(node.id)):
        db.upsert_artist_link(link)


_SLATE_LIMIT = 8


def partition_filmography(
    credits: tuple[FilmCredit, ...], today: date, *, limit: int = _SLATE_LIMIT
) -> tuple[FilmCredit | None, tuple[FilmCredit, ...]]:
    """Split a filmography into (latest released, soonest-first upcoming slate).

    Pure — undated credits drop out of both halves (they can't be placed on the calendar).
    """
    released = [c for c in credits if c.when and c.when <= today]
    upcoming = sorted(
        (c for c in credits if c.when and c.when > today), key=lambda c: c.when or today
    )
    latest = max(released, key=lambda c: c.when or today) if released else None
    return latest, tuple(upcoming[:limit])


async def _split_filmography(
    client: httpx.AsyncClient, settings: Settings, person_id: str, today: date
) -> tuple[FilmCredit | None, tuple[FilmCredit, ...]]:
    """(latest released, upcoming slate) for a person's filmography — one TMDB call."""
    key = secret(settings.tmdb_api_key)
    if not key:
        return None, ()
    credits = await TmdbSource().person_credits(client, key, person_id)
    return partition_filmography(credits, today)


async def _resolve_artist_node(
    client: httpx.AsyncClient, db: Database, name: str, kind: NodeKind, settings: Settings | None
) -> Node:
    """Find an existing graph node, else mint one — resolving a person onto their TMDB id.

    Keying a new creator on their canonical ``person:tmdb:<id>`` (rather than a name slug)
    is what lets the filmography pipeline auto-attach for someone you don't already track.
    Falls back to a name-slug node if there's no key, no match, or TMDB is unavailable.
    """
    existing = find_artist(db, name)
    if existing is not None:
        return existing
    key = secret(settings.tmdb_api_key) if settings is not None else None
    if kind is NodeKind.PERSON and key:
        try:
            person_id = await TmdbSource().search_person(client, key, name)
        except Exception as exc:  # a TMDB hiccup must not block following the creator
            log.warning("artists.person_resolve_error", name=name, error=str(exc))
            person_id = None
        if person_id is not None:
            node = Node.create(
                NodeKind.PERSON, name, source="tmdb", source_id=person_id, owned=True, followed=True
            )
            return db.get_node(node.id) or node  # collapse onto an existing tmdb node if present
    return Node.create(kind, name, owned=True, followed=True)


async def add_artist(
    client: httpx.AsyncClient,
    db: Database,
    name: str,
    *,
    kind: NodeKind = NodeKind.PERSON,
    links: list[LinkSpec],
    fetch: bool = True,
    settings: Settings | None = None,
    today: date | None = None,
) -> Node:
    """Follow an artist (reusing an existing graph node if one matches) + attach links."""
    node = await _resolve_artist_node(client, db, name, kind, settings)
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
    # a film/TV person's body of work is a free pipeline we can derive from their id
    ensure_filmography_link(db, node)
    if fetch:
        await refresh_artist(client, db, node, settings=settings, today=today)
    return node


async def refresh_artist(
    client: httpx.AsyncClient,
    db: Database,
    node: Node,
    *,
    settings: Settings | None = None,
    today: date | None = None,
) -> int:
    """Re-fetch the latest content for each fetchable link. Returns how many updated."""
    ensure_filmography_link(db, node)  # backfill creators followed before this pipeline existed
    updated = 0
    for link in db.iter_artist_links(node.id):
        activity = await _latest_for(client, node, link, settings, today)
        if activity is not None:
            db.update_link_activity(
                node.id,
                link.platform,
                title=activity.title,
                url=activity.url,
                posted_at=activity.posted_at,
                fetched_at=utc_now(),
            )
            updated += 1
    return updated


async def _latest_for(
    client: httpx.AsyncClient,
    node: Node,
    link: ArtistLink,
    settings: Settings | None,
    today: date | None,
) -> Activity | None:
    """The newest drop on one link — filmography's latest release, or a feed's latest post."""
    try:
        if link.platform is SocialPlatform.FILMOGRAPHY:
            person_id = _tmdb_person_id(node)
            if person_id is None or settings is None:
                return None
            latest, _ = await _split_filmography(client, settings, person_id, today or _utc_today())
            if latest is None:
                return None
            return Activity(
                title=f"{latest.role}: {latest.title}", url=latest.url, posted_at=latest.when
            )
        fetcher = fetcher_for(link.platform)
        if fetcher is None:
            return None
        return await fetcher.fetch_latest(client, link)
    except Exception as exc:  # a flaky external endpoint must not abort the other links
        log.warning("artists.fetch_error", platform=link.platform.value, error=str(exc))
        return None


def _utc_today() -> date:
    return utc_today()


# --- the /rd-artist JSON contract -----------------------------------------
@dataclass(frozen=True, slots=True)
class SlateItem:
    """An upcoming credit on a creator's filmography — the discovery payoff.

    ``tracked`` marks whether the work is already in the local tracker, so the skill can
    diff "of their slate, here's what you don't follow yet" and offer a /rd-add hook.
    """

    title: str
    kind: str  # "movie" | "tv"
    role: str
    when: date | None
    url: str
    tracked: bool


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
    slate: tuple[SlateItem, ...] = ()  # upcoming filmography (film/TV creators)
    members: tuple[Node, ...] = ()  # for a group: its people
    groups: tuple[Node, ...] = ()  # for a person: their groups
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
            "slate": [
                {
                    "title": s.title,
                    "kind": s.kind,
                    "role": s.role,
                    "when": s.when.isoformat() if s.when else None,
                    "url": s.url,
                    "tracked": s.tracked,
                }
                for s in self.slate
            ],
            "members": [{"name": n.name, "node_id": n.id} for n in self.members],
            "groups": [{"name": n.name, "node_id": n.id} for n in self.groups],
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
        members=tuple(views.members_of(db, node)),
        groups=tuple(views.groups_of(db, node)),
        _freshness=freshness,
    )


async def build_report_live(
    client: httpx.AsyncClient,
    db: Database,
    node: Node | None,
    query: str,
    settings: Settings,
    today: date,
) -> ArtistReport:
    """``build_report`` enriched with the upcoming-filmography slate (one TMDB call).

    The slate is the artist-first payoff for film/TV creators — their canonical pipeline
    is a release calendar, not a feed — so it's computed live rather than stored.
    """
    report = build_report(db, node, query, settings, today)
    person_id = _tmdb_person_id(node) if node is not None else None
    if node is None or person_id is None:
        return report
    try:
        _, upcoming = await _split_filmography(client, settings, person_id, today)
    except Exception as exc:  # a transient TMDB hiccup must not sink the whole card
        log.warning("artists.slate_error", node=node.id, error=str(exc))
        return report
    if not upcoming:
        return report
    tracked_tmdb = {
        w.entity.external_ids.get("tmdb")
        for w in report.tracked_works
        if w.entity.external_ids.get("tmdb")
    }
    slate = tuple(
        SlateItem(
            title=c.title,
            kind=c.media,
            role=c.role,
            when=c.when,
            url=c.url,
            tracked=c.tmdb_id in tracked_tmdb,
        )
        for c in upcoming
    )
    return replace(report, slate=slate)
