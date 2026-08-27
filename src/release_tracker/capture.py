"""Persisting a looked-up title into the tracker — the write half of the `/rd-add` path.

Lifted out of ``cli.py`` so it is callable from *any* frontend. Every function here takes
the ``Database`` and (optionally) the ``httpx.AsyncClient`` it should use rather than
opening its own, matching the convention the rest of the library already follows
(``pipeline.pull_entity``, ``enrich.enrich_work``): the caller owns the lifecycle. A
long-running app can then hand over the client it already has open from the candidate
search instead of paying for a fresh TLS handshake on every capture.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

import httpx

from release_tracker import matching
from release_tracker.config import Settings
from release_tracker.db import Database
from release_tracker.enrich import enrich_work
from release_tracker.lookup import (
    RdReport,
    capture_candidates,
    lookup,
    report_for_candidate,
    select_candidate,
)
from release_tracker.models import ConsumptionState, Entity, MediaKind, Node, NodeKind
from release_tracker.pipeline import pull_entity
from release_tracker.sources.base import Candidate, make_client
from release_tracker.titles import season_label

__all__ = ["CaptureOutcome", "capture_work", "entity_for", "run_capture"]


@dataclass(frozen=True, slots=True)
class CaptureOutcome:
    """What one capture resolved to: a report to render, the entity if it was persisted, and —
    when the title collides — the candidate set to surface instead of adding the wrong one."""

    report: RdReport | None
    entity: Entity | None = None
    ambiguous: tuple[tuple[MediaKind, Candidate], ...] = ()

    @property
    def tracked(self) -> bool:
        return self.entity is not None


def entity_for(db: Database, name: str, report: RdReport, season: int | None) -> Entity:
    """The entity to upsert for a capture — reusing an existing one keyed by canonical id.

    Dedup by the pinned id (not the title slug) so re-capturing the same work under a different
    typed title ("Odyssey" vs "The Odyssey") updates the one row instead of forking a duplicate.
    Season captures keep the title-slug id ("Show: Season N" is already unique per season, and one
    show id spans every season), so they only fold in via the slug path. A brand-new capture is
    titled from the *canonical* ``matched_title`` for a deterministic, collision-free slug.
    """
    assert report.kind is not None  # guaranteed by the caller's kind-None guard
    base_title = report.matched_title or name
    if season is None:
        for id_key, value in report.canonical.items():
            existing = db.find_entity_by_external_id(id_key, value)
            if existing is not None:
                state = (
                    ConsumptionState.WANT
                    if existing.consumption_state is ConsumptionState.UNSET
                    else existing.consumption_state
                )
                return existing.model_copy(
                    update={
                        "external_ids": {**existing.external_ids, **report.canonical},
                        "consumption_state": state,
                    }
                )
        title = base_title
    else:
        title = season_label(base_title, season)
    return Entity.create(
        title,
        report.kind,
        external_ids=dict(report.canonical),
        consumption_state=ConsumptionState.WANT,
        season=season,
    )


async def capture_work(
    db: Database,
    settings: Settings,
    name: str,
    report: RdReport,
    *,
    season: int | None = None,
    client: httpx.AsyncClient | None = None,
) -> Entity | None:
    """Capture a looked-up title into the tracker — always, whenever we know its kind.

    Returns the persisted entity (so a caller can jump to it), or ``None`` if it was skipped.

    This is a *tracker*, tech included, so ``/rd-add`` never refuses a capture:
    - **Anything that pinned a canonical id** captures *with* it, then pulls dates + enriches
      who/where/what — *including date-less (TBA) ones* (the skill then proposes a
      speculative window so nothing sits date-less).
    - **A kind where a miss is expected** (tech, other) captures as a bare entity with no ids
      and no auto-pull — exactly like the manual ``rdt add`` path that seeded e.g. Steam
      Frames. The skill follows up with a release window.

    The only true skip is a total miss with no kind at all, and a *strict* kind we couldn't
    pin (see ``matching.STRICT_CAPTURE_KINDS``): a bare unpinned movie would be a bogus,
    un-enrichable stub, better surfaced as "not tracked" so the title can be corrected. Tech
    is resolvable but not strict — Wikidata simply hasn't heard of most devices, so a miss
    there says nothing about whether the name was right.

    With ``season``, the entry is canonical-titled ``"Show: Season N"`` and carries the
    structured coord so enrichment auto-wires the series link + subtitle (full-auto path).
    """
    if report.kind is None:
        return None
    if matching.requires_canonical_for_capture(report.kind) and not report.canonical:
        return None
    entity = entity_for(db, name, report, season)
    db.upsert_entity(entity)
    db.upsert_node(
        Node(id=entity.id, node_kind=NodeKind.WORK, name=entity.title, owned=True, external_ids={})
    )
    if report.canonical:  # only a pinned, resolvable entity has ids to pull dates / enrich from
        async with contextlib.AsyncExitStack() as stack:
            http = client or await stack.enter_async_context(make_client())
            await pull_entity(db, settings, entity, client=http)  # dates via the pinned ids
            await enrich_work(http, db, settings, entity)  # who/where/what
    return entity


async def run_capture(
    db: Database,
    settings: Settings,
    name: str,
    *,
    kind_hint: MediaKind | None = None,
    region: str | None = None,
    season: int | None = None,
    latest: bool = False,
    want_year: int | None = None,
    id_pick: dict[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> CaptureOutcome:
    """Look a title up and persist it, refusing to guess when the name collides.

    Every kind goes through ``select_candidate``; a name collision then surfaces the
    candidate list rather than silently adding the wrong (or a duplicate) title. Tech joined
    them once Wikidata gave it an id space — "RTX 5090" vs "RTX 5090D" and "Steam Deck" vs
    "Steam Deck OLED" are exactly the picks a user has to make.

    A *miss* is a different thing from a collision, and the two must not share an answer.
    For a movie a miss means the title was wrong; for a device it usually just means
    Wikidata is thin, and the device still has to be trackable. So the miss branch hands
    off to ``capture_work``, whose own gate keeps movie/tv/game untracked while letting tech
    through as a bare entity.
    """
    async with contextlib.AsyncExitStack() as stack:
        http = client or await stack.enter_async_context(make_client())
        kinded = await capture_candidates(http, name, settings, kind_hint=kind_hint)
        kind_of = {id(c): k for k, c in kinded}
        pick = select_candidate(
            [c for _, c in kinded], latest=latest, want_year=want_year, id_pick=id_pick
        )
        if pick.outcome == "ambiguous":
            # too close to call — surface the list, persist nothing (force an explicit pick).
            return CaptureOutcome(
                report=None,
                ambiguous=tuple((kind_of[id(c)], c) for c in pick.candidates),
            )
        if pick.outcome == "no_match" or pick.cand is None:
            # nothing solid — same web-fallback report as a plain miss.
            report = await lookup(name, settings, kind_hint=kind_hint, region=region, season=season)
            # `select_candidate` also reports no_match when --latest/--year/--id filtered every
            # contender out. `lookup` re-runs the search *without* those, so it can come back
            # with a confident match for the very title the selector rejected — capturing that
            # would silently track the wrong year's entry. Only an unfiltered miss falls back.
            narrowed = latest or want_year is not None or bool(id_pick)
            entity = (
                None
                if narrowed
                else await capture_work(db, settings, name, report, season=season, client=http)
            )
            return CaptureOutcome(report=report, entity=entity)
        chosen = pick.cand
        report = await report_for_candidate(
            http, name, kind_of[id(chosen)], chosen, settings, season=season
        )
        entity = await capture_work(db, settings, name, report, season=season, client=http)
    return CaptureOutcome(report=report, entity=entity)
