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
    WorkRelation,
)
from release_tracker.resolve import PRECISION_RANK, best_estimates

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
    ``pivot_when`` is the display/upcoming date (it may fall back across channels so a
    theatrical-only film still surfaces). ``available_when`` is stricter: the date on
    the user's *consumption* channel with no film fallback — a theatrical release does
    not make a digitally-consumed film available. Only a confirmed, elapsed
    ``available_when`` makes a work actually "available".
    """

    entity_id: str
    title: str
    kind: MediaKind
    theatrical: DateCell | None
    digital: DateCell | None
    pivot_when: date | None
    pivot_confirmed: bool
    available_when: date | None
    available_confirmed: bool
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
    derived_from: tuple[RelatedWork, ...] = ()  # what it descends from
    derivatives: tuple[RelatedWork, ...] = ()  # what descends from it


@dataclass(frozen=True, slots=True)
class SeasonEntry:
    """One tracked season/part of a series, for the `seasons` walk."""

    entity: Entity
    season: int | None
    part: int | None  # mid-season cut (Part/Volume/Cour N), if any
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
    """Best dated estimate matching ``channels`` (any if None), preferring ``region``.

    Confirmed dates win over speculative ones: a wishlist/early *estimated* date must
    not shadow the real *confirmed* release (e.g. a game with a speculative early-access
    guess and a confirmed launch). Within the surviving pool, the most *precise* date
    wins before the soonest — a coarse year-date's materialized day (Jan 1 / Dec 31) is
    an artifact, so a known month/day must not be shadowed by a vague "2026". Only fall
    back to speculative when nothing is confirmed.
    """
    cands = [e for e in estimates if e.release_date and (channels is None or e.channel in channels)]
    if region is not None:
        cands = [e for e in cands if e.region == region] or cands  # fall back to any region
    confirmed = [e for e in cands if e.certainty is Certainty.CONFIRMED]
    pool = confirmed or cands
    if not pool:
        return None
    finest = max(PRECISION_RANK[e.precision] for e in pool)
    pool = [e for e in pool if PRECISION_RANK[e.precision] == finest]
    return min(pool, key=lambda e: e.release_date or date.max)


def _cell(est: BestEstimate | None) -> DateCell | None:
    if est is None:
        return None
    return DateCell(est.release_date, est.precision, est.certainty is Certainty.CONFIRMED)


def _pivot(
    theatrical: BestEstimate | None, digital: BestEstimate | None, channel: str
) -> BestEstimate | None:
    """The display/upcoming date per the configured channel.

    Falls back across channels so a film with only a theatrical date still surfaces in
    the row and in ``upcoming`` — availability is gated separately by :func:`_consumption`.
    """
    if channel == "theatrical":
        return theatrical or digital
    if channel == "digital":
        return digital or theatrical
    dated = [e for e in (theatrical, digital) if e and e.release_date]
    return min(dated, key=lambda e: e.release_date or date.max) if dated else None


def _consumption(
    theatrical: BestEstimate | None, digital: BestEstimate | None, channel: str
) -> BestEstimate | None:
    """The estimate on the user's *consumption* channel — what makes a work "available".

    Unlike :func:`_pivot` this does not fall a film back to theatrical: if you consume
    digitally, a theatrical-only release is not yet available to you. For tv/games the
    single release is carried as ``digital`` (``theatrical`` is ``None``), so it governs
    under either preference; the ``theatrical`` branch keeps that fallback for them.
    """
    if channel == "digital":
        return digital
    if channel == "theatrical":
        return theatrical or digital
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
    consume = _consumption(theatrical, digital, settings.availability_channel)
    credits = _credit_lines(db, entity.id)
    return TrackRow(
        entity_id=entity.id,
        title=entity.title,
        kind=entity.kind,
        theatrical=_cell(theatrical),
        digital=_cell(digital),
        pivot_when=pivot.release_date if pivot else None,
        pivot_confirmed=pivot is not None and pivot.certainty is Certainty.CONFIRMED,
        available_when=consume.release_date if consume else None,
        available_confirmed=consume is not None and consume.certainty is Certainty.CONFIRMED,
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
        CreditLine(
            e.role if isinstance(e.role, CreditRole) else CreditRole.OTHER, n.name, n.id, n.owned
        )
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
# the three consumption buckets are exhaustive + disjoint, so nothing falls into limbo:
#   finished (watched/dropped) -> `watched`;  out + active -> `available`;
#   everything else not-finished -> `upcoming` (dated, or an explicit "no date yet").
_FINISHED = (ConsumptionState.WATCHED, ConsumptionState.DROPPED)
_ACTIVE = (ConsumptionState.WANT, ConsumptionState.WATCHING)


def _is_available(r: TrackRow, today: date) -> bool:
    """Out and unfinished: a *confirmed* consumption-channel date has elapsed, still active.

    A speculative past date does not count (we don't know it released), and neither does a
    theatrical release under a digital preference — only the strict ``available_when`` governs.
    """
    return (
        r.available_when is not None
        and r.available_when < today
        and r.available_confirmed
        and r.state in _ACTIVE
    )


def _has_firm_upcoming_date(r: TrackRow, today: date) -> bool:
    """A real future date to schedule on (vs. a stale past guess or no date at all)."""
    return r.pivot_when is not None and r.pivot_when >= today


def _upcoming_sort_key(r: TrackRow, today: date) -> date:
    """Firm future date for ordering; no-firm-date rows sort to the TBA tail."""
    if r.pivot_when is not None and r.pivot_when >= today:
        return r.pivot_when
    return date.max


def upcoming(
    db: Database,
    today: date,
    settings: Settings,
    *,
    days: int | None = None,
    kind: MediaKind | None = None,
) -> list[TrackRow]:
    """Anything not finished and not yet available — i.e. still ahead of you.

    Future-dated first (soonest first), then an explicit **no-date** tail: not-finished
    works with no firm future date (none at all, or only a stale past guess). A title can't
    be "neither available nor upcoming" — if it isn't out and isn't watched, it's upcoming.
    The ``days`` window only applies to firm-dated rows; the no-date tail is dropped when set.
    """
    rows: list[TrackRow] = []
    for r in _track_rows(db, today, settings, kind=kind):
        if r.state in _FINISHED or _is_available(r, today):
            continue
        if _has_firm_upcoming_date(r, today):
            if r.pivot_when is not None and (days is None or (r.pivot_when - today).days <= days):
                rows.append(r)
        elif days is None:  # the no-date tail only shows on an unbounded view
            rows.append(r)
    rows.sort(key=lambda r: _upcoming_sort_key(r, today))
    return rows


def available(
    db: Database,
    today: date,
    settings: Settings,
    *,
    kind: MediaKind | None = None,
) -> list[TrackRow]:
    """Works that are out and unfinished (want/watching), newest first."""
    rows = [r for r in _track_rows(db, today, settings, kind=kind) if _is_available(r, today)]
    rows.sort(key=lambda r: r.available_when or date.min, reverse=True)
    return rows


def watched(
    db: Database,
    today: date,
    settings: Settings,
    *,
    kind: MediaKind | None = None,
) -> list[TrackRow]:
    """Works you're done with (watched/dropped), most recently released first."""
    rows = [r for r in _track_rows(db, today, settings, kind=kind) if r.state in _FINISHED]
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
        derived_from=tuple(derived_from(db, entity.id)),
        derivatives=tuple(derivatives_of(db, entity.id)),
    )


