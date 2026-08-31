"""UI-agnostic read models over the entity + graph store.

The CLI renders these with rich; a future web/d3 frontend can call the same
functions. Nothing here touches a terminal — it returns typed rows so the
"compact observability" surface (everything sorted by release date, with
who/where/what attached) has one place to evolve.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from release_tracker.config import Settings
from release_tracker.contingency import (
    ProfileMatcher,
    Resolution,
    ResolutionStatus,
    combine,
    estimate_resolution,
    matcher_from_settings,
)
from release_tracker.db import Database
from release_tracker.links import SourceLink, work_sources
from release_tracker.models import (
    BestEstimate,
    Bucket,
    Certainty,
    ConsumptionState,
    CreditRole,
    DatePrecision,
    DescriptorKind,
    Entity,
    LinkTier,
    MediaKind,
    Node,
    NodeKind,
    RelationKind,
    ReleaseChannel,
    SocialPlatform,
    SourceTier,
    WorkRelation,
)
from release_tracker.query import VocabEntry, Vocabulary
from release_tracker.resolve import PRECISION_RANK, best_estimates

Freshness = Literal["fresh", "aging", "stale"]
_THEATRICAL = (
    ReleaseChannel.THEATRICAL,
    ReleaseChannel.THEATRICAL_LIMITED,
    ReleaseChannel.PREMIERE,
)

# Role priority for picking a work's defining creator(s) — the trustworthy fixture
# (director/creator/dev) leads; cast trails. Lower sorts first.
_ROLE_RANK: dict[CreditRole, int] = {
    CreditRole.DIRECTOR: 0,
    CreditRole.CREATOR: 0,
    CreditRole.DEVELOPER: 0,
    CreditRole.SHOWRUNNER: 1,
    CreditRole.AUTHOR: 1,
    CreditRole.HOST: 1,
    CreditRole.ARTIST: 2,
    CreditRole.WRITER: 3,
    CreditRole.COMPOSER: 4,
    CreditRole.PUBLISHER: 5,
    CreditRole.STUDIO: 5,
    CreditRole.ANIMATION_STUDIO: 5,
    CreditRole.NETWORK: 5,
    CreditRole.VOICE: 8,
    CreditRole.CAST: 9,
    CreditRole.OTHER: 7,
}


@dataclass(frozen=True, slots=True)
class CreditLine:
    """One who-edge, resolved to a name (a credited person/org)."""

    role: CreditRole
    name: str
    node_id: str
    owned: bool


@dataclass(frozen=True, slots=True)
class TagLine:
    """One what-edge: a descriptor with its trust (sourced genre vs model theme)."""

    name: str
    kind: DescriptorKind
    predicted: bool  # True => model-derived (a flagged hypothesis, not a fact)
    # the descriptor node behind it: two kinds can share a name (horror the genre, horror
    # the theme), so an editor that unlinked by name would unlink both
    node_id: str = ""


@dataclass(frozen=True, slots=True)
class PlatformLine:
    """One place to watch: a platform and every market it is known to be live in.

    One line per *platform*, not per edge — Netflix in six countries is one place to watch,
    and six rows would blow out the browse column and the card alike. ``regions`` empty means
    unscoped: a broadcast network, or a source that never told us where.
    """

    name: str
    predicted: bool
    node_id: str = ""
    regions: tuple[str, ...] = ()  # ISO-2s, sorted
    providers: tuple[str, ...] = ()  # tmdb / justwatch / model — provenance for the card

    def reach_in(self, home: Iterable[str]) -> int:
        """How many of ``home``'s markets this platform is known to cover (0 when unscoped)."""
        return len(frozenset(self.regions) & frozenset(home))

    def rank(self, home: Iterable[str]) -> tuple[int, int, int, int, str]:
        """Display order for a truncated list, most useful first.

        The column answers *where can I watch this*, so a market-verified offer outranks an
        unscoped attribution: a network says who aired it, an offer says where to press play.
        Ordering is therefore reachable-here, then unscoped (still true, just unlocated), then
        somewhere you cannot get to — and predictions after all three, since a guess must
        never displace a fact off the end of a two-name column.

        Between platforms equally reachable from ``home``, the wider one leads: carrying a
        title across eight markets is what a primary home looks like, and it beats sorting
        a one-market reseller first purely on its initial.
        """
        home = frozenset(home)
        covered = self.reach_in(home)
        tier = 0 if covered else (1 if not self.regions else 2)
        return (int(self.predicted), tier, -covered, -len(self.regions), self.name.casefold())

    def live_in(self, regions: Iterable[str]) -> bool:
        """True when this platform is live in any of ``regions`` — or is unscoped.

        An unscoped platform matches anything on purpose: "we don't know where" must not
        read as "nowhere you can reach", which would hide every broadcast network.
        """
        return not self.regions or bool(frozenset(self.regions) & frozenset(regions))


