"""Turn many sourced observations into a confidence score and a best estimate.

This is the part public DBs don't do: instead of "confirmed | unknown" we weigh
source trust, corroboration, recency and stance, then surface the *chosen* date
per (entity, channel, region) while keeping every supporting claim addressable.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date

from release_tracker.models import (
    BestEstimate,
    Certainty,
    DatePrecision,
    ReleaseObservation,
    SourceTier,
)

# how much each stance is trusted, before source-tier / corroboration adjustments
_CERTAINTY_WEIGHT: dict[Certainty, float] = {
    Certainty.CONFIRMED: 1.0,
    Certainty.DELAYED: 0.95,
    Certainty.LEAKED: 0.6,
    Certainty.ESTIMATED: 0.5,
    Certainty.RUMORED: 0.4,
    Certainty.PREDICTED: 0.35,
}

PRECISION_RANK: dict[DatePrecision, int] = {
    DatePrecision.EXACT: 4,
    DatePrecision.MONTH: 3,
    DatePrecision.QUARTER: 2,
    DatePrecision.YEAR: 1,
    DatePrecision.TBA: 0,
}

# stances that represent an officially committed date (vs a best-guess window)
_CONFIRMED: frozenset[Certainty] = frozenset({Certainty.CONFIRMED, Certainty.DELAYED})


def score_observation(obs: ReleaseObservation, *, corroboration: int = 1) -> float:
    """Confidence in [0, 1] for a single claim.

    Combines stance weight, source tier and how many *other* independent sources
    agree on the same (channel, region, date). Corroboration has diminishing
    returns so a single official source still beats three rumor blogs.
    """
    base = _CERTAINTY_WEIGHT.get(obs.certainty, 0.4)
    tier_boost = int(obs.source_tier) / int(SourceTier.OFFICIAL)  # 0..1
    # diminishing corroboration: 1 source -> 0, 2 -> ~0.17, 4 -> ~0.3, inf -> 0.5
    corrob = 0.5 * (1 - 1 / max(corroboration, 1))
    raw = 0.5 * base + 0.3 * tier_boost + 0.2 * (base * 2 * corrob)
    return round(min(1.0, max(0.0, raw)), 3)


def rescore(observations: Sequence[ReleaseObservation]) -> list[ReleaseObservation]:
    """Return copies of ``observations`` with ``confidence`` filled in.

    Corroboration is counted across *distinct providers/sources* that assert the
    same (channel, region, release_date).
    """
    groups: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for obs in observations:
        groups[_slot_key(obs)].add(obs.source_url or obs.provider)

    scored: list[ReleaseObservation] = []
    for obs in observations:
        corroboration = len(groups[_slot_key(obs)])
        confidence = score_observation(obs, corroboration=corroboration)
        scored.append(obs.model_copy(update={"confidence": confidence}))
    return scored


def _slot_key(obs: ReleaseObservation) -> tuple[str, str, str]:
    """Identity for corroboration: same channel + region + date == same claim."""
    return (obs.channel.value, obs.region, obs.release_date.isoformat() if obs.release_date else "")


def _sort_key(obs: ReleaseObservation) -> tuple[int, int, float, int, str, str]:
    """Best claim first.

    A *confirmed* official date wins outright; after that **precision dominates** —
    a known month ("Oct 2026") beats a vague year ("2026") even from a higher-trust
    source, because a coarse date's materialized day (Jan 1 / Dec 31) is an artifact,
    not a real early date. Only then do confidence (tier + corroboration) and recency
    break the remaining ties; ``fetched_at`` lets a freshly re-curated manual date
    supersede an older one of equal standing.
    """
    return (
        1 if obs.certainty in _CONFIRMED else 0,
        PRECISION_RANK[obs.precision],
        obs.confidence,
        int(obs.source_tier),
        obs.published_at.isoformat() if obs.published_at else "",
        obs.fetched_at.isoformat() if obs.fetched_at else "",
    )


def best_estimates(observations: Iterable[ReleaseObservation]) -> list[BestEstimate]:
    """Collapse to one best estimate per (entity, channel, region)."""
    scored = rescore(list(observations))
    by_slot: dict[tuple[str, str, str], list[ReleaseObservation]] = defaultdict(list)
    for obs in scored:
        by_slot[(obs.entity_id, obs.channel.value, obs.region)].append(obs)

    results: list[BestEstimate] = []
    for (entity_id, _channel, region), obss in by_slot.items():
        ranked = sorted(obss, key=_sort_key, reverse=True)
        winner = ranked[0]
        results.append(
            BestEstimate(
                entity_id=entity_id,
                channel=winner.channel,
                region=region,
                release_date=winner.release_date,
                date_end=winner.date_end,
                precision=winner.precision,
                certainty=winner.certainty,
                price=winner.price,
                confidence=winner.confidence,
                supporting_observation_ids=tuple(o.id for o in ranked),
                fetched_at=winner.fetched_at,
            )
        )
    # Suppress a redundant dateless (TBA) estimate when the same entity has a finer-dated
    # one on any channel (e.g. IGDB "TBD" alongside a Steam quarter). The source's TBA stays
    # in storage — we just don't surface a no-date row once a real date exists somewhere.
    dated_entities = {e.entity_id for e in results if e.release_date is not None}
    results = [
        e for e in results if e.release_date is not None or e.entity_id not in dated_entities
    ]
    results.sort(key=lambda e: (e.release_date or date.max, -e.confidence))
    return results
