"""An entry the user is still deciding about — everything we inferred, nothing written yet.

Two things arrive here. A **candidate** picked out of the add screen's search results, which
already has a canonical id and only needs a title confirmed. And a **synthetic** entry for a
device that does not exist in any database yet, which is the case the tracker actually exists
for: "Steam Deck 2" has no TMDB row, no IGDB row and no Wikidata item, and waiting for one
defeats the point of tracking it.

For the synthetic case the lineage does the prefilling. Strip the trailing generation marker,
look the family up, and the facts that survive a generation — that it is tech, roughly what
sort of tech, who makes it — come back with it. The facts that *don't* survive are left
alone: no dates (successor cadence is far too noisy to guess from, a median error of ~200
days on real chains), and no inherited ids, which would be false claims about a different
device.

Nothing here is written until :func:`commit`. That is the point of the type: inference is
good enough to save typing and not good enough to trust silently, so the draft is a thing the
user reads and corrects first. Category is the field that most needs it — a line can change
category between generations, and a regex over the name never sees that coming.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

import httpx

from release_tracker.capture import capture_work, write_work
from release_tracker.config import Settings
from release_tracker.db import Database
from release_tracker.edits import USER_PROVIDER, set_date
from release_tracker.logging import get_logger
from release_tracker.lookup import MATCH_FLOOR, report_for_candidate
from release_tracker.models import (
    ConsumptionState,
    Edge,
    Entity,
    MediaKind,
    RelationKind,
    ReleaseChannel,
    SourceTier,
    WorkRelation,
)
from release_tracker.sources.base import Candidate
from release_tracker.sources.wikidata import Lineage, find_lineage
from release_tracker.tech import (
    CATEGORY_OVERRIDE_KEY,
    TechCategory,
    classify_tech,
    looks_like_tech,
)
from release_tracker.titles import Version, season_label, split_version

__all__ = [
    "PREDECESSOR_KEY",
    "Draft",
    "commit",
    "for_candidate",
    "infer_freeform",
    "infer_synthetic",
    "prefill",
]

log = get_logger("drafts")

# Provenance for a speculative entry: the item it was positioned against. Inert as an
# external id — `links.work_sources` has no formatter for it, so it never renders as a
# source — and it is the anchor a later "has the successor been announced?" check would read.
PREDECESSOR_KEY = "wikidata_predecessor"

# A hand-authored date goes on the generic channel, the same one the edit screen offers
# first. Store and retail channels are a puller's business.
_DRAFT_CHANNEL = ReleaseChannel.PRIMARY


@dataclass(frozen=True, slots=True)
class Draft:
    """A proposed entry: what we would write, before anyone agrees to it."""

    title: str
    kind: MediaKind
    category: TechCategory | None = None  # tech only; None elsewhere
    edtf: str = ""  # hand-typed during review, empty means "no date yet"
    version: Version | None = None  # the generation marker, when the title carried one
    predecessor: Lineage | None = None  # what it was positioned against
    candidate: Candidate | None = None  # set when this came from a real search hit
    season: int | None = None  # TV only; structured coords, as `rdt add --season` writes them
    part: int | None = None  # TV only; the mid-season cut within the season
    # Why each field looks the way it does, one line per inference that fired. The review
    # screen prints these verbatim: a prefill nobody can account for is worse than none.
    reasons: tuple[str, ...] = ()

    @property
    def synthetic(self) -> bool:
        """True when nothing out there matched and this entry is the user's own claim."""
        return self.candidate is None


def for_candidate(title: str, kind: MediaKind, candidate: Candidate) -> Draft:
    """A draft standing in for a search hit, so review and capture share one path."""
    return Draft(
        title=title,
        kind=kind,
        category=classify_tech(title) if kind is MediaKind.TECH else None,
        candidate=candidate,
    )


