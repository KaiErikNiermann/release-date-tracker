"""UI-agnostic read models over the entity + graph store.

The CLI renders these with rich; a future web/d3 frontend can call the same
functions. Nothing here touches a terminal — it returns typed rows so the
"compact observability" surface (everything sorted by release date, with
who/where/what attached) has one place to evolve.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from release_tracker.config import Settings
from release_tracker.db import Database
from release_tracker.models import (
    BestEstimate,
    Certainty,
    ConsumptionState,
    CreditRole,
    DatePrecision,
    DescriptorKind,
    Entity,
    LinkTier,
    MediaKind,
    Node,
    NodeKind,
    RelationKind,
    ReleaseChannel,
    SocialPlatform,
    SourceTier,
)
from release_tracker.resolve import best_estimates

Freshness = Literal["fresh", "aging", "stale"]
_THEATRICAL = (
    ReleaseChannel.THEATRICAL,
    ReleaseChannel.THEATRICAL_LIMITED,
    ReleaseChannel.PREMIERE,
)

# Role priority for picking a work's defining creator(s) — the trustworthy fixture
# (director/creator/dev) leads; cast trails. Lower sorts first.
_ROLE_RANK: dict[CreditRole, int] = {
    CreditRole.DIRECTOR: 0,
    CreditRole.CREATOR: 0,
    CreditRole.DEVELOPER: 0,
    CreditRole.SHOWRUNNER: 1,
    CreditRole.AUTHOR: 1,
    CreditRole.HOST: 1,
    CreditRole.ARTIST: 2,
    CreditRole.WRITER: 3,
    CreditRole.COMPOSER: 4,
    CreditRole.PUBLISHER: 5,
    CreditRole.STUDIO: 5,
    CreditRole.ANIMATION_STUDIO: 5,
    CreditRole.NETWORK: 5,
    CreditRole.VOICE: 8,
    CreditRole.CAST: 9,
    CreditRole.OTHER: 7,
}


@dataclass(frozen=True, slots=True)
class CreditLine:
    """One who-edge, resolved to a name (a credited person/org)."""

    role: CreditRole
    name: str
    node_id: str
    owned: bool


@dataclass(frozen=True, slots=True)
class TagLine:
    """One what-edge: a descriptor with its trust (sourced genre vs model theme)."""

    name: str
    kind: DescriptorKind
    predicted: bool  # True => model-derived (a flagged hypothesis, not a fact)


@dataclass(frozen=True, slots=True)
class PlatformLine:
    """One where-edge: a consumption platform, possibly only a prediction."""

    name: str
    predicted: bool


@dataclass(frozen=True, slots=True)
class DateCell:
    """A single release milestone: when, how precise, and confirmed vs speculative."""

    when: date | None
    precision: DatePrecision
    confirmed: bool


@dataclass(frozen=True, slots=True)
class TrackRow:
    """A row of the upcoming/available surface: dual dates + who/where/what + state.

    ``theatrical`` is movie-only (region-scoped); ``digital`` is the "when can I
    actually watch it" date (digital for movies, the single release for tv/games).
    ``pivot_when`` is the date that governs availability per the configured channel.
    """

    entity_id: str
    title: str
    kind: MediaKind
    theatrical: DateCell | None
    digital: DateCell | None
    pivot_when: date | None
    who: tuple[str, ...]
    where: tuple[str, ...]
    what: tuple[TagLine, ...]
    freshness: Freshness | None
    has_notes: bool
    state: ConsumptionState


@dataclass(frozen=True, slots=True)
class WorkCard:
    """Everything known about one work: dates + the full who/where/what graph."""

    entity: Entity
    estimates: tuple[BestEstimate, ...]
    credits: tuple[CreditLine, ...]
    platforms: tuple[PlatformLine, ...]
    tags: tuple[TagLine, ...]
    series: tuple[str, ...] = field(default_factory=tuple)
    season: int | None = None  # this work's season/part number within its series


@dataclass(frozen=True, slots=True)
class SeasonEntry:
    """One tracked season/part of a series, for the `seasons` walk."""

    entity: Entity
    season: int | None
    when: date | None
    owned: bool


def freshness(fetched_at: datetime | None, today: date, settings: Settings) -> Freshness | None:
    """Green/orange/red bucket for how recently the underlying date was refreshed."""
    if fetched_at is None:
        return None
    age = (today - fetched_at.date()).days
    if age <= settings.fresh_days:
        return "fresh"
    if age <= settings.stale_days:
        return "aging"
    return "stale"


def _pick(
    estimates: Iterable[BestEstimate],
    channels: tuple[ReleaseChannel, ...] | None,
    *,
    region: str | None = None,
) -> BestEstimate | None:
    """Soonest dated estimate matching ``channels`` (any if None), preferring ``region``."""
    cands = [e for e in estimates if e.release_date and (channels is None or e.channel in channels)]
    if region is not None:
        cands = [e for e in cands if e.region == region] or cands  # fall back to any region
    return min(cands, key=lambda e: e.release_date or date.max) if cands else None


def _cell(est: BestEstimate | None) -> DateCell | None:
    if est is None:
        return None
    return DateCell(est.release_date, est.precision, est.certainty is Certainty.CONFIRMED)


def _pivot(
    theatrical: BestEstimate | None, digital: BestEstimate | None, channel: str
) -> BestEstimate | None:
    """The estimate that governs availability, per the configured consumption channel."""
    if channel == "theatrical":
        return theatrical or digital
    if channel == "digital":
        return digital or theatrical
    dated = [e for e in (theatrical, digital) if e and e.release_date]
    return min(dated, key=lambda e: e.release_date or date.max) if dated else None


def _track_row(
    db: Database, entity: Entity, today: date, settings: Settings, has_notes: bool
) -> TrackRow:
    estimates = best_estimates(db.iter_observations(entity.id))
    region = settings.regions[0] if settings.regions else "US"
    if entity.kind is MediaKind.MOVIE:
        theatrical = _pick(estimates, _THEATRICAL, region=region)
        digital = _pick(estimates, (ReleaseChannel.DIGITAL,))
    else:
        theatrical = None
        digital = _pick(estimates, None)  # the single release date
    pivot = _pivot(theatrical, digital, settings.availability_channel)
    credits = _credit_lines(db, entity.id)
    return TrackRow(
        entity_id=entity.id,
        title=entity.title,
        kind=entity.kind,
        theatrical=_cell(theatrical),
        digital=_cell(digital),
        pivot_when=pivot.release_date if pivot else None,
        who=tuple(dict.fromkeys(c.name for c in credits))[:2],
        where=tuple(p.name for p in _platform_lines(db, entity.id)[:2]),
        what=tuple(_tag_lines(db, entity.id)[:4]),
        freshness=freshness(pivot.fetched_at if pivot else None, today, settings),
        has_notes=has_notes,
        state=entity.consumption_state,
    )


def _track_rows(
    db: Database, today: date, settings: Settings, *, kind: MediaKind | None
) -> list[TrackRow]:
    notes = db.note_counts()
    return [
        _track_row(db, e, today, settings, notes.get(e.id, 0) > 0)
        for e in db.iter_entities()
        if kind is None or e.kind is kind
    ]


def _collapse_estimates(estimates: Iterable[BestEstimate]) -> tuple[BestEstimate, ...]:
    """One row per channel (soonest region), so a card shows ~3 lines, not 60."""
    by_channel: dict[ReleaseChannel, BestEstimate] = {}
    for est in estimates:
        cur = by_channel.get(est.channel)
        if cur is None or (est.release_date or date.max) < (cur.release_date or date.max):
            by_channel[est.channel] = est
    return tuple(sorted(by_channel.values(), key=lambda e: e.release_date or date.max))


# --- graph -> resolved lines ----------------------------------------------
def _credit_lines(db: Database, entity_id: str) -> list[CreditLine]:
    edges = db.edges_to(entity_id, RelationKind.CREDITED_ON)
    nodes = db.get_nodes(e.src_id for e in edges)
    lines = [
        CreditLine(e.role or CreditRole.OTHER, n.name, n.id, n.owned)
        for e in edges
        if (n := nodes.get(e.src_id))
    ]
    lines.sort(key=lambda c: (_ROLE_RANK.get(c.role, 7), c.name))
    return lines


def _platform_lines(db: Database, entity_id: str) -> list[PlatformLine]:
    edges = db.edges_from(entity_id, RelationKind.AVAILABLE_ON)
    nodes = db.get_nodes(e.dst_id for e in edges)
    return [
        PlatformLine(n.name, e.source_tier is SourceTier.MODEL)
        for e in edges
        if (n := nodes.get(e.dst_id))
    ]


def _tag_lines(db: Database, entity_id: str) -> list[TagLine]:
    edges = db.edges_from(entity_id, RelationKind.EXHIBITS)
    nodes = db.get_nodes(e.dst_id for e in edges)
    lines = [
        TagLine(
            n.name, n.descriptor_kind or DescriptorKind.GENRE, e.source_tier is SourceTier.MODEL
        )
        for e in edges
        if (n := nodes.get(e.dst_id))
    ]
    # sourced genres first, soft themes after
    lines.sort(key=lambda t: (t.predicted, t.name))
    return lines


def _series_names(db: Database, entity_id: str) -> tuple[str, ...]:
    edges = db.edges_from(entity_id, RelationKind.PART_OF_SERIES)
    nodes = db.get_nodes(e.dst_id for e in edges)
    return tuple(n.name for e in edges if (n := nodes.get(e.dst_id)))


# --- public builders ------------------------------------------------------
def upcoming(
    db: Database,
    today: date,
    settings: Settings,
    *,
    days: int | None = None,
    kind: MediaKind | None = None,
) -> list[TrackRow]:
    """Works whose consumption (pivot) date is still in the future, soonest first."""
    rows = [
        r
        for r in _track_rows(db, today, settings, kind=kind)
        if r.pivot_when is not None
        and r.pivot_when >= today
        and (days is None or (r.pivot_when - today).days <= days)
    ]
    rows.sort(key=lambda r: r.pivot_when or date.max)
    return rows


def available(
    db: Database,
    today: date,
    settings: Settings,
    *,
    kind: MediaKind | None = None,
) -> list[TrackRow]:
    """Works that are out (pivot date passed) and unfinished (want/watching), newest first."""
    watch_states = (ConsumptionState.WANT, ConsumptionState.WATCHING)
    rows = [
        r
        for r in _track_rows(db, today, settings, kind=kind)
        if r.pivot_when is not None and r.pivot_when < today and r.state in watch_states
    ]
    rows.sort(key=lambda r: r.pivot_when or date.min, reverse=True)
    return rows


def _earliest_date(db: Database, entity_id: str) -> date | None:
    dates = [
        e.release_date for e in best_estimates(db.iter_observations(entity_id)) if e.release_date
    ]
    return min(dates) if dates else None


def work_card(db: Database, entity: Entity) -> WorkCard:
    """Full who/where/what + dates for one work (the `card` surface)."""
    series_edges = db.edges_from(entity.id, RelationKind.PART_OF_SERIES)
    season = next((e.ordinal for e in series_edges if e.ordinal is not None), None)
    return WorkCard(
        entity=entity,
        estimates=_collapse_estimates(best_estimates(db.iter_observations(entity.id))),
        credits=tuple(_credit_lines(db, entity.id)),
        platforms=tuple(_platform_lines(db, entity.id)),
        tags=tuple(_tag_lines(db, entity.id)),
        series=_series_names(db, entity.id),
        season=season,
    )


def seasons_of_series(db: Database, series_node: Node) -> list[SeasonEntry]:
    """Tracked seasons/parts of a series, ordered by season number (the `seasons` walk)."""
    out: list[SeasonEntry] = []
    for edge in db.edges_to(series_node.id, RelationKind.PART_OF_SERIES):
        work = db.get_entity(edge.src_id)
        if work is not None:
            out.append(
                SeasonEntry(
                    work, edge.ordinal, _earliest_date(db, work.id), work_is_owned(db, work.id)
                )
            )
    out.sort(key=lambda s: (s.season is None, s.season or 0, s.entity.title))
    return out


@dataclass(frozen=True, slots=True)
class CreditedWork:
    """A work a node is credited on, for the one-hop `who` walk."""

    entity: Entity
    role: CreditRole
    owned: bool


def works_by_node(db: Database, node: Node) -> list[CreditedWork]:
    """Works a person/org is credited on (the first two-hop walk primitive)."""
    out: list[CreditedWork] = []
    for edge in db.edges_from(node.id, RelationKind.CREDITED_ON):
        work = db.get_entity(edge.dst_id)
        if work is not None:
            out.append(
                CreditedWork(work, edge.role or CreditRole.OTHER, work_is_owned(db, work.id))
            )
    out.sort(key=lambda w: w.entity.title)
    return out


def work_is_owned(db: Database, entity_id: str) -> bool:
    node = db.get_node(entity_id)
    return node.owned if node else False


# --- artist radar ---------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ArtistRow:
    """A row of the creator radar: who posted most recently + their pipelines."""

    node_id: str
    name: str
    kind: NodeKind
    last_post: tuple[SocialPlatform, date] | None  # (platform, date) of newest content
    freshness: Freshness | None  # how recently we *checked* (vs when they posted)
    free: tuple[SocialPlatform, ...]
    paid: tuple[SocialPlatform, ...]
    n_works: int


def artists(
    db: Database, today: date, settings: Settings, *, sort: str = "recency"
) -> list[ArtistRow]:
    """The followed-creator radar. ``sort`` is 'recency' (newest content first) or 'name'."""
    rows: list[ArtistRow] = []
    for node in db.followed_artists():
        links = db.iter_artist_links(node.id)
        dated = [(link.platform, link.last_post_at) for link in links if link.last_post_at]
        last_post = max(dated, key=lambda p: p[1]) if dated else None
        fetched = [link.fetched_at for link in links if link.fetched_at]
        rows.append(
            ArtistRow(
                node_id=node.id,
                name=node.name,
                kind=node.node_kind,
                last_post=last_post,
                freshness=freshness(max(fetched) if fetched else None, today, settings),
                free=tuple(link.platform for link in links if link.tier is LinkTier.FREE),
                paid=tuple(link.platform for link in links if link.tier is LinkTier.PAID),
                n_works=len(db.edges_from(node.id, RelationKind.CREDITED_ON)),
            )
        )
    if sort == "name":
        rows.sort(key=lambda r: r.name.lower())
    else:  # recency: newest post first, undated artists last (by name)
        rows.sort(key=lambda r: (r.last_post is None, _neg_ordinal(r.last_post), r.name.lower()))
    return rows


def _neg_ordinal(last_post: tuple[SocialPlatform, date] | None) -> int:
    """Sort key helper: most recent post first (descending date)."""
    return -last_post[1].toordinal() if last_post else 0


def members_of(db: Database, org: Node) -> list[Node]:
    """People who are members of a group/studio/band (the org -> people walk)."""
    ids = [e.src_id for e in db.edges_to(org.id, RelationKind.MEMBER_OF)]
    nodes = db.get_nodes(ids)
    return sorted((nodes[i] for i in ids if i in nodes), key=lambda n: n.name)


def groups_of(db: Database, person: Node) -> list[Node]:
    """Groups/studios/bands a person belongs to (the person -> orgs walk)."""
    ids = [e.dst_id for e in db.edges_from(person.id, RelationKind.MEMBER_OF)]
    nodes = db.get_nodes(ids)
    return sorted((nodes[i] for i in ids if i in nodes), key=lambda n: n.name)