@dataclass(frozen=True, slots=True)
class DateCell:
    """A single release milestone: when, how precise, and confirmed vs speculative."""

    when: date | None
    precision: DatePrecision
    confirmed: bool
    end: date | None = None  # upper bound for a window (e.g. 2027-2029); None for a single date


@dataclass(frozen=True, slots=True)
class TrackRow:
    """A row of the upcoming/available surface: dual dates + who/where/what + state.

    ``theatrical`` is movie-only (region-scoped); ``digital`` is the "when can I
    actually watch it" date (digital for movies, the single release for tv/games).
    ``pivot_when`` is the display/upcoming date (it may fall back across channels so a
    theatrical-only film still surfaces). ``available_when`` is stricter: the date on
    the user's *consumption* channel with no film fallback — a theatrical release does
    not make a digitally-consumed film available. Only a confirmed, elapsed
    ``available_when`` makes a work actually "available".
    """

    entity_id: str
    title: str
    kind: MediaKind
    theatrical: DateCell | None
    digital: DateCell | None
    pivot_when: date | None
    pivot_confirmed: bool
    available_resolution: Resolution  # "available to me": max over my contingencies + blockers
    blockers: tuple[str, ...]  # human labels for an unsatisfied profile / pending|never condition
    # The who/where/what graph, carried in full and role-qualified. Display limits belong to
    # the renderer, not the model: truncating here would make `cast:` silently miss a
    # third-billed actor, and flattening the role would make `director:` and `cast:` the
    # same query. See `who`/`where` below for the flat display forms.
    credits: tuple[CreditLine, ...]
    platforms: tuple[PlatformLine, ...]
    what: tuple[TagLine, ...]
    series: tuple[str, ...]
    aliases: tuple[str, ...]
    season: int | None
    part: int | None
    bucket: Bucket  # which consumption surface this lands on — the one partition rule
    freshness: Freshness | None
    has_notes: bool
    state: ConsumptionState

    @property
    def who(self) -> tuple[str, ...]:
        """Credited names, de-duplicated, in credit order (the flat display form)."""
        return tuple(dict.fromkeys(c.name for c in self.credits))

    @property
    def where(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.platforms)

    @property
    def available_when(self) -> date | None:
        """The resolved availability date (None unless fully RESOLVED) — derived."""
        return self.available_resolution.when

    @property
    def available_confirmed(self) -> bool:
        return self.available_resolution.status is ResolutionStatus.RESOLVED

    @property
    def years(self) -> frozenset[int]:
        """Every year this work can reasonably be said to belong to.

        A film with a December theatrical and a February digital spans two years, and a
        `year:` query means either — matching only the pivot would drop half of them.
        """
        cells = (self.theatrical, self.digital)
        return frozenset(
            {c.when.year for c in cells if c is not None and c.when is not None}
            | ({self.pivot_when.year} if self.pivot_when is not None else set())
        )


@dataclass(frozen=True, slots=True)
class WorkCard:
    """Everything known about one work: dates + the full who/where/what graph."""

    entity: Entity
    estimates: tuple[BestEstimate, ...]
    credits: tuple[CreditLine, ...]
    platforms: tuple[PlatformLine, ...]
    tags: tuple[TagLine, ...]
    series: tuple[str, ...] = field(default_factory=tuple)
    season: int | None = None  # this work's season/part number within its series
    derived_from: tuple[RelatedWork, ...] = ()  # what it descends from
    derivatives: tuple[RelatedWork, ...] = ()  # what descends from it
    blockers: tuple[ConditionLine, ...] = ()  # external conditions this work is BLOCKED_BY
    # where these dates can be read, split into what we can refetch and what the user has to
    # open themselves — derived, so it always matches the ids actually pinned right now.
    sources: tuple[SourceLink, ...] = ()


@dataclass(frozen=True, slots=True)
class ConditionLine:
    """An external blocker on a work's card: its name + resolution status/date."""

    name: str
    status: str  # 'resolved' | 'pending' | 'never'
    when: date | None


@dataclass(frozen=True, slots=True)
class SeasonEntry:
    """One tracked season/part of a series, for the `seasons` walk."""

    entity: Entity
    season: int | None
    part: int | None  # mid-season cut (Part/Volume/Cour N), if any
    when: date | None
    owned: bool


def freshness(fetched_at: datetime | None, today: date, settings: Settings) -> Freshness | None:
    """Green/orange/red bucket for how recently the underlying date was refreshed."""
    if fetched_at is None:
        return None
    age = (today - fetched_at.date()).days
    if age <= settings.fresh_days:
        return "fresh"
    if age <= settings.stale_days:
        return "aging"
    return "stale"


