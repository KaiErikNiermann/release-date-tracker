"""Tier-0 orchestration: run the right sources over each entity, persist results.

I/O-bound and embarrassingly parallel, so sources run concurrently per entity and
entities run concurrently (bounded). Writes are incremental — each entity's
observations are committed as they arrive, so an interrupted run resumes cleanly
and never loses completed work.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from release_tracker.config import Settings
from release_tracker.db import Database
from release_tracker.logging import get_logger
from release_tracker.models import Entity
from release_tracker.sources import sources_for
from release_tracker.sources.base import make_client

log = get_logger("pipeline")


@dataclass(slots=True)
class PullStats:
    entities: int = 0
    observations: int = 0
    errors: int = 0


async def _pull_entity(
    client: httpx.AsyncClient,
    db: Database,
    entity: Entity,
    settings: Settings,
    sem: asyncio.Semaphore,
    stats: PullStats,
) -> None:
    sources = sources_for(entity.kind)
    if not sources:
        log.debug("pipeline.no_sources", entity=entity.title, kind=entity.kind.value)
        return
    async with sem:
        results = await asyncio.gather(
            *(src.pull(client, entity, settings) for src in sources),
            return_exceptions=True,
        )
    merged_ids: dict[str, str] = {}
    for src, result in zip(sources, results, strict=True):
        if isinstance(result, BaseException):
            stats.errors += 1
            log.error(
                "pipeline.source_error",
                source=src.name,
                entity=entity.title,
                error=str(result),
            )
            continue
        merged_ids.update(result.external_ids)
        if result.observations:
            stats.observations += db.upsert_observations(result.observations)
    if merged_ids:
        db.merge_external_ids(entity.id, merged_ids)
    stats.entities += 1


async def pull_all(
    db: Database, settings: Settings, *, concurrency: int = 6, watched_only: bool = True
) -> PullStats:
    entities = list(db.iter_entities(watched_only=watched_only))
    stats = PullStats()
    sem = asyncio.Semaphore(concurrency)
    async with make_client() as client:
        await asyncio.gather(*(_pull_entity(client, db, e, settings, sem, stats) for e in entities))
    log.info(
        "pipeline.done",
        entities=stats.entities,
        observations=stats.observations,
        errors=stats.errors,
    )
    return stats
