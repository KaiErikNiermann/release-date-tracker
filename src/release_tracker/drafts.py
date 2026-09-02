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
from release_tracker.config import Settings, secret
from release_tracker.db import Database
from release_tracker.edits import USER_PROVIDER, add_series, add_tag, set_date
from release_tracker.logging import get_logger
from release_tracker.lookup import MATCH_FLOOR, report_for_candidate
from release_tracker.models import (
    ConsumptionState,
    DescriptorKind,
    Edge,
    Entity,
    MediaKind,
    RelationKind,
    ReleaseChannel,
    SourceTier,
    WorkRelation,
)
from release_tracker.sources.base import Candidate, MediaGraph
from release_tracker.sources.igdb import IgdbSource
from release_tracker.sources.tmdb import TmdbSource
from release_tracker.sources.wikidata import Lineage, find_lineage
from release_tracker.tech import (
    CATEGORY_OVERRIDE_KEY,
    TechCategory,
    classify_tech,
    looks_like_tech,
)
from release_tracker.titles import Version, slice_title, split_version

__all__ = [
    "PREDECESSOR_KEY",
    "Carried",
    "Draft",
    "commit",
    "for_candidate",
    "infer_franchise",
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

# Provenance for a carried franchise link: ours, not the source's and not the user's. It
# shares `enrich`'s predicted-platform provider so the two render alike — both are things we
# worked out rather than read.
_INFERRED_PROVIDER = "model"


@dataclass(frozen=True, slots=True)
class Carried:
    """What a sibling lends an entry no database has heard of yet.

    Two fields, and the shortness is the measurement rather than caution. Across ten sequel
    pairs the developer was contradicted 3 times and the publisher 4 — New Vegas to Fallout
    4, Portal to Portal 2, Wizard of Legend to its sequel — while genre held 0 for 10,
    including the three 2D-to-3D jumps (Risk of Rain 2, Wizard of Legend 2, Enter the
    Gungeon 2) that keep overlapping genres anyway. So the franchise link and the genres
    come across; the studio never does.

    The franchise link is also the *licence* for the rest. A sibling in no collection lends
    nothing however close its name reads, which is what keeps "Fallout: New Vegas" from
    telling us about "Fallout 5" on the strength of a shared word.
    """

    predecessor: str  # the sibling's title, named in every line this produces
    series: tuple[str, str | None]  # (name, source_id), as `MediaGraph.series` carries it
    genres: tuple[str, ...] = ()

    @property
    def reasons(self) -> tuple[str, ...]:
        """One line per field carried, each naming what it was carried from.

        The review screen prints these verbatim, which is the whole licence for guessing:
        a prefill costs one keystroke to correct *if* you can see it is a guess.
        """
        said = [f"franchise carried from “{self.predecessor}”, which is in the same collection"]
        if self.genres:
            said.append(f"genre carried from “{self.predecessor}”: {', '.join(self.genres)}")
        return tuple(said)


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
    part_label: str | None = None  # what that cut was called — "Part"/"Act"/"Vol"
    carried: Carried | None = None  # what a predecessor lends a sequel nothing has listed
    # Why each field looks the way it does, one line per inference that fired. The review
    # screen prints these verbatim: a prefill nobody can account for is worse than none.
    reasons: tuple[str, ...] = ()

    @property
    def synthetic(self) -> bool:
        """True when nothing out there matched and this entry is the user's own claim."""
        return self.candidate is None


def for_candidate(
    title: str,
    kind: MediaKind,
    candidate: Candidate,
    *,
    season: int | None = None,
    part: int | None = None,
    part_label: str | None = None,
    reasons: tuple[str, ...] = (),
) -> Draft:
    """A draft standing in for a search hit, so review and capture share one path.

    Coords are kind-gated the way :func:`prefill` gates them: a season on a film is not a
    coordinate, it is a mistake, and carrying it into a hidden form field is how it becomes
    an invisible one.
    """
    tv = kind is MediaKind.TV
    return Draft(
        title=title,
        kind=kind,
        category=classify_tech(title) if kind is MediaKind.TECH else None,
        candidate=candidate,
        season=season if tv else None,
        part=part if tv else None,
        part_label=part_label if tv else None,
        reasons=reasons if tv and season is not None else (),
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
    settings: Settings | None = None,
) -> Draft:
    """:func:`prefill`, plus the inferences that need the network.

    Two of them, one per kind of unlisted thing. Tech has a lineage worth chasing, so when
    the ladder lands on tech the device path runs on top and folds in the generation marker
    and the predecessor. Films and games have a *franchise* worth chasing instead, and the
    sibling that carries it is already in ``hits``. One row comes out either way — the
    unannounced case is the richest rung of this ladder, not a feature beside it.

    ``settings`` is what the franchise rung needs to reach IGDB and TMDB; without it that
    rung simply does not fire, which is the same shape as a source being unconfigured.
    """
    draft = prefill(
        text,
        kind_hint=kind_hint,
        year_hint=year_hint,
        season_hint=season_hint,
        hits=hits,
    )
    if draft.kind is MediaKind.TECH:
        device = await infer_synthetic(client, draft.title)
        if device is None:  # no generation marker — nothing to look a family up by
            return draft
        # The lineage speaks for itself on the review screen (`follows <predecessor>`), so it
        # adds no reason line here; only the fields it actually filled come across.
        return replace(draft, version=device.version, predecessor=device.predecessor)
    if settings is None:
        return draft
    carried = await infer_franchise(client, settings, draft.kind, hits)
    if carried is None:
        return draft
    return replace(draft, carried=carried, reasons=(*draft.reasons, *carried.reasons))


async def infer_franchise(
    client: httpx.AsyncClient,
    settings: Settings,
    kind: MediaKind,
    hits: Sequence[tuple[MediaKind, Candidate]],
) -> Carried | None:
    """What the strongest sibling in the search results lends an unlisted sequel.

    No stem search and no prefix rule, because neither is needed: searching a film or a game
    that has not been listed yet already surfaces the rest of its series, which is the same
    observation :func:`_consensus_kind` makes about the kind. "Fallout 5" returns Fallout 4.

    What licenses the carry is the sibling being in a **collection**, not its name looking
    similar. A sibling in none lends nothing however close it reads — that is the gate
    between "Fallout 5 follows Fallout 4" and the thing it must never say, which is that
    "Fallout: New Vegas" is a Fallout-numbered game.

    None whenever any link in that chain is missing, and never an exception: this runs on
    every keystroke's search in the add screen, and a franchise it cannot read is not a
    reason to lose the row.
    """
    if kind not in (MediaKind.MOVIE, MediaKind.GAME):
        return None  # TV carries its franchise through seasons; the rest have no collection
    sibling = next((c for k, c in hits if k is kind and c.score >= MATCH_FLOOR), None)
    if sibling is None:
        return None
    try:
        graph = await _sibling_graph(client, settings, kind, sibling.canonical_id)
    except Exception as exc:
        # This rides on top of an already-good draft and runs on every keystroke's search.
        # Losing the row — and with it the hit list the caller's own handler resets — because
        # a genre lookup timed out is far worse than adding an entry with no franchise on it.
        log.warning("drafts.franchise_failed", predecessor=sibling.title, error=str(exc))
        return None
    if graph is None or graph.series is None:
        return None
    log.info(
        "drafts.franchise",
        predecessor=sibling.title,
        series=graph.series[0],
        genres=len(graph.genres),
    )
    return Carried(predecessor=sibling.title, series=graph.series, genres=graph.genres)


async def _sibling_graph(
    client: httpx.AsyncClient, settings: Settings, kind: MediaKind, source_id: str
) -> MediaGraph | None:
    """The who/what/series a source already assembles, for one sibling — one request."""
    if kind is MediaKind.MOVIE:
        key = secret(settings.tmdb_api_key)
        return await TmdbSource().movie_graph(client, key, source_id) if key else None
    return await IgdbSource().game_graph(client, settings, source_id)


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
        # The coords have to reach *both* calls: the report resolves the season's own air
        # date rather than the show's, and the capture titles and files it as that season.
        # Passing them to neither is how a season typed on this form used to vanish.
        report = await report_for_candidate(
            client, draft.title, draft.kind, draft.candidate, settings, season=draft.season
        )
        return await capture_work(
            db,
            settings,
            draft.title,
            report,
            season=draft.season,
            part=draft.part,
            part_label=draft.part_label,
            client=client,
        )

    # Season coords are titled the way `rdt add --season` titles them, so a season added here
    # and one added from the CLI land on the same row rather than forking a near-duplicate.
    title = slice_title(draft.title, draft.season, draft.part, draft.part_label)
    entity = Entity.create(
        title,
        draft.kind,
        external_ids=_external_ids(draft),
        season=draft.season,
        part=draft.part,
        part_label=draft.part_label,
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
    _write_carried(db, entity, draft)
    log.info("drafts.committed", title=draft.title, kind=draft.kind.value)
    return entity


def _write_carried(db: Database, entity: Entity, draft: Draft) -> None:
    """Persist what the predecessor lent, at the provenance of a guess.

    MODEL tier and unowned, the same footing ``add_platform(predicted=True)`` uses. That is
    not hedging for its own sake: the day IGDB or TMDB lists this entry, its own pull reads
    the real franchise and genres, and a carried edge has to be the one that loses. Marking
    these as the user's own statement would make them permanent.
    """
    if draft.carried is None:
        return
    name, source_id = draft.carried.series
    add_series(
        db,
        entity,
        name,
        source=_INFERRED_PROVIDER,
        source_id=source_id,
        tier=SourceTier.MODEL,
        confidence=0.4,
    )
    for genre in draft.carried.genres:
        add_tag(db, entity, genre, DescriptorKind.GENRE, inferred=True)
    log.info(
        "drafts.carried",
        entity=entity.title,
        predecessor=draft.carried.predecessor,
        genres=len(draft.carried.genres),
    )


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