def _pick(
    estimates: Iterable[BestEstimate],
    channels: tuple[ReleaseChannel, ...] | None,
    *,
    matcher: ProfileMatcher | None = None,
) -> BestEstimate | None:
    """Best dated estimate matching ``channels`` (any if None), optionally profile-gated.

    With a ``matcher``, only estimates whose facets the user accepts survive (a hard gate —
    this is how region/platform/OS/language constrain "available to me"). Confirmed dates
    win over speculative; within the surviving pool the most *precise* date wins before the
    soonest (a coarse year's Jan-1 materialization is an artifact, not a real early date).
    """
    cands = [e for e in estimates if e.release_date and (channels is None or e.channel in channels)]
    if matcher is not None:
        cands = [e for e in cands if matcher.matches({"region": e.region, **e.contingencies})]
    confirmed = [e for e in cands if e.certainty is Certainty.CONFIRMED]
    pool = confirmed or cands
    if not pool:
        return None
    finest = max(PRECISION_RANK[e.precision] for e in pool)
    pool = [e for e in pool if PRECISION_RANK[e.precision] == finest]
    return min(pool, key=lambda e: e.release_date or date.max)


def _cell(est: BestEstimate | None) -> DateCell | None:
    if est is None:
        return None
    return DateCell(
        est.release_date, est.precision, est.certainty is Certainty.CONFIRMED, end=est.date_end
    )


def _pivot(
    theatrical: BestEstimate | None, digital: BestEstimate | None, channel: str
) -> BestEstimate | None:
    """The display/upcoming date per the configured channel.

    Falls back across channels so a film with only a theatrical date still surfaces in
    the row and in ``upcoming`` — availability is gated separately by :func:`_consumption`.
    """
    if channel == "theatrical":
        return theatrical or digital
    if channel == "digital":
        return digital or theatrical
    dated = [e for e in (theatrical, digital) if e and e.release_date]
    return min(dated, key=lambda e: e.release_date or date.max) if dated else None


def _consumption(
    theatrical: BestEstimate | None, digital: BestEstimate | None, channel: str
) -> BestEstimate | None:
    """The estimate on the user's *consumption* channel — what makes a work "available".

    Unlike :func:`_pivot` this does not fall a film back to theatrical: if you consume
    digitally, a theatrical-only release is not yet available to you. For tv/games the
    single release is carried as ``digital`` (``theatrical`` is ``None``), so it governs
    under either preference; the ``theatrical`` branch keeps that fallback for them.
    """
    if channel == "digital":
        return digital
    if channel == "theatrical":
        return theatrical or digital
    dated = [e for e in (theatrical, digital) if e and e.release_date]
    return min(dated, key=lambda e: e.release_date or date.max) if dated else None


def _condition_resolutions(db: Database, entity_id: str) -> list[Resolution]:
    """Resolutions of the external conditions this work is BLOCKED_BY.

    An unauthored condition (edge exists, no row yet) reads as PENDING. A shared condition
    node gates every work linked to it — resolve it once and all dependents unblock.
    """
    edges = db.edges_from(entity_id, RelationKind.BLOCKED_BY)
    if not edges:
        return []
    conds = db.get_conditions(e.dst_id for e in edges)
    nodes = db.get_nodes(e.dst_id for e in edges)
    out: list[Resolution] = []
    for edge in edges:
        node = nodes.get(edge.dst_id)
        if node is None:
            continue
        cond = conds.get(edge.dst_id)
        if cond is None:
            out.append(Resolution(ResolutionStatus.PENDING, blocker=node.name))
            continue
        status = ResolutionStatus(cond.status)
        out.append(
            Resolution(
                status,
                when=cond.resolve_date if status is ResolutionStatus.RESOLVED else None,
                blocker=node.name if status is not ResolutionStatus.RESOLVED else None,
            )
        )
    return out


