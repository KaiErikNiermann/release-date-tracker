"""Populate the who/where/what graph for a resolved Work.

Sources-first, per the project ethos: credits / companies / genres / platforms come
from TMDB/IGDB (high-trust, real provenance per edge); only soft *themes* are LLM-
derived, written at ``SourceTier.MODEL`` and ``owned=False`` so they render as
flagged, rejectable hypotheses — never load-bearing.

Each fact becomes one or more `Node`s plus a provenance-tagged `Edge`. Everything is
idempotent (re-enriching refreshes in place) and degrades gracefully when a key or
canonical id is missing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from release_tracker.config import Settings
from release_tracker.db import Database
from release_tracker.logging import get_logger
from release_tracker.models import (
    DescriptorKind,
    Edge,
    Entity,
    MediaKind,
    Node,
    NodeKind,
    RelationKind,
    SourceTier,
)
from release_tracker.platforms import learn_predicted_platform
from release_tracker.sources.base import Credit, MediaGraph, pinned_id
from release_tracker.sources.igdb import IgdbSource
from release_tracker.sources.tmdb import TmdbSource
from release_tracker.titles import split_season

_TV_KINDS = (MediaKind.TV, MediaKind.ANIME)

log = get_logger("enrich")

# (name, predicted?) — predicted homes are written as MODEL-tier edges.
PlatformRef = tuple[str, bool]
_MAX_THEMES = 6


@dataclass(slots=True)
class EnrichSummary:
    """What an enrichment pass wrote, for CLI feedback."""

    resolved: bool = False
    people: int = 0
    orgs: int = 0
    genres: int = 0
    themes: int = 0
    platforms: int = 0
    series: int = 0


def _provider(kind: MediaKind) -> str:
    return "igdb" if kind is MediaKind.GAME else "tmdb"


async def enrich_work(
    client: httpx.AsyncClient,
    db: Database,
    settings: Settings,
    entity: Entity,
    *,
    include_themes: bool = True,
) -> EnrichSummary:
    """Fetch + persist who/where/what for one resolved Work into the graph."""
    db.upsert_node(
        Node(
            id=entity.id,
            node_kind=NodeKind.WORK,
            name=entity.title,
            owned=True,
            external_ids=entity.external_ids,
        )
    )
    graph, platforms = await _fetch(client, settings, entity)
    if graph is None:
        return EnrichSummary(resolved=False)

    now = datetime.now(UTC)
    provider = _provider(entity.kind)
    summary = EnrichSummary(resolved=True)

    for credit in graph.credits:
        _write_credit(db, entity, credit, provider, now)
        if credit.node_kind is NodeKind.PERSON:
            summary.people += 1
        else:
            summary.orgs += 1

    for genre in graph.genres:
        _write_descriptor(
            db, entity, genre, DescriptorKind.GENRE, provider, SourceTier.AGGREGATOR, now
        )
        summary.genres += 1

    if graph.series is not None:
        season = split_season(entity.title)[1] if entity.kind in _TV_KINDS else None
        _write_series(db, entity, graph.series, provider, now, ordinal=season)
        summary.series = 1

    for name, predicted in platforms:
        _write_platform(db, entity, name, predicted, provider, now)
        summary.platforms += 1

    if include_themes:
        for theme in await _llm_themes(settings, entity, graph):
            _write_descriptor(
                db, entity, theme, DescriptorKind.THEME, "openai", SourceTier.MODEL, now
            )
            summary.themes += 1

    log.info("enrich.done", entity=entity.title, **asdict(summary))
    return summary


# --- edge writers (each: ensure node, then provenance-tagged edge) --------
def _write_credit(
    db: Database, entity: Entity, credit: Credit, provider: str, now: datetime
) -> None:
    node = Node.create(
        credit.node_kind, credit.name, source=provider, source_id=credit.source_id, owned=False
    )
    db.upsert_node(node)
    db.upsert_edge(
        Edge(
            src_id=node.id,
            dst_id=entity.id,
            relation=RelationKind.CREDITED_ON,
            role=credit.role,
            source_provider=provider,
            source_tier=SourceTier.AGGREGATOR,
            confidence=0.85,
            fetched_at=now,
        )
    )


def _write_descriptor(
    db: Database,
    entity: Entity,
    name: str,
    kind: DescriptorKind,
    provider: str,
    tier: SourceTier,
    now: datetime,
) -> None:
    node = Node.create(NodeKind.DESCRIPTOR, name, descriptor_kind=kind, owned=False)
    db.upsert_node(node)
    db.upsert_edge(
        Edge(
            src_id=entity.id,
            dst_id=node.id,
            relation=RelationKind.EXHIBITS,
            source_provider=provider,
            source_tier=tier,
            confidence=0.8 if tier is SourceTier.AGGREGATOR else 0.5,
            fetched_at=now,
        )
    )


def _write_series(
    db: Database,
    entity: Entity,
    series: tuple[str, str | None],
    provider: str,
    now: datetime,
    *,
    ordinal: int | None = None,
) -> None:
    name, source_id = series
    node = Node.create(NodeKind.SERIES, name, source=provider, source_id=source_id, owned=False)
    db.upsert_node(node)
    db.upsert_edge(
        Edge(
            src_id=entity.id,
            dst_id=node.id,
            relation=RelationKind.PART_OF_SERIES,
            ordinal=ordinal,  # the season/part number, for "all seasons of X" queries
            source_provider=provider,
            source_tier=SourceTier.AGGREGATOR,
            confidence=0.9,
            fetched_at=now,
        )
    )


def _write_platform(
    db: Database, entity: Entity, name: str, predicted: bool, provider: str, now: datetime
) -> None:
    node = Node.create(NodeKind.PLATFORM, name, owned=False)
    db.upsert_node(node)
    db.upsert_edge(
        Edge(
            src_id=entity.id,
            dst_id=node.id,
            relation=RelationKind.AVAILABLE_ON,
            source_provider="model" if predicted else provider,
            source_tier=SourceTier.MODEL if predicted else SourceTier.AGGREGATOR,
            confidence=0.4 if predicted else 0.85,
            fetched_at=now,
        )
    )


# --- per-kind source fetch ------------------------------------------------
async def _fetch(
    client: httpx.AsyncClient, settings: Settings, entity: Entity
) -> tuple[MediaGraph | None, list[PlatformRef]]:
    key = settings.tmdb_api_key
    match entity.kind:
        case MediaKind.MOVIE:
            tmdb_id, skip = pinned_id(entity.external_ids, "tmdb")
            if skip or not (tmdb_id and key):
                return None, []
            src = TmdbSource()
            graph = await src.movie_graph(client, key, tmdb_id)
            actual = await src.movie_platforms(client, key, tmdb_id, settings.regions)
            where = [(p, False) for p in actual] or await _predicted_where(graph, settings)
            return graph, where
        case MediaKind.TV | MediaKind.ANIME:
            tmdb_id, skip = pinned_id(entity.external_ids, "tmdb")
            if skip or not (tmdb_id and key):
                return None, []
            src = TmdbSource()
            graph = await src.tv_graph(client, key, tmdb_id)
            actual = await src.tv_platforms(client, key, tmdb_id, settings.regions)
            return graph, [(p, False) for p in actual]
        case MediaKind.GAME:
            igdb_id, skip = pinned_id(entity.external_ids, "igdb")
            if skip or not igdb_id:
                return None, []
            graph = await IgdbSource().game_graph(client, settings, igdb_id)
            return graph, [(p, False) for p in graph.platforms]
        case _:
            return None, []


async def _predicted_where(graph: MediaGraph, settings: Settings) -> list[PlatformRef]:
    """Nothing streaming yet: predict the studio's typical home (a MODEL-tier guess)."""
    studios = tuple(c.name for c in graph.credits if c.node_kind is NodeKind.ORG)
    predicted = await learn_predicted_platform(studios, settings)
    return [(predicted, True)] if predicted else []


