"""UI-agnostic read models over the entity + graph store.

The CLI renders these with rich; a future web/d3 frontend can call the same
functions. Nothing here touches a terminal — it returns typed rows so the
"compact observability" surface (everything sorted by release date, with
who/where/what attached) has one place to evolve.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from release_tracker.db import Database
from release_tracker.models import (
    BestEstimate,
    Certainty,
    CreditRole,
    DatePrecision,
    DescriptorKind,
    Entity,
    MediaKind,
    Node,
    RelationKind,
    ReleaseChannel,
    SourceTier,
)
from release_tracker.resolve import best_estimates

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
class UpcomingRow:
    """A single line of the date-sorted overview: the headline date + who/where/what."""

    entity_id: str
    title: str
    kind: MediaKind
    when: date | None
    precision: DatePrecision
    confidence: float
    confirmed: bool
    who: tuple[str, ...]
    where: tuple[str, ...]
    what: tuple[TagLine, ...]


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


def next_release(db: Database, entity_id: str, today: date) -> BestEstimate | None:
    """The soonest *upcoming* milestone (date >= today).

    Picking the soonest future date — not the global earliest — means a film that
    already had a festival premiere but releases widely next month still reads as
    upcoming, with the wide date as its headline.
    """
    future = [
        e
        for e in best_estimates(db.iter_observations(entity_id))
        if e.release_date and e.release_date >= today
    ]
    return min(future, key=lambda e: e.release_date or date.max) if future else None


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
    *,
    days: int | None = None,
    kind: MediaKind | None = None,
    who_limit: int = 2,
    where_limit: int = 2,
    what_limit: int = 4,
) -> list[UpcomingRow]:
    """Date-sorted rows for works releasing on/after ``today`` (optionally within ``days``)."""
    rows: list[UpcomingRow] = []
    for entity in db.iter_entities():
        if kind is not None and entity.kind is not kind:
            continue
        est = next_release(db, entity.id, today)
        if est is None or est.release_date is None:
            continue
        if days is not None and (est.release_date - today).days > days:
            continue
        credits = _credit_lines(db, entity.id)
        tags = _tag_lines(db, entity.id)
        rows.append(
            UpcomingRow(
                entity_id=entity.id,
                title=entity.title,
                kind=entity.kind,
                when=est.release_date,
                precision=est.precision,
                confidence=est.confidence,
                confirmed=est.certainty is Certainty.CONFIRMED,
                # dedupe by name (a person credited twice, e.g. director+writer)
                who=tuple(dict.fromkeys(c.name for c in credits))[:who_limit],
                where=tuple(p.name for p in _platform_lines(db, entity.id)[:where_limit]),
                what=tuple(tags[:what_limit]),
            )
        )
    rows.sort(key=lambda r: (r.when or date.max, -r.confidence))
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
