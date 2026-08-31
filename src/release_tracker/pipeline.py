"""Tier-0 orchestration: run the right sources over each entity, persist results.

I/O-bound and embarrassingly parallel, so sources run concurrently per entity and
entities run concurrently (bounded). Writes are incremental — each entity's
observations are committed as they arrive, so an interrupted run resumes cleanly
and never loses completed work.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx

from release_tracker.clock import utc_now, utc_today
from release_tracker.config import Settings
from release_tracker.db import Database
from release_tracker.logging import get_logger
from release_tracker.models import (
    BestEstimate,
    Certainty,
    DatePrecision,
    Edge,
    Entity,
    MediaKind,
    Node,
    NodeKind,
    RelationKind,
    ReleaseChannel,
    ReleaseObservation,
    SourceTier,
)
from release_tracker.platforms import canonical_platform
from release_tracker.resolve import best_estimates
from release_tracker.sources import justwatch, sources_for
from release_tracker.sources.base import SourceResult, make_client
from release_tracker.sources.justwatch import JustWatchAvailability
from release_tracker.titles import coords_of, search_title

log = get_logger("pipeline")

# JustWatch offers are film/TV retailer ground truth; a persisted VOD row is a confirmed digital
# date under a provider of its own, so a plain `pull` (which only clears API_PROVIDERS) leaves it.
_JUSTWATCH_PROVIDER = "justwatch"


@dataclass(slots=True)
class PullStats:
    entities: int = 0
    observations: int = 0
    errors: int = 0
    # provider -> why it could not answer. Distinct from `errors`: nothing went wrong, the
    # source was never in a position to look.
    skipped: dict[str, str] = field(default_factory=dict[str, str])


@dataclass(slots=True)
class RefreshResult:
    """Before/after best-estimates for one refreshed entity (or the error that stopped it)."""

    entity_id: str
    title: str
    before: list[BestEstimate] = field(default_factory=list[BestEstimate])
    after: list[BestEstimate] = field(default_factory=list[BestEstimate])
    error: str | None = None
    # provider -> why it could not answer. An empty diff means something different when a
    # source was never asked, and the caller has to be able to say so.
    skipped: dict[str, str] = field(default_factory=dict[str, str])


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
    # refresh: clear this entity's prior rows for the providers that answered, so re-pulls
    # (e.g. after re-pinning a canonical id) don't leave wrong-match ghosts.
    #
    # A *skipped* source must not count as answering. It returns an empty result exactly
    # like a source that looked and found nothing, so treating the two alike meant an
    # unconfigured TMDB wiped every date it had previously written — losing your key and
    # running a refresh silently emptied the tracker and reported a clean run.
    succeeded = tuple(
        src.name
        for src, r in zip(sources, results, strict=True)
        if isinstance(r, SourceResult) and r.skipped is None
    )
    db.delete_observations(entity.id, succeeded)
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
        if result.skipped is not None:
            stats.skipped[src.name] = result.skipped
            log.info("pipeline.source_skipped", source=src.name, reason=result.skipped)
            continue
        merged_ids.update(result.external_ids)
        if result.observations:
            stats.observations += db.upsert_observations(result.observations)
    if merged_ids:
        db.merge_external_ids(entity.id, merged_ids)
    stats.entities += 1


async def pull_entity(
    db: Database, settings: Settings, entity: Entity, *, client: httpx.AsyncClient | None = None
) -> PullStats:
    """Resolve + pull one entity (canonical ids + observations). Reuses the batch path.

    Opens its own client when not given one, so callers (e.g. enrich) can run a single
    title through the same resolution/persistence logic as a full ``pull``.
    """
    stats = PullStats()
    sem = asyncio.Semaphore(1)
    if client is not None:
        await _pull_entity(client, db, entity, settings, sem, stats)
        return stats
    async with make_client() as owned_client:
        await _pull_entity(owned_client, db, entity, settings, sem, stats)
    return stats


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


# --- batch refresh (dates keyed by canonical id, full JustWatch fidelity) --------------------
def persist_availability(db: Database, entity: Entity, avail: JustWatchAvailability) -> int:
    """Persist JustWatch's earliest confirmed VOD as a ``digital`` observation (0/1 written).

    JustWatch is display-only in the ``rd`` lookup path; ``refresh`` writes it so a confirmed
    digital date (e.g. a store's ``availableFromTime``) survives into ``upcoming``/``available``.
    Filed under its own ``justwatch`` provider — a plain ``pull`` only clears the live-API
    providers, so this row (like a hand-authored ``manual`` one) is left intact by a later pull.
    Re-persisting first drops the prior justwatch digital row so a moved date can't leave a ghost.
    """
    vod = avail.earliest_vod
    if vod is None:
        return 0
    db.delete_channel_observations(entity.id, _JUSTWATCH_PROVIDER, ReleaseChannel.DIGITAL)
    where = f"{avail.earliest_vod_platform} ({avail.earliest_vod_country})"
    db.upsert_observation(
        ReleaseObservation(
            entity_id=entity.id,
            channel=ReleaseChannel.DIGITAL,
            region=avail.earliest_vod_country or "WW",
            release_date=vod,
            precision=DatePrecision.EXACT,
            certainty=Certainty.CONFIRMED,
            source_tier=SourceTier.FIRST_PARTY_STORE,
            provider=_JUSTWATCH_PROVIDER,
            source_name=f"JustWatch · {avail.earliest_vod_platform}",
            source_quote=where,
            confidence=0.95,
            fetched_at=utc_now(),
        )
    )
    return 1


def persist_platforms(db: Database, entity: Entity, avail: JustWatchAvailability) -> int:
    """Persist JustWatch's live flatrate homes as region-scoped ``AVAILABLE_ON`` edges.

    Only ``flatrate``. A buy/rent offer is a transaction, not a place the work lives, and
    persisting those would put Apple TV Store and Amazon Video on the card of every title
    ever released.

    Filed at ``FIRST_PARTY_STORE`` because that is what it is — the storefront's own listing,
    strictly better provenance than TMDB's aggregator-tier copy of the same feed with the
    market stripped off. On a season entity the offers are season-scoped, so the edges are
    too: "Yellowjackets: Season 3" gets Paramount+ and not Netflix, which the show-level
    answer cannot express.
    """
    homes = {
        (canonical_platform(o.platform), o.country)
        for o in avail.offers
        if o.monetization == "flatrate" and o.platform and o.country
    }
    if not homes:
        return 0
    db.delete_edges(entity.id, RelationKind.AVAILABLE_ON, (_JUSTWATCH_PROVIDER,))
    now = utc_now()
    for name, country in sorted(homes):
        node = Node.create(NodeKind.PLATFORM, name, owned=False)
        db.upsert_node(node)
        db.upsert_edge(
            Edge(
                src_id=entity.id,
                dst_id=node.id,
                relation=RelationKind.AVAILABLE_ON,
                region=country,
                source_provider=_JUSTWATCH_PROVIDER,
                source_tier=SourceTier.FIRST_PARTY_STORE,
                confidence=0.9,
                fetched_at=now,
            )
        )
    return len(homes)


# JustWatch releaseType -> the channel its date belongs on.
_ANNOUNCED_CHANNEL: dict[str, ReleaseChannel] = {
    "digital": ReleaseChannel.DIGITAL,
    "physical": ReleaseChannel.PHYSICAL,
}


def persist_announced(db: Database, entity: Entity, avail: JustWatchAvailability) -> int:
    """Persist a season's *announced* platform date (0/1 written).

    The offer-less case a future season is in: nothing to read an ``availableFromTime`` off,
    but the platform has published a date. Same lifecycle as :func:`persist_availability` —
    dropped and rewritten first, so a date that moves leaves no ghost behind.
    """
    announced = avail.announced
    if announced is None:
        return 0
    channel = _ANNOUNCED_CHANNEL.get(announced.release_type)
    if channel is None:
        return 0
    db.delete_channel_observations(entity.id, _JUSTWATCH_PROVIDER, channel)
    db.upsert_observation(
        ReleaseObservation(
            entity_id=entity.id,
            channel=channel,
            region=announced.country,
            release_date=announced.when,
            precision=DatePrecision.EXACT,
            certainty=Certainty.CONFIRMED,
            source_tier=SourceTier.FIRST_PARTY_STORE,
            provider=_JUSTWATCH_PROVIDER,
            source_name=f"JustWatch · {announced.platform}",
            source_quote=f"{announced.platform} ({announced.country})",
            # under the 0.95 a live offer earns — announced, not yet happened
            confidence=0.9,
            fetched_at=utc_now(),
        )
    )
    return 1


async def _refresh_offers(
    client: httpx.AsyncClient, db: Database, settings: Settings, entity: Entity
) -> None:
    """Run the JustWatch scan for one entity and persist its earliest VOD, guarded against
    wrong-title matches (the year-sanity / can't-predate-theatrical checks ``rd`` uses)."""
    # deferred imports: the guards live in lookup, which pulls a heavy dependency graph.
    from release_tracker.lookup import justwatch_predates_theatrical, justwatch_year_mismatch
    from release_tracker.matching import year_hint
    from release_tracker.resolve import (
        confirmed_theatrical_by_region,
        earliest_confirmed_theatrical,
    )

    if not settings.justwatch_enabled or entity.kind not in (MediaKind.MOVIE, MediaKind.TV):
        return
    obs = list(db.iter_observations(entity.id))
    hint = year_hint([o.release_date for o in obs if o.release_date], utc_today())
    season, _ = coords_of(entity)
    if entity.kind is MediaKind.TV and season is not None:
        avail = await justwatch.season_availability(
            client,
            search_title(entity.title),
            season=season,
            countries=settings.justwatch_regions,
            # deliberately not `hint`: that is the *season's* year, and `pick_node` would
            # reject the show for not matching it. The title floor plus the season having to
            # exist on the matched show is the guard here.
            year=None,
        )
    else:
        avail = await justwatch.availability(
            client,
            entity.title,
            entity.kind,
            countries=settings.justwatch_regions,
            year=hint,
            floor=justwatch.TheatricalFloor(
                confirmed_theatrical_by_region(obs), earliest_confirmed_theatrical(obs)
            ),
        )
    if avail is None or justwatch_year_mismatch(avail, hint) is not None:
        return
    if justwatch_predates_theatrical(avail, obs):
        return
    persist_availability(db, entity, avail)
    persist_announced(db, entity, avail)
    persist_platforms(db, entity, avail)


async def _refresh_one(
    client: httpx.AsyncClient,
    db: Database,
    settings: Settings,
    entity: Entity,
    *,
    offers: bool,
    enrich: bool,
    sem: asyncio.Semaphore,
) -> RefreshResult:
    """Refresh one entity in place (Tier-0 + optional JustWatch + optional enrich), returning the
    before/after best-estimates. Each stage acquires ``sem`` on its own (never nested), so the
    batch stays bounded and one entity's failure is isolated to its own result."""
    before = list(best_estimates(db.iter_observations(entity.id)))
    stats = PullStats()
    try:
        await _pull_entity(client, db, entity, settings, sem, stats)  # acquires sem itself
        if offers:
            async with sem:
                await _refresh_offers(client, db, settings, entity)
        if enrich:
            from release_tracker.enrich import enrich_work

            async with sem:
                await enrich_work(client, db, settings, entity)
    except Exception as exc:  # one bad entity must not abort the batch
        log.error("pipeline.refresh_error", entity=entity.title, error=str(exc))
        return RefreshResult(entity.id, entity.title, before, before, error=str(exc))
    after = list(best_estimates(db.iter_observations(entity.id)))
    return RefreshResult(entity.id, entity.title, before, after, skipped=dict(stats.skipped))


async def refresh_entity(
    db: Database,
    settings: Settings,
    entity: Entity,
    *,
    offers: bool = True,
    enrich: bool = False,
    client: httpx.AsyncClient | None = None,
) -> RefreshResult:
    """Refresh one entity through the same stages ``rdt refresh`` runs, with its before/after.

    The single-card path in the TUI wants exactly what the batch does — a Tier-0 pull *and*
    the offer scan — because "update" that quietly skipped a source would be the kind of
    difference nobody discovers until a date is wrong. Sharing ``_refresh_one`` is what keeps
    the two honest; the defaults here mirror the CLI's.
    """
    sem = asyncio.Semaphore(1)
    if client is not None:
        return await _refresh_one(
            client, db, settings, entity, offers=offers, enrich=enrich, sem=sem
        )
    async with make_client() as owned:
        return await _refresh_one(
            owned, db, settings, entity, offers=offers, enrich=enrich, sem=sem
        )


async def refresh_entities(
    db: Database,
    settings: Settings,
    entities: list[Entity],
    *,
    offers: bool = True,
    enrich: bool = False,
    concurrency: int = 6,
) -> list[RefreshResult]:
    """Refresh a set of already-tracked entities by their pinned canonical ids (no search, so no
    name collisions). Reuses the single-entity pull path; one client + bounded concurrency."""
    sem = asyncio.Semaphore(concurrency)
    async with make_client() as client:
        results = await asyncio.gather(
            *(
                _refresh_one(client, db, settings, e, offers=offers, enrich=enrich, sem=sem)
                for e in entities
            )
        )
    return list(results)