# --- LLM theme extraction (soft, flagged) ---------------------------------
class _Themes(BaseModel):
    themes: list[str] = Field(
        description="3-6 short thematic descriptors (mood/subject/style), NOT genres"
    )


_THEME_PROMPT = (
    "You extract a few concise *thematic* descriptors for a work — recurring subjects, "
    "moods, or stylistic registers (e.g. 'revenge', 'bureaucratic dread', 'coming of age', "
    "'neo-noir'). Do NOT repeat genres. Lowercase, 1-3 words each, at most 6. If the summary "
    "is too thin to tell, return an empty list rather than guessing."
)


async def _llm_themes(settings: Settings, entity: Entity, graph: MediaGraph) -> tuple[str, ...]:
    if not settings.openai_api_key:
        return ()
    context = (
        f"Title: {entity.title}\nKind: {entity.kind.value}\n"
        f"Genres: {', '.join(graph.genres) or 'unknown'}\n"
        f"Summary: {graph.summary or 'unknown'}"
    )
    try:
        completion = await AsyncOpenAI(api_key=settings.openai_api_key).beta.chat.completions.parse(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": _THEME_PROMPT},
                {"role": "user", "content": context},
            ],
            response_format=_Themes,
        )
    except Exception as exc:  # network / API / quota — themes are optional, never fatal
        log.warning("enrich.themes_error", entity=entity.title, error=str(exc))
        return ()
    parsed = completion.choices[0].message.parsed
    if parsed is None:
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for theme in parsed.themes:
        cleaned = theme.strip().lower()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return tuple(out[:_MAX_THEMES])