def _track_row(
    db: Database, entity: Entity, today: date, settings: Settings, has_notes: bool
) -> TrackRow:
    estimates = best_estimates(db.iter_observations(entity.id))
    matcher = matcher_from_settings(settings)
    chan = settings.availability_channel
    # display picks (unfiltered) drive the row + upcoming ordering — nothing is hidden
    if entity.kind is MediaKind.MOVIE:
        theatrical = _pick(estimates, _THEATRICAL)
        digital = _pick(estimates, (ReleaseChannel.DIGITAL,))
    else:
        theatrical = None
        digital = _pick(estimates, None)  # the single release date
    pivot = _pivot(theatrical, digital, chan)
    # consumption picks are profile-gated: "is it released *for me* on my channel"
    if entity.kind is MediaKind.MOVIE:
        consume = _consumption(
            _pick(estimates, _THEATRICAL, matcher=matcher),
            _pick(estimates, (ReleaseChannel.DIGITAL,), matcher=matcher),
            chan,
        )
    else:
        consume = _consumption(None, _pick(estimates, None, matcher=matcher), chan)
    facet = estimate_resolution(consume)
    conditions = _condition_resolutions(db, entity.id)
    available_to_me = combine([facet, *conditions])
    # blockers shown as badges: a profile mismatch (a consumption date exists in general but
    # none match my profile) + any explicit blocking condition that hasn't resolved yet.
    blockers: tuple[str, ...] = ()
    if consume is None and _consumption(theatrical, digital, chan) is not None:
        blockers += ("not available for your profile",)
    blockers += tuple(
        c.blocker for c in conditions if c.status is not ResolutionStatus.RESOLVED and c.blocker
    )
    return TrackRow(
        entity_id=entity.id,
        title=entity.title,
        kind=entity.kind,
        theatrical=_cell(theatrical),
        digital=_cell(digital),
        pivot_when=pivot.release_date if pivot else None,
        pivot_confirmed=pivot is not None and pivot.certainty is Certainty.CONFIRMED,
        available_resolution=available_to_me,
        blockers=blockers,
        credits=tuple(_credit_lines(db, entity.id)),
        platforms=tuple(_platform_lines(db, entity.id)),
        what=tuple(_tag_lines(db, entity.id)),
        series=_series_names(db, entity.id),
        aliases=tuple(entity.aliases),
        season=entity.season,
        part=entity.part,
        bucket=bucket_of(entity.consumption_state, available_to_me, today),
        freshness=freshness(pivot.fetched_at if pivot else None, today, settings),
        has_notes=has_notes,
        state=entity.consumption_state,
    )


def track_row(
    db: Database, entity: Entity, today: date, settings: Settings, has_notes: bool = False
) -> TrackRow:
    """Rebuild one row (~0.3 ms).

    After a write, patching the single affected row beats rebuilding the whole snapshot
    by ~two orders of magnitude, which is what lets a long-running view stay live.
    """
    return _track_row(db, entity, today, settings, has_notes)


def track_rows(
    db: Database, today: date, settings: Settings, *, kind: MediaKind | None = None
) -> list[TrackRow]:
    """Every tracked work as a fully-built row, unfiltered and unsorted.

    The one expensive read (~8-10 queries per entity). Callers that need more than one
    bucket — the TUI, which filters in memory per keystroke — should build this **once**
    rather than calling ``upcoming``/``available``/``watched``, which each rebuild it.
    """
    notes = db.note_counts()
    return [
        _track_row(db, e, today, settings, notes.get(e.id, 0) > 0)
        for e in db.iter_entities()
        if kind is None or e.kind is kind
    ]


def build_vocabulary(db: Database) -> Vocabulary:
    """Snapshot the graph's completable names, ranked by how many works use each.

    The DB-touching half of query completion; ``query.suggest`` itself stays pure. ~1.8k
    strings at present, so it is held in memory and substring-scanned per keystroke.
    """

    def entries(node_kind: NodeKind) -> tuple[VocabEntry, ...]:
        return tuple(VocabEntry(value=n.name, uses=u) for n, u in db.nodes_by_kind(node_kind))

    credits: dict[CreditRole, list[VocabEntry]] = {}
    for raw_role, name, uses in db.credited_names():
        try:
            role = CreditRole(raw_role)
        except ValueError:  # a role the enum has since dropped — ignore, don't crash
            continue
        credits.setdefault(role, []).append(VocabEntry(value=name, uses=uses))

    return Vocabulary(
        credits={role: tuple(entries) for role, entries in credits.items()},
        descriptors=tuple(
            VocabEntry(value=n.name, uses=u, descriptor_kind=n.descriptor_kind)
            for n, u in db.nodes_by_kind(NodeKind.DESCRIPTOR)
        ),
        people=entries(NodeKind.PERSON),
        orgs=entries(NodeKind.ORG),
        platforms=entries(NodeKind.PLATFORM),
        regions=tuple(VocabEntry(value=r, uses=u) for r, u in db.availability_regions()),
        series=entries(NodeKind.SERIES),
        titles=tuple(VocabEntry(value=e.title) for e in db.iter_entities()),
    )


def _collapse_estimates(
    estimates: Iterable[BestEstimate], *, per_region: bool = False
) -> tuple[BestEstimate, ...]:
    """One row per channel (soonest region), so a card shows ~3 lines, not 60.

    ``per_region`` keeps the markets apart instead, which is what tech needs: a film's
    "soonest anywhere" is a useful summary because you can travel to the date, but a device
    launches and is priced per country, so collapsing Taiwan's date onto a US reader states
    something false rather than something approximate.
    """
    by_slot: dict[tuple[ReleaseChannel, str | None], BestEstimate] = {}
    for est in estimates:
        key = (est.channel, est.region if per_region else None)
        cur = by_slot.get(key)
        if cur is None or (est.release_date or date.max) < (cur.release_date or date.max):
            by_slot[key] = est
    return tuple(sorted(by_slot.values(), key=lambda e: e.release_date or date.max))