def _consensus_kind(
    hits: Sequence[tuple[MediaKind, Candidate]],
) -> tuple[MediaKind, int] | None:
    """The kind every *credible* hit agrees on, and how many said so — else None.

    The franchise case. Searching a film that has not been listed yet still surfaces the rest
    of its series, and "the things that actually look like what you typed are all movies" is
    real evidence about what you are adding. ``Candidate.score`` is already title similarity
    against the query (:func:`matching.score_candidate`) and is comparable across sources, so
    ``MATCH_FLOOR`` — the line the capture path already uses for "we don't trust this match" —
    is exactly the right filter. Hits below it are noise and say nothing; a split verdict
    (a film and a game of the same name) says nothing either, and we decline rather than guess.
    """
    kinds = [kind for kind, cand in hits if cand.score >= MATCH_FLOOR]
    if not kinds or len(set(kinds)) != 1:
        return None
    return kinds[0], len(kinds)


def _infer_kind(
    title: str,
    kind_hint: MediaKind | None,
    season_hint: int | None,
    hits: Sequence[tuple[MediaKind, Candidate]],
    reasons: list[str],
) -> MediaKind:
    """The kind ladder, strongest evidence first, recording why the winning rung fired."""
    if kind_hint is not None:
        reasons.append(f"kind from your `kind:{kind_hint.value}`")
        return kind_hint
    # `season:` is the user's own word too, so it outranks anything read off the results.
    if season_hint is not None:
        reasons.append(f"`season:{season_hint}` means this is a series")
        return MediaKind.TV
    if (consensus := _consensus_kind(hits)) is not None:
        kind, count = consensus
        plural = "es" if count > 1 else ""
        reasons.append(f"kind read off the {count} match{plural} above (all {kind.value})")
        return kind
    if looks_like_tech(title):
        reasons.append("the name reads like a device")
        return MediaKind.TECH
    return MediaKind.OTHER


def prefill(
    text: str,
    *,
    kind_hint: MediaKind | None = None,
    year_hint: int | None = None,
    season_hint: int | None = None,
    hits: Sequence[tuple[MediaKind, Candidate]] = (),
) -> Draft:
    """A draft for anything at all, filled in from whatever the query and the results imply.

    This is the always-available door: no canonical id, no version marker and no source
    coverage required, because four of the nine kinds (book, music, podcast, comic) have no
    source at all and a tracker that cannot hold an unannounced thing is missing the point.

    Pure and synchronous on purpose. It is the one inference that must still work when the
    search itself has just failed, and it is the part worth unit-testing rung by rung.

    Deliberately never infers a *date*. The lineage code declines to guess one from successor
    cadence for good reason (see the module docstring), and a franchise's other entries say
    even less about when a new one ships — only an explicit ``year:`` fills that field.
    """
    title = text.strip()
    reasons: list[str] = []
    kind = _infer_kind(title, kind_hint, season_hint, hits, reasons)
    if year_hint is not None:
        reasons.append(f"date from your `year:{year_hint}`")
    return Draft(
        title=title,
        kind=kind,
        category=classify_tech(title) if kind is MediaKind.TECH else None,
        edtf=str(year_hint) if year_hint is not None else "",
        season=season_hint if kind is MediaKind.TV else None,
        reasons=tuple(reasons),
    )


async def infer_freeform(
    client: httpx.AsyncClient,
    text: str,
    *,
    kind_hint: MediaKind | None = None,
    year_hint: int | None = None,
    season_hint: int | None = None,
    hits: Sequence[tuple[MediaKind, Candidate]] = (),
) -> Draft:
    """:func:`prefill`, plus the one inference that needs the network.

    Tech is the only kind with a lineage worth chasing, so when the ladder lands on tech the
    device path runs on top and folds in the generation marker and the predecessor. One row
    comes out either way — the unannounced-device case is the richest rung of this ladder,
    not a feature sitting beside it.
    """
    draft = prefill(
        text,
        kind_hint=kind_hint,
        year_hint=year_hint,
        season_hint=season_hint,
        hits=hits,
    )
    if draft.kind is not MediaKind.TECH:
        return draft
    device = await infer_synthetic(client, draft.title)
    if device is None:  # no generation marker — nothing to look a family up by
        return draft
    # The lineage speaks for itself on the review screen (`follows <predecessor>`), so it adds
    # no reason line here; only the fields it actually filled come across.
    return replace(draft, version=device.version, predecessor=device.predecessor)