def seasons_of_series(db: Database, series_node: Node) -> list[SeasonEntry]:
    """Tracked seasons/parts of a series, ordered by season number (the `seasons` walk)."""
    out: list[SeasonEntry] = []
    for edge in db.edges_to(series_node.id, RelationKind.PART_OF_SERIES):
        work = db.get_entity(edge.src_id)
        if work is not None:
            out.append(
                SeasonEntry(
                    work,
                    edge.ordinal,
                    edge.part,
                    _earliest_date(db, work.id),
                    work_is_owned(db, work.id),
                )
            )
    out.sort(key=lambda s: (s.season is None, s.season or 0, s.part or 0, s.entity.title))
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
                CreditedWork(
                    work,
                    edge.role if isinstance(edge.role, CreditRole) else CreditRole.OTHER,
                    work_is_owned(db, work.id),
                )
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
    profile_url: str | None = None  # the direct profile to open (skips the recommender feed)
    latest_url: str | None = None  # the newest drop itself (the recently-active source)


def artists(
    db: Database, today: date, settings: Settings, *, sort: str = "recency"
) -> list[ArtistRow]:
    """The followed-creator radar. ``sort`` is 'recency' (newest content first) or 'name'."""
    rows: list[ArtistRow] = []
    for node in db.followed_artists():
        links = db.iter_artist_links(node.id)
        dated = [(link, link.last_post_at) for link in links if link.last_post_at is not None]
        recent = max(dated, key=lambda p: p[1]) if dated else None
        primary = recent[0] if recent else None
        last_post = (primary.platform, recent[1]) if primary and recent else None
        # the profile to open: prefer the recently-active source, else a free pipeline, else any
        profile = primary or next((lk for lk in links if lk.tier is LinkTier.FREE), None)
        profile = profile or (links[0] if links else None)
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
                profile_url=profile.url if profile else None,
                latest_url=primary.latest_url if primary else None,
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


# --- cross-media lineage (derived_from) -----------------------------------
@dataclass(frozen=True, slots=True)
class RelatedWork:
    """One end of a DERIVED_FROM edge, with the relation that links them."""

    node: Node
    relation: WorkRelation


def derived_from(db: Database, node_id: str) -> list[RelatedWork]:
    """What this work descends from: adaptation source, parent show, original, ..."""
    edges = db.edges_from(node_id, RelationKind.DERIVED_FROM)
    nodes = db.get_nodes(e.dst_id for e in edges)
    return [
        RelatedWork(n, e.role)
        for e in edges
        if isinstance(e.role, WorkRelation) and (n := nodes.get(e.dst_id))
    ]


def derivatives_of(db: Database, node_id: str) -> list[RelatedWork]:
    """What descends from this work: spinoffs, sequels, adaptations, tie-ins, ..."""
    edges = db.edges_to(node_id, RelationKind.DERIVED_FROM)
    nodes = db.get_nodes(e.src_id for e in edges)
    return [
        RelatedWork(n, e.role)
        for e in edges
        if isinstance(e.role, WorkRelation) and (n := nodes.get(e.src_id))
    ]