def pulled_estimates(
    db: Database, entity: Entity, manual_provider: str
) -> dict[ReleaseChannel, BestEstimate]:
    """The best estimate per channel with everything hand-authored held out.

    The edit form needs *both* sides of each date row, and the card's own estimates only
    carry the winner — which is sometimes the hand-authored one, so labelling it "pulled"
    would be wrong. Re-resolving over the sources alone is what makes the comparison honest.
    """
    sourced = [o for o in db.iter_observations(entity.id) if o.provider != manual_provider]
    return {
        est.channel: est
        for est in _collapse_estimates(
            best_estimates(sourced), per_region=entity.kind is MediaKind.TECH
        )
    }


# --- graph -> resolved lines ----------------------------------------------
def _credit_lines(db: Database, entity_id: str) -> list[CreditLine]:
    edges = db.edges_to(entity_id, RelationKind.CREDITED_ON)
    nodes = db.get_nodes(e.src_id for e in edges)
    lines = [
        CreditLine(
            e.role if isinstance(e.role, CreditRole) else CreditRole.OTHER, n.name, n.id, n.owned
        )
        for e in edges
        if (n := nodes.get(e.src_id))
    ]
    lines.sort(key=lambda c: (_ROLE_RANK.get(c.role, 7), c.name))
    return lines


def _platform_lines(db: Database, entity_id: str) -> list[PlatformLine]:
    """The where-edges folded to one line per platform, markets unioned.

    Sourced answers sort ahead of predictions: the renderers truncate this list, and a guess
    displacing a fact off the end of the column is the one ordering that must not happen.
    """
    edges = db.edges_from(entity_id, RelationKind.AVAILABLE_ON)
    nodes = db.get_nodes(e.dst_id for e in edges)
    grouped: dict[str, tuple[str, set[str], set[str], list[bool]]] = {}
    for e in edges:
        if (n := nodes.get(e.dst_id)) is None:
            continue
        _, regions, providers, tiers = grouped.setdefault(n.id, (n.name, set(), set(), []))
        if e.region:
            regions.add(e.region)
        providers.add(e.source_provider)
        tiers.append(e.source_tier is SourceTier.MODEL)
    lines = [
        # predicted only when *every* edge behind it is a guess — one sourced answer settles it
        PlatformLine(name, all(tiers), node_id, tuple(sorted(regions)), tuple(sorted(providers)))
        for node_id, (name, regions, providers, tiers) in grouped.items()
    ]
    # A stable, home-independent order so the model is deterministic; the display order that
    # actually matters is `PlatformLine.rank`, which the renderers apply with the reader's
    # own markets in hand.
    lines.sort(key=lambda p: (p.predicted, -len(p.regions), p.name.casefold()))
    return lines


def _tag_lines(db: Database, entity_id: str) -> list[TagLine]:
    edges = db.edges_from(entity_id, RelationKind.EXHIBITS)
    nodes = db.get_nodes(e.dst_id for e in edges)
    lines = [
        TagLine(
            n.name,
            n.descriptor_kind or DescriptorKind.GENRE,
            e.source_tier is SourceTier.MODEL,
            n.id,
        )
        for e in edges
        if (n := nodes.get(e.dst_id))
    ]
    # sourced genres first, soft themes after
    lines.sort(key=lambda t: (t.predicted, t.name))
    return lines


def _series_names(db: Database, entity_id: str) -> tuple[str, ...]:
    edges = db.edges_from(entity_id, RelationKind.PART_OF_SERIES)
    nodes = db.get_nodes(e.dst_id for e in edges)
    return tuple(n.name for e in edges if (n := nodes.get(e.dst_id)))


# --- public builders ------------------------------------------------------
# the three consumption buckets are exhaustive + disjoint, so nothing falls into limbo:
#   finished (watched/dropped/skipped) -> `watched`;  out + active -> `available`;
#   everything else not-finished -> `upcoming` (dated, or an explicit "no date yet").
# skipped is "done with it" even for an unreleased title: a conscious pass keeps it out of
# the upcoming queue while preserving the preference (vs. `forget`, which deletes it).
_FINISHED = (ConsumptionState.WATCHED, ConsumptionState.DROPPED, ConsumptionState.SKIPPED)
_ACTIVE = (ConsumptionState.WANT, ConsumptionState.WATCHING)