async def infer_synthetic(client: httpx.AsyncClient, text: str) -> Draft | None:
    """A draft for a device nothing has heard of, or None when the name says nothing useful.

    Requires a trailing generation marker. Without one there is no lineage to look up and
    nothing to infer, so the honest answer is no draft rather than an empty form — the
    caller falls back to the plain "no matches" state.
    """
    stem, version = split_version(text)
    if version is None:
        return None
    lineage = await find_lineage(client, stem)
    log.info(
        "drafts.synthetic",
        text=text,
        stem=stem,
        version=version.token,
        predecessor=lineage.qid if lineage else None,
    )
    return Draft(
        title=text.strip(),
        kind=MediaKind.TECH,
        # From the *typed* name, not the predecessor's: the user is telling us what they
        # think this is, and a line that changed category between generations is exactly
        # the case the review screen exists to catch either way.
        category=classify_tech(text),
        version=version,
        predecessor=lineage,
    )


def _external_ids(draft: Draft) -> dict[str, str]:
    ids: dict[str, str] = {}
    if draft.predecessor is not None:
        ids[PREDECESSOR_KEY] = draft.predecessor.qid
    # Only worth storing when it disagrees with what the title would have said anyway.
    if draft.category is not None and draft.category is not classify_tech(draft.title):
        ids[CATEGORY_OVERRIDE_KEY] = draft.category.value
    return ids


async def commit(
    db: Database,
    settings: Settings,
    draft: Draft,
    client: httpx.AsyncClient,
) -> Entity | None:
    """Persist the draft. Returns the entity, or None if a candidate capture declined it.

    A candidate goes through the normal capture — pull its dates, enrich it — because it has
    a canonical id and that machinery is strictly better than anything typed by hand. A
    synthetic entry has nothing to pull, so it is written directly, exactly as ``rdt add``
    writes one, plus whatever date the user filled in during review.
    """
    if draft.candidate is not None:
        report = await report_for_candidate(
            client, draft.title, draft.kind, draft.candidate, settings
        )
        return await capture_work(db, settings, draft.title, report, client=client)

    # Season coords are titled the way `rdt add --season` titles them, so a season added here
    # and one added from the CLI land on the same row rather than forking a near-duplicate.
    title = season_label(draft.title, draft.season) if draft.season is not None else draft.title
    entity = Entity.create(
        title,
        draft.kind,
        external_ids=_external_ids(draft),
        season=draft.season,
        part=draft.part,
        # Adding something by hand *is* stating an intent, so it starts wanted. Left unset it
        # could never reach `available` — `bucket_of` gates that on an active state — and the
        # search path through `capture_work` already sets exactly this.
        consumption_state=ConsumptionState.WANT,
    )
    write_work(db, entity)
    if draft.edtf.strip():
        # A bad literal must not lose the entry that was just created — the row is already
        # there and the user can fix the date from the card.
        try:
            set_date(db, entity, _DRAFT_CHANNEL, draft.edtf.strip())
        except ValueError as exc:
            log.warning("drafts.bad_edtf", title=draft.title, edtf=draft.edtf, error=str(exc))
    _link_predecessor(db, entity, draft)
    log.info("drafts.committed", title=draft.title, kind=draft.kind.value)
    return entity


def _link_predecessor(db: Database, entity: Entity, draft: Draft) -> None:
    """Record the lineage edge, but only when the predecessor is already tracked.

    Deliberately opportunistic. Hanging the edge requires a node at the far end, and
    manufacturing one would put a device the user has no interest in — one that already
    shipped — into their tracker purely as an anchor. When they *do* track it (common for
    films and games, rare for hardware) the card gets "successor of X" for free.
    """
    if draft.predecessor is None:
        return
    prior = db.find_entity_by_external_id("wikidata", draft.predecessor.qid)
    if prior is None:
        return
    db.upsert_edge(
        Edge(
            src_id=entity.id,
            dst_id=prior.id,
            relation=RelationKind.DERIVED_FROM,
            role=WorkRelation.SUCCESSOR,
            source_provider=USER_PROVIDER,
            source_tier=SourceTier.OFFICIAL,
            confidence=1.0,
            owned=True,
        )
    )
    log.info("drafts.linked_predecessor", entity=entity.title, predecessor=prior.title)
