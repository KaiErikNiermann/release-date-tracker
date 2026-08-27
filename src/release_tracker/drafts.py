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

from dataclasses import dataclass

import httpx

from release_tracker.capture import capture_work
from release_tracker.config import Settings
from release_tracker.db import Database
from release_tracker.edits import USER_PROVIDER, set_date
from release_tracker.logging import get_logger
from release_tracker.lookup import report_for_candidate
from release_tracker.models import (
    Edge,
    Entity,
    MediaKind,
    Node,
    NodeKind,
    RelationKind,
    ReleaseChannel,
    SourceTier,
    WorkRelation,
)
from release_tracker.sources.base import Candidate
from release_tracker.sources.wikidata import Lineage, find_lineage
from release_tracker.tech import CATEGORY_OVERRIDE_KEY, TechCategory, classify_tech
from release_tracker.titles import Version, split_version

__all__ = [
    "PREDECESSOR_KEY",
    "Draft",
    "commit",
    "for_candidate",
    "infer_synthetic",
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

    entity = Entity.create(draft.title, draft.kind, external_ids=_external_ids(draft))
    db.upsert_entity(entity)
    db.upsert_node(
        Node(id=entity.id, node_kind=NodeKind.WORK, name=draft.title, owned=True, external_ids={})
    )
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