def bucket_of(state: ConsumptionState, available: Resolution, today: date) -> Bucket:
    """Classify a work into exactly one consumption bucket.

    The single partition rule, shared by the CLI views, the query language's ``is:`` field
    and the TUI — so none of them can disagree about what "available" means. Out and
    unfinished requires a *confirmed* consumption-channel date that has elapsed: a
    speculative past date does not count (we don't know it released), and neither does a
    theatrical release under a digital preference — only the strict ``available`` governs.

    Note ``UNSET`` is in neither ``_FINISHED`` nor ``_ACTIVE``, so an unset work is never
    *available*; it falls to ``upcoming`` regardless of its date, until you state an intent.
    """
    if state in _FINISHED:
        return Bucket.WATCHED
    if (
        available.when is not None
        and available.when < today
        and available.status is ResolutionStatus.RESOLVED
        and state in _ACTIVE
    ):
        return Bucket.AVAILABLE
    return Bucket.UPCOMING


def _is_available(r: TrackRow, today: date) -> bool:
    """Out and unfinished — now just a read of the bucket decided in :func:`bucket_of`."""
    return r.bucket is Bucket.AVAILABLE


def _has_firm_upcoming_date(r: TrackRow, today: date) -> bool:
    """A real future date to schedule on (vs. a stale past guess or no date at all)."""
    return r.pivot_when is not None and r.pivot_when >= today


def _upcoming_sort_key(r: TrackRow, today: date) -> date:
    """Firm future date for ordering; no-firm-date rows sort to the TBA tail."""
    if r.pivot_when is not None and r.pivot_when >= today:
        return r.pivot_when
    return date.max


def upcoming(
    db: Database,
    today: date,
    settings: Settings,
    *,
    days: int | None = None,
    kind: MediaKind | None = None,
) -> list[TrackRow]:
    """Anything not finished and not yet available — i.e. still ahead of you.

    Future-dated first (soonest first), then an explicit **no-date** tail: not-finished
    works with no firm future date (none at all, or only a stale past guess). A title can't
    be "neither available nor upcoming" — if it isn't out and isn't watched, it's upcoming.
    The ``days`` window only applies to firm-dated rows; the no-date tail is dropped when set.
    """
    rows: list[TrackRow] = []
    for r in track_rows(db, today, settings, kind=kind):
        if r.state in _FINISHED or _is_available(r, today):
            continue
        if _has_firm_upcoming_date(r, today):
            if r.pivot_when is not None and (days is None or (r.pivot_when - today).days <= days):
                rows.append(r)
        elif days is None:  # the no-date tail only shows on an unbounded view
            rows.append(r)
    rows.sort(key=lambda r: _upcoming_sort_key(r, today))
    return rows


def available(
    db: Database,
    today: date,
    settings: Settings,
    *,
    kind: MediaKind | None = None,
) -> list[TrackRow]:
    """Works that are out and unfinished (want/watching), newest first."""
    rows = [r for r in track_rows(db, today, settings, kind=kind) if _is_available(r, today)]
    rows.sort(key=lambda r: r.available_when or date.min, reverse=True)
    return rows


def watched(
    db: Database,
    today: date,
    settings: Settings,
    *,
    kind: MediaKind | None = None,
) -> list[TrackRow]:
    """Works you're done with (watched/dropped/skipped), most recently released first."""
    rows = [r for r in track_rows(db, today, settings, kind=kind) if r.state in _FINISHED]
    rows.sort(key=lambda r: r.pivot_when or date.min, reverse=True)
    return rows


# --- batch-refresh support (target selection + a before/after date diff) ----------------------
def refresh_targets(
    db: Database,
    today: date,
    settings: Settings,
    *,
    kind: MediaKind | None = None,
    state: ConsumptionState | None = None,
    since: date | None = None,
    until: date | None = None,
    days: int | None = None,
) -> list[Entity]:
    """Tracked entities matching the `rdt refresh` filters — keyed by entity, never by title.

    ``kind``/``state`` filter directly; ``since``/``until``/``days`` filter on the same display
    *pivot* date ``upcoming`` sorts on (so "everything releasing in a window" means what the user
    sees). An entity with no pivot date is excluded from any date-window filter.
    """
    dated = since is not None or until is not None or days is not None
    out: list[Entity] = []
    for e in db.iter_entities():
        if kind is not None and e.kind is not kind:
            continue
        if state is not None and e.consumption_state is not state:
            continue
        if not dated:  # no date window: kind/state (or nothing) already decided it
            out.append(e)
            continue
        pivot = _track_row(db, e, today, settings, False).pivot_when
        if pivot is None:
            continue
        if since is not None and pivot < since:
            continue
        if until is not None and pivot > until:
            continue
        if days is not None and not (today <= pivot <= today + timedelta(days=days)):
            continue
        out.append(e)
    return out


