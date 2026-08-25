"""Manual-resolution support: find canonical-id candidates and rank them.

Blind first-result matching is unreliable for ambiguous titles ("Blade"), and
perfect automated matching is impossible anyway. So instead we make it *fast to
pick the right one by hand*: a hardened search returns scored candidates with
their canonical ids, the user pins one, and the pullers use it directly.

The pieces here are deliberately db-aware only through plain values (dates), so
they stay easy to test.
"""

from __future__ import annotations

import asyncio
from datetime import date

import httpx

from release_tracker.config import Settings
from release_tracker.db import Database
from release_tracker.logging import get_logger
from release_tracker.models import Entity, MediaKind
from release_tracker.sources import sources_for
from release_tracker.sources.base import Candidate, Source
from release_tracker.titles import search_title, title_similarity

log = get_logger("matching")

# The canonical id an entity of this kind must have pinned to be "resolved".
# Kinds without a Tier-0 source can't be resolved here and are skipped.
REQUIRED_ID: dict[MediaKind, str] = {
    MediaKind.MOVIE: "tmdb",
    MediaKind.TV: "tmdb",
    MediaKind.GAME: "igdb",
}


def required_id_key(kind: MediaKind) -> str | None:
    return REQUIRED_ID.get(kind)


def is_resolvable(kind: MediaKind) -> bool:
    return kind in REQUIRED_ID


def needs_resolution(entity: Entity) -> bool:
    """True if the entity is resolvable but its required canonical id isn't pinned."""
    key = required_id_key(entity.kind)
    return key is not None and key not in entity.external_ids


def is_released(dates: list[date], today: date) -> bool:
    """Treat as released only when we know dates and none of them are still ahead."""
    return bool(dates) and max(dates) < today


def year_hint(dates: list[date], today: date) -> int | None:
    """Best year to disambiguate by: the soonest upcoming date, else the latest known."""
    future = sorted(d for d in dates if d >= today)
    if future:
        return future[0].year
    return max(dates).year if dates else None


def score_candidate(
    query_title: str, query_year: int | None, candidate: Candidate, kind: MediaKind
) -> float:
    """Rank a candidate by title similarity, plus year proximity where meaningful.

    For TV we ignore year: the watchlist date is a *season's* date, while the
    canonical record's year is the *show's* debut — comparing them is misleading.
    """
    sim = title_similarity(query_title, candidate.title)
    if kind is MediaKind.TV or query_year is None or candidate.year is None:
        return round(sim, 3)
    delta = abs(query_year - candidate.year)
    year_score = 1.0 if delta == 0 else max(0.0, 1 - delta / 4)
    return round(0.75 * sim + 0.25 * year_score, 3)


def build_worklist(db: Database, today: date, *, include_released: bool = False) -> list[Entity]:
    """Entities that are resolvable, not yet pinned, and (by default) still upcoming."""
    out: list[Entity] = []
    for entity in db.iter_entities(watched_only=True):
        if not needs_resolution(entity):
            continue
        if not include_released and is_released(db.observation_dates(entity.id), today):
            continue
        out.append(entity)
    return out


async def candidates_for(
    client: httpx.AsyncClient,
    entity: Entity,
    settings: Settings,
    *,
    hint_year: int | None = None,
    limit: int = 6,
) -> list[Candidate]:
    """Query every Tier-0 source supporting this kind, score and rank candidates."""
    query = search_title(entity.title)

    async def search(src: Source) -> list[Candidate]:
        """One source's hits, or none — a dead provider degrades the search, never fails it."""
        try:
            return await src.search_candidates(client, query, entity.kind, settings, limit=limit)
        except Exception as exc:
            log.warning(
                "matching.source_error", source=src.name, entity=entity.title, error=str(exc)
            )
            return []

    # Fanned out rather than awaited in turn: the sources are independent HTTP calls, so a
    # serial loop cost the sum of their latencies for no reason. Per-source isolation stays
    # inside `search`, so this cannot turn one flaky provider into a total failure.
    batches = await asyncio.gather(*(search(src) for src in sources_for(entity.kind)))
    ranked = [cand for found in batches for cand in found]
    for cand in ranked:
        cand.score = score_candidate(entity.title, hint_year, cand, entity.kind)
    ranked.sort(key=lambda c: c.score, reverse=True)
    return ranked
