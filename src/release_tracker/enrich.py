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

from dataclasses import asdict, dataclass, replace
from datetime import datetime

import httpx
from openai import APIStatusError, AsyncOpenAI, AuthenticationError, PermissionDeniedError
from pydantic import BaseModel, Field

from release_tracker.clock import utc_now
from release_tracker.config import Settings, secret
from release_tracker.db import Database
from release_tracker.deltas import (
    estimate_digital,
    estimate_theatrical_from_premiere,
    match_studio,
)
from release_tracker.logging import get_logger
from release_tracker.models import (
    Certainty,
    DatePrecision,
    DescriptorKind,
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
from release_tracker.platforms import learn_predicted_platform
from release_tracker.resolve import commercial_anchor, earliest_premiere
from release_tracker.sources.base import Credit, MediaGraph, pinned_id
from release_tracker.sources.igdb import IgdbSource
from release_tracker.sources.tmdb import TmdbSource
from release_tracker.titles import extract_part, split_season

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

    now = utc_now()
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

    if graph.is_anime:  # medium/origin tag (orthogonal to format), sourced + high-trust
        _write_descriptor(
            db, entity, "anime", DescriptorKind.ORIGIN, provider, SourceTier.AGGREGATOR, now
        )

    if graph.series is not None:
        season = part = None
        if entity.kind is MediaKind.TV:  # explicit coords win; fall back to title parsing
            season = entity.season if entity.season is not None else split_season(entity.title)[1]
            part = entity.part if entity.part is not None else extract_part(entity.title)
        _write_series(db, entity, graph.series, provider, now, ordinal=season, part=part)
        summary.series = 1

    for name, predicted in platforms:
        _write_platform(db, entity, name, predicted, provider, now)
        summary.platforms += 1

    if include_themes:
        for theme in await llm_themes(settings, entity, graph):
            _write_descriptor(
                db, entity, theme, DescriptorKind.THEME, "openai", SourceTier.MODEL, now
            )
            summary.themes += 1

    if entity.kind is MediaKind.MOVIE:
        _persist_speculative_digital(db, entity, graph, now)

    log.info("enrich.done", entity=entity.title, **asdict(summary))
    return summary


def _persist_speculative_digital(
    db: Database, entity: Entity, graph: MediaGraph, now: datetime
) -> None:
    """Movie with no confirmed digital: store a PREDICTED digital observation so the
    speculative date is a first-class, freshness-tracked fact (not live-only).

    The anchor is the wide/limited *commercial* theatrical (theatrical + studio window). A
    festival PREMIERE never anchors directly — it precedes the commercial run — so if only a
    premiere is known we chain through an estimated wide date, at compounded low confidence.
    """
    obs = list(db.iter_observations(entity.id))
    if any(o.channel is ReleaseChannel.DIGITAL and o.certainty is Certainty.CONFIRMED for o in obs):
        return

    studio = match_studio([c.name for c in graph.credits if c.node_kind is NodeKind.ORG])
    # the home-video clock starts at the wide/limited commercial release — never a festival
    # PREMIERE. If only a premiere is known, chain through an estimated wide date (low confidence).
    if anchor := commercial_anchor(obs, confirmed_only=True):
        assert anchor.release_date is not None
        est = estimate_digital(anchor.release_date, studio)
        source_name = "theatrical + studio window"
    elif premiere := earliest_premiere(obs, confirmed_only=True):
        assert premiere.release_date is not None
        est_wide = estimate_theatrical_from_premiere(premiere.release_date)
        est = estimate_digital(est_wide.when, studio, theatrical_confirmed=False)
        est = replace(est, basis=f"premiere-chained: {est.basis}")
        source_name = "premiere -> est. wide -> digital"
    else:
        return
    db.upsert_observations(
        [
            ReleaseObservation(
                entity_id=entity.id,
                channel=ReleaseChannel.DIGITAL,
                region="WW",
                release_date=est.when,
                precision=DatePrecision.EXACT,
                certainty=Certainty.PREDICTED,
                source_tier=SourceTier.MODEL,
                provider="model",
                source_name=source_name,
                source_quote=est.basis,
                confidence=est.confidence,
                fetched_at=now,
            )
        ]
    )


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
    part: int | None = None,
) -> None:
    name, source_id = series
    node = Node.create(NodeKind.SERIES, name, source=provider, source_id=source_id, owned=False)
    db.upsert_node(node)
    db.upsert_edge(
        Edge(
            src_id=entity.id,
            dst_id=node.id,
            relation=RelationKind.PART_OF_SERIES,
            ordinal=ordinal,  # the season number, for "all seasons of X" queries
            part=part,  # the mid-season cut (Part/Volume/Cour N) within the season
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
    key = secret(settings.tmdb_api_key)
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
        case MediaKind.TV:
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


# Error codes that mean the call can never succeed as configured — a rejected key or an
# unbillable account — as opposed to a blip worth retrying.
_STANDING_LLM_FAILURES = frozenset(
    {
        "insufficient_quota",
        "credit_balance_exhausted",
        "billing_hard_limit_reached",
        "invalid_api_key",
        "account_deactivated",
    }
)

# Set once a standing failure is seen, which switches themes off for the rest of the process.
_themes_disabled: str | None = None


def reset_theme_gate() -> None:
    """Re-arm theme extraction after a standing failure (new key/credits, and for tests)."""
    global _themes_disabled  # a process-lifetime latch, deliberately module state
    _themes_disabled = None


def standing_llm_failure(exc: Exception) -> str | None:
    """Why an LLM call can never succeed as configured, or None if it was a transient blip."""
    if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
        return "the API key was rejected"
    if isinstance(exc, APIStatusError) and {exc.code, exc.type} & _STANDING_LLM_FAILURES:
        return f"the account cannot be billed ({exc.code or exc.type})"
    return None


async def llm_themes(settings: Settings, entity: Entity, graph: MediaGraph) -> tuple[str, ...]:
    """Soft thematic tags. Optional by design — any failure yields no themes, never an error.

    Themes sit on the *interactive* add path, so a call that cannot succeed must not be paid
    for over and over: an exhausted balance costs ~2.5s per add once the SDK's retries are
    counted, and every later add pays it again. A standing failure therefore latches themes off
    for the process (see :func:`standing_llm_failure`), and the retry budget is one — losing a
    few soft tags to a blip is cheaper than making someone wait for them.
    """
    global _themes_disabled  # a process-lifetime latch, deliberately module state
    if not settings.openai_api_key or _themes_disabled is not None:
        return ()
    context = (
        f"Title: {entity.title}\nKind: {entity.kind.value}\n"
        f"Genres: {', '.join(graph.genres) or 'unknown'}\n"
        f"Summary: {graph.summary or 'unknown'}"
    )
    try:
        completion = await AsyncOpenAI(
            api_key=secret(settings.openai_api_key), max_retries=1
        ).beta.chat.completions.parse(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": _THEME_PROMPT},
                {"role": "user", "content": context},
            ],
            response_format=_Themes,
        )
    except Exception as exc:  # network / API / quota — themes are optional, never fatal
        if (reason := standing_llm_failure(exc)) is not None:
            _themes_disabled = reason
            log.warning("enrich.themes_disabled", reason=reason, error=str(exc))
        else:
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