@dataclass(frozen=True, slots=True)
class DateChange:
    """One channel's date moving between two refreshes (either side may be ``None``)."""

    channel: str
    region: str
    old: date | None
    new: date | None
    new_confirmed: bool


def diff_estimates(
    before: Iterable[BestEstimate], after: Iterable[BestEstimate]
) -> list[DateChange]:
    """The per-channel date changes between two best-estimate snapshots (soonest region per
    channel, matching the display pivot). Only channels whose date actually moved are returned."""
    b = {e.channel: e for e in _collapse_estimates(before)}
    a = {e.channel: e for e in _collapse_estimates(after)}
    out: list[DateChange] = []
    for channel in sorted(set(b) | set(a), key=lambda c: c.value):
        bo, ao = b.get(channel), a.get(channel)
        old = bo.release_date if bo else None
        new = ao.release_date if ao else None
        if old != new:
            side = ao or bo
            out.append(
                DateChange(
                    channel=channel.value,
                    region=side.region if side else "WW",
                    old=old,
                    new=new,
                    new_confirmed=bool(ao and ao.certainty is Certainty.CONFIRMED),
                )
            )
    return out


def _earliest_date(db: Database, entity_id: str) -> date | None:
    dates = [
        e.release_date for e in best_estimates(db.iter_observations(entity_id)) if e.release_date
    ]
    return min(dates) if dates else None


def work_card(db: Database, entity: Entity) -> WorkCard:
    """Full who/where/what + dates for one work (the `card` surface)."""
    series_edges = db.edges_from(entity.id, RelationKind.PART_OF_SERIES)
    season = next((e.ordinal for e in series_edges if e.ordinal is not None), None)
    return WorkCard(
        entity=entity,
        estimates=_collapse_estimates(
            best_estimates(db.iter_observations(entity.id)),
            per_region=entity.kind is MediaKind.TECH,
        ),
        credits=tuple(_credit_lines(db, entity.id)),
        platforms=tuple(_platform_lines(db, entity.id)),
        tags=tuple(_tag_lines(db, entity.id)),
        series=_series_names(db, entity.id),
        season=season,
        derived_from=tuple(derived_from(db, entity.id)),
        derivatives=tuple(derivatives_of(db, entity.id)),
        blockers=_blocker_lines(db, entity.id),
        sources=work_sources(entity, _source_urls(db, entity.id)),
    )


def _source_urls(db: Database, entity_id: str) -> dict[str, str]:
    """provider -> the url it recorded, newest wins.

    Sources already write the canonical page onto every observation and nothing has ever
    shown it. For IGDB it is the only usable url, since the pinned id is numeric and its
    pages are addressed by slug.
    """
    return {
        obs.provider: obs.source_url for obs in db.iter_observations(entity_id) if obs.source_url
    }


def _blocker_lines(db: Database, entity_id: str) -> tuple[ConditionLine, ...]:
    """The external conditions a work is BLOCKED_BY, with each condition's resolution."""
    edges = db.edges_from(entity_id, RelationKind.BLOCKED_BY)
    if not edges:
        return ()
    conds = db.get_conditions(e.dst_id for e in edges)
    nodes = db.get_nodes(e.dst_id for e in edges)
    lines: list[ConditionLine] = []
    for edge in edges:
        node = nodes.get(edge.dst_id)
        if node is None:
            continue
        cond = conds.get(edge.dst_id)
        lines.append(
            ConditionLine(
                node.name,
                cond.status if cond else ResolutionStatus.PENDING.value,
                cond.resolve_date if cond else None,
            )
        )
    return tuple(lines)


def seasons_of_series(db: Database, series_node: Node) -> list[SeasonEntry]:
    """Tracked seasons/parts of a series, ordered by season number (the `seasons` walk)."""
    out: list[SeasonEntry] = []
    for edge in db.edges_to(series_node.id, RelationKind.PART_OF_SERIES):
        work = db.get_entity(edge.src_id)
        if work is not None:
            out.append(
                SeasonEntry(
                    work,
                    edge.ordinal,
                    edge.part,
                    _earliest_date(db, work.id),
                    work_is_owned(db, work.id),
                )
            )
    out.sort(key=lambda s: (s.season is None, s.season or 0, s.part or 0, s.entity.title))
    return out


@dataclass(frozen=True, slots=True)
class CreditedWork:
    """A work a node is credited on, for the one-hop `who` walk."""

    entity: Entity
    role: CreditRole
    owned: bool


def works_by_node(db: Database, node: Node) -> list[CreditedWork]:
    """Works a person/org is credited on (the first two-hop walk primitive)."""
    out: list[CreditedWork] = []
    for edge in db.edges_from(node.id, RelationKind.CREDITED_ON):
        work = db.get_entity(edge.dst_id)
        if work is not None:
            out.append(
                CreditedWork(
                    work,
                    edge.role if isinstance(edge.role, CreditRole) else CreditRole.OTHER,
                    work_is_owned(db, work.id),
                )
            )
    out.sort(key=lambda w: w.entity.title)
    return out


def work_is_owned(db: Database, entity_id: str) -> bool:
    node = db.get_node(entity_id)
    return node.owned if node else False


# --- artist radar ---------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ArtistRow:
    """A row of the creator radar: who posted most recently + their pipelines."""

    node_id: str
    name: str
    kind: NodeKind
    last_post: tuple[SocialPlatform, date] | None  # (platform, date) of newest content
    freshness: Freshness | None  # how recently we *checked* (vs when they posted)
    free: tuple[SocialPlatform, ...]
    paid: tuple[SocialPlatform, ...]
    n_works: int
    profile_url: str | None = None  # the direct profile to open (skips the recommender feed)
    latest_url: str | None = None  # the newest drop itself (the recently-active source)


def artists(
    db: Database, today: date, settings: Settings, *, sort: str = "recency"
) -> list[ArtistRow]:
    """The followed-creator radar. ``sort`` is 'recency' (newest content first) or 'name'."""
    rows: list[ArtistRow] = []
    for node in db.followed_artists():
        links = db.iter_artist_links(node.id)
        dated = [(link, link.last_post_at) for link in links if link.last_post_at is not None]
        recent = max(dated, key=lambda p: p[1]) if dated else None
        primary = recent[0] if recent else None
        last_post = (primary.platform, recent[1]) if primary and recent else None
        # the profile to open: prefer the recently-active source, else a free pipeline, else any
        profile = primary or next((lk for lk in links if lk.tier is LinkTier.FREE), None)
        profile = profile or (links[0] if links else None)
        fetched = [link.fetched_at for link in links if link.fetched_at]
        rows.append(
            ArtistRow(
                node_id=node.id,
                name=node.name,
                kind=node.node_kind,
                last_post=last_post,
                freshness=freshness(max(fetched) if fetched else None, today, settings),
                free=tuple(link.platform for link in links if link.tier is LinkTier.FREE),
                paid=tuple(link.platform for link in links if link.tier is LinkTier.PAID),
                n_works=len(db.edges_from(node.id, RelationKind.CREDITED_ON)),
                profile_url=profile.url if profile else None,
                latest_url=primary.latest_url if primary else None,
            )
        )
    if sort == "name":
        rows.sort(key=lambda r: r.name.lower())
    else:  # recency: newest post first, undated artists last (by name)
        rows.sort(key=lambda r: (r.last_post is None, _neg_ordinal(r.last_post), r.name.lower()))
    return rows


def _neg_ordinal(last_post: tuple[SocialPlatform, date] | None) -> int:
    """Sort key helper: most recent post first (descending date)."""
    return -last_post[1].toordinal() if last_post else 0


def members_of(db: Database, org: Node) -> list[Node]:
    """People who are members of a group/studio/band (the org -> people walk)."""
    ids = [e.src_id for e in db.edges_to(org.id, RelationKind.MEMBER_OF)]
    nodes = db.get_nodes(ids)
    return sorted((nodes[i] for i in ids if i in nodes), key=lambda n: n.name)


def groups_of(db: Database, person: Node) -> list[Node]:
    """Groups/studios/bands a person belongs to (the person -> orgs walk)."""
    ids = [e.dst_id for e in db.edges_from(person.id, RelationKind.MEMBER_OF)]
    nodes = db.get_nodes(ids)
    return sorted((nodes[i] for i in ids if i in nodes), key=lambda n: n.name)


# --- cross-media lineage (derived_from) -----------------------------------
@dataclass(frozen=True, slots=True)
class RelatedWork:
    """One end of a DERIVED_FROM edge, with the relation that links them."""

    node: Node
    relation: WorkRelation


def derived_from(db: Database, node_id: str) -> list[RelatedWork]:
    """What this work descends from: adaptation source, parent show, original, ..."""
    edges = db.edges_from(node_id, RelationKind.DERIVED_FROM)
    nodes = db.get_nodes(e.dst_id for e in edges)
    return [
        RelatedWork(n, e.role)
        for e in edges
        if isinstance(e.role, WorkRelation) and (n := nodes.get(e.dst_id))
    ]


def derivatives_of(db: Database, node_id: str) -> list[RelatedWork]:
    """What descends from this work: spinoffs, sequels, adaptations, tie-ins, ..."""
    edges = db.edges_to(node_id, RelationKind.DERIVED_FROM)
    nodes = db.get_nodes(e.src_id for e in edges)
    return [
        RelatedWork(n, e.role)
        for e in edges
        if isinstance(e.role, WorkRelation) and (n := nodes.get(e.src_id))
    ]
