"""One-shot lookup: resolve concrete + speculative dates for a single title.

This is the engine behind the ``rdt rd "<name>"`` command (and the global ``/rd``
skill). It deliberately does *not* touch the local seed/DB: given a free-text
name it searches the Tier-0 sources (TMDB / IGDB / Steam), picks the best
canonical match, pulls whatever dates those sources know, and then fills the gaps
with the speculative estimators in :mod:`release_tracker.deltas`:

* movies — always surface theatrical *and* a precise digital date (confirmed if
  TMDB exposes a type-4 release, else theatrical + the distributor's PVOD window;
  if even theatrical is unknown, a low-confidence guess off TMDB's primary date);
* tv — the air date plus the likely streaming platform(s);
* games — the announced date, or a coarse quarter/year collapsed to a precise
  point with a margin of error.

Every dated claim is annotated confirmed/speculative with a rough confidence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import httpx

from release_tracker.config import Settings, secret
from release_tracker.contingency import REGION_WILDCARD
from release_tracker.deltas import (
    Estimate,
    estimate_digital,
    estimate_theatrical_from_premiere,
    match_studio,
    precise_from_coarse,
)
from release_tracker.logging import get_logger
from release_tracker.matching import rank_candidate
from release_tracker.models import (
    Certainty,
    DatePrecision,
    Entity,
    MediaKind,
    ReleaseChannel,
    ReleaseObservation,
)
from release_tracker.platforms import canonical_platform, learn_predicted_platform
from release_tracker.resolve import (
    commercial_anchor,
    confirmed_theatrical_by_region,
    earliest_confirmed_theatrical,
    earliest_premiere,
)
from release_tracker.sources import justwatch, sources_for, unavailable_for
from release_tracker.sources.base import (
    Candidate,
    PlatformOffer,
    Source,
    SourceResult,
    make_client,
)
from release_tracker.sources.ddg import WebInfo, instant_answer
from release_tracker.sources.igdb import IgdbSource
from release_tracker.sources.justwatch import JustWatchAvailability
from release_tracker.sources.tmdb import TmdbSource
from release_tracker.sources.whentostream import WhenToStreamHints
from release_tracker.sources.whentostream import hints as wts_hints
from release_tracker.sources.wiki import WikiHints, wiki_hints
from release_tracker.tech import TechInfo, classify_tech, looks_like_tech, tech_info
from release_tracker.titles import search_title
from release_tracker.trends import StudioTrend, narrow_coarse

log = get_logger("lookup")

# Kinds we can auto-detect (each has at least one Tier-0 source).
# The kinds an unhinted search sweeps. Public so a caller can report on exactly the sources
# that sweep consulted, rather than guessing at them.
DETECT_KINDS: tuple[MediaKind, ...] = (MediaKind.MOVIE, MediaKind.TV, MediaKind.GAME)
# below this title-similarity we don't trust the match — caller should web-search. Public
# because it is also the line between "these hits describe what you typed" and "these are
# noise", which is what `drafts.prefill` reads a kind off.
MATCH_FLOOR = 0.4
# how far the top candidate's score must lead the runner-up to auto-pick it on the capture
# (``--track``) path. Within this band the matches are "too close to call" — we refuse to
# guess and surface the list so the user picks (via --latest / --year / --id). Plain `/rd`
# ignores this and always takes the top match; the gate only guards persistence.
_DOMINANCE = 0.15
# a clear tech name wins over a media match weaker than this (e.g. "RTX 5090"
# fuzzily hitting some film should still be treated as a GPU).
_TECH_OVERRIDE = 0.85
# region tech falls back to when none is given (a home market must be assumed).
_DEFAULT_TECH_REGION = "US"

Stance = Literal["confirmed", "speculative"]


@dataclass(slots=True, frozen=True)
class Claim:
    """One annotated, dated answer line."""

    label: str
    when: date | None
    precision: DatePrecision
    stance: Stance
    confidence: float
    margin_days: int | None
    basis: str
    region: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "date": self.when.isoformat() if self.when else None,
            "precision": self.precision.value,
            "stance": self.stance,
            "confidence": self.confidence,
            "margin_days": self.margin_days,
            "basis": self.basis,
            "region": self.region,
        }


@dataclass(slots=True, frozen=True)
class RdReport:
    """Everything the CLI / skill needs to render one lookup."""

    query: str
    found: bool
    kind: MediaKind | None = None
    matched_title: str | None = None
    url: str | None = None
    canonical: dict[str, str] = field(default_factory=dict[str, str])
    claims: tuple[Claim, ...] = ()
    # streaming = where it actually streams now (confirmed); predicted_platform =
    # the studio's typical home when nothing's streaming yet (a prediction).
    streaming: tuple[str, ...] = ()
    predicted_platform: str | None = None
    price: str | None = None
    notes: tuple[str, ...] = ()
    # tech-only: category, the region this lookup is scoped to, and the domains a
    # web search should prefer (region is a hard constraint for tech, not film/tv).
    category: str | None = None
    region: str | None = None
    preferred_sources: tuple[str, ...] = ()
    # keyless web-context, attached ONLY when the structured sources came up empty (no
    # match / no dates) — the gap where a manual web search would otherwise be needed.
    web_info: WebInfo | None = None
    # Wikipedia pointer (always, when a page exists) + raw infobox facets (only when sources
    # were sparse) — the skill mines `sections` to pre-fill contingencies, cutting manual churn.
    wiki_hints: WikiHints | None = None
    # JustWatch offer scan (film/TV): per-region buy/rent/stream offers with price + since-date,
    # plus the derived earliest-VOD date and its storefront/country (the VPN target). Attached
    # only when an offer surfaces somewhere — the structured "where + how much + how early" answer
    # that lets the skill skip a manual "where to watch" web search.
    availability: JustWatchAvailability | None = None
    # When To Stream (movies): US PVOD/SVOD dates mined from the per-film article — corroborates
    # the digital window and carries the predicted subscription-drop date + named service.
    whentostream: WhenToStreamHints | None = None

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "query": self.query,
            "found": self.found,
            "kind": self.kind.value if self.kind else None,
            "matched_title": self.matched_title,
            "url": self.url,
            "canonical": dict(self.canonical),
            "claims": [c.to_dict() for c in self.claims],
            "streaming": list(self.streaming),
            "predicted_platform": self.predicted_platform,
            "price": self.price,
            "notes": list(self.notes),
            "category": self.category,
            "region": self.region,
            "preferred_sources": list(self.preferred_sources),
        }
        if self.web_info is not None:  # lean: omit the key entirely on the happy path
            out["web_info"] = self.web_info.to_dict()
        if self.wiki_hints is not None:
            out["wiki_hints"] = self.wiki_hints.to_dict()
        if self.availability is not None:
            out["availability"] = self.availability.to_dict()
        if self.whentostream is not None:
            out["whentostream"] = self.whentostream.to_dict()
        return out


async def lookup(
    query: str,
    settings: Settings,
    *,
    kind_hint: MediaKind | None = None,
    region: str | None = None,
    season: int | None = None,
) -> RdReport:
    """Resolve a single title to confirmed + speculative dates.

    ``region`` only matters for tech (a hard per-country constraint); film/tv/game
    dates are reported as earliest-worldwide and ignore it. ``season`` pins a specific TV
    season explicitly (preferred over parsing it out of the title).
    """
    async with make_client() as client:
        if kind_hint is MediaKind.TECH:
            return await _tech_lookup(client, query, settings, region)
        if kind_hint is not None:
            cands = await _search_kind(client, query, kind_hint, settings)
            picked = (kind_hint, cands[0]) if cands else None
        else:
            picked = await _detect(client, query, settings)
            # nothing solid from the media DBs, but the title clearly names a
            # gadget? treat it as tech rather than forcing a bad film/game match.
            if looks_like_tech(query) and (picked is None or picked[1].score < _TECH_OVERRIDE):
                return await _tech_lookup(client, query, settings, region)

        if picked is None or picked[1].score < MATCH_FLOOR:
            return RdReport(
                query=query,
                found=False,
                notes=("No confident match in TMDB/IGDB/Steam — web_info attached as fallback.",),
                web_info=await instant_answer(client, query),
                wiki_hints=await wiki_hints(client, query, want_facets=True),
            )

        kind, cand = picked
        return await report_for_candidate(client, query, kind, cand, settings, season=season)


async def report_for_candidate(
    client: httpx.AsyncClient,
    query: str,
    kind: MediaKind,
    cand: Candidate,
    settings: Settings,
    *,
    season: int | None = None,
    region: str | None = None,
) -> RdReport:
    """Build the full dated report for an already-chosen (kind, candidate).

    Split out of :func:`lookup` so the capture path can pick a candidate explicitly
    (``--latest`` / ``--year`` / ``--id`` / a disambiguation choice) and still get the
    identical Tier-0 + JustWatch + WhenToStream report without re-running the search.

    ``region`` is only read for tech, where it decides which market's date leads.
    """
    # keep the raw query as the title (so "Show: Season 5" still resolves the
    # season) but pin the canonical id we just chose so pullers don't re-search.
    # An explicit `season` (the `--season` path) is authoritative over title parsing.
    entity = Entity.create(
        query, kind, external_ids={cand.id_key: cand.canonical_id}, season=season
    )

    # Concurrent, like the batch path in pipeline._pull_entity: every source joins on ids
    # already pinned on `entity` above, so none of them waits on another's output. Walked
    # serially this was the whole first second of an interactive add.
    async def _pull(src: Source) -> SourceResult | None:
        try:
            return await src.pull(client, entity, settings)
        except Exception as exc:
            log.warning("lookup.pull_error", source=src.name, error=str(exc))
            return None

    pulled = await asyncio.gather(*(_pull(src) for src in sources_for(kind)))
    results: list[SourceResult] = [r for r in pulled if r is not None]  # gather keeps order
    observations = [obs for r in results for obs in r.observations]
    canonical: dict[str, str] = {cand.id_key: cand.canonical_id}
    # What the sources want said about this pull, before any of the claim logic runs. The
    # season check lives here: it is the difference between "no air date yet" and "that
    # season is not on this show", and only the source that looked can tell them apart.
    source_notes = tuple(note for r in results for note in r.notes)
    for r in results:
        canonical.update(r.external_ids)

    tmdb_id = canonical.get("tmdb")
    predicted: str | None = None
    match kind:
        case MediaKind.MOVIE:
            claims, notes, streaming, predicted = await _movie_claims(
                client, settings, tmdb_id, observations
            )
            price = None
        case MediaKind.TV:
            claims, streaming, notes = await _tv_claims(
                client, settings, tmdb_id, observations, absent=bool(source_notes)
            )
            price = None
        case MediaKind.TECH:
            # never _game_claims: that path narrows on a *publisher's* release timing and
            # ends its miss branch with "No release info found on IGDB/Steam", which is a
            # nonsense thing to tell someone who asked about a phone.
            tech = _tech_policy(query, settings, region)
            claims, market_note = _tech_claims(observations, tech)
            notes = [market_note, *tech.notes]
            price, streaming = None, ()
        case _:  # game
            claims, price, notes = await _game_claims(
                client, settings, canonical.get("igdb"), observations
            )
            streaming = ()

    # JustWatch (film/TV) + When To Stream (movies): fetched concurrently. JustWatch gives the
    # global-earliest real VOD date + live flatrate homes; When To Stream corroborates the US
    # digital window and adds the predicted SVOD-drop date + service.
    # Stage 1 of the Wikipedia hint is a title search that depends on nothing below, so start
    # it here and let it ride alongside the offer scan rather than paying for it serially at the
    # end. `claims` is read pre-merge: a JustWatch date can only *add* to it, so at worst this
    # mines facets for a title whose only date came from a store — still a sparse-sources case.
    wiki_task = asyncio.ensure_future(wiki_hints(client, query, want_facets=not claims))

    avail: JustWatchAvailability | None = None
    wts: WhenToStreamHints | None = None
    if kind in (MediaKind.MOVIE, MediaKind.TV):
        # A season-pinned lookup asks JustWatch for *that season's* node, so the offers come
        # back scoped at the source. (This used to be skipped outright on the belief that
        # JustWatch only answers at show level. It does not, and the show-level reading is
        # actively wrong here: Yellowjackets carries Netflix on seasons 1-2 but not on 3.)
        # `search_title` because the entity title is "Show: Season N" and the search wants
        # the show.
        jw_task = (
            (
                justwatch.season_availability(
                    client,
                    search_title(cand.title),
                    season=season,
                    countries=settings.justwatch_regions,
                    year=cand.year,
                )
                if season is not None
                else justwatch.availability(
                    client,
                    cand.title,
                    kind,
                    countries=settings.justwatch_regions,
                    year=cand.year,
                    # per-market cinema dates, so a pre-order listing dated to theatrical day
                    # can't be mistaken for the digital release (see TheatricalFloor).
                    floor=justwatch.TheatricalFloor(
                        confirmed_theatrical_by_region(observations),
                        earliest_confirmed_theatrical(observations),
                    ),
                )
            )
            if settings.justwatch_enabled
            else _none()
        )
        wts_task = (
            wts_hints(client, cand.title, kind=kind, year=cand.year)
            if settings.whentostream_enabled
            else _none()
        )
        avail, wts = await asyncio.gather(jw_task, wts_task)
        year_reason = justwatch_year_mismatch(avail, cand.year) if avail is not None else None
        if avail is not None and year_reason is not None:
            # the matched title's year is implausible for this film — a same-name collision.
            note = f"JustWatch match discarded: {year_reason} — likely a wrong title."
            notes = (*notes, note)
            avail = None
        if avail is not None and justwatch_predates_theatrical(avail, observations):
            # a real VOD release can't precede the in-cinema run — this is a wrong-title match
            # (a same-named title already on digital). Drop the offer block, don't fold it.
            notes = (*notes, _collision_note(avail, observations))
            avail = None
        if avail is not None:
            claims, streaming, predicted, extra = merge_justwatch(
                list(claims), streaming, predicted, avail
            )
            notes = (*notes, *extra)
            claims, extra = merge_announced(list(claims), avail)
            notes = (*notes, *extra)
        if wts is not None:
            claims, extra = _merge_whentostream(list(claims), wts)
            notes = (*notes, *extra)

    return RdReport(
        query=query,
        found=bool(claims),
        kind=kind,
        matched_title=cand.title,
        url=cand.url,
        canonical=canonical,
        claims=tuple(claims),
        streaming=streaming,
        predicted_platform=predicted,
        price=price,
        # the sources' own words first: they explain an empty result the claim layer can only
        # describe. `_tv_claims` already stands down when one of these is present.
        notes=(*source_notes, *notes),
        # matched the title but no dates surfaced — same gap a manual search would fill
        web_info=None if claims else await instant_answer(client, query),
        # always pin the Wikipedia page; mine its facets only when sources were sparse
        wiki_hints=await wiki_task,
        availability=avail,
        whentostream=wts,
    )


# --- candidate selection --------------------------------------------------
async def _search_kind(
    client: httpx.AsyncClient, query: str, kind: MediaKind, settings: Settings
) -> list[Candidate]:
    from release_tracker.matching import candidates_for

    return await candidates_for(
        client,
        Entity.create(query, kind),
        settings,
        limit=5,
        weight=settings.popularity_weight,
    )


async def _detect(
    client: httpx.AsyncClient, query: str, settings: Settings
) -> tuple[MediaKind, Candidate] | None:
    """Pick the best (kind, candidate) across movie/tv/game.

    Ranked by title similarity, then source-native popularity. The tiebreak
    matters for titles that exist as both a film and a show ("Severance"): the
    popular one wins instead of whichever kind happened to be searched first.
    """
    best_key: tuple[float, float] | None = None
    best: tuple[MediaKind, Candidate] | None = None
    for kind in DETECT_KINDS:
        for cand in await _search_kind(client, query, kind, settings):
            key = (rank_candidate(cand, weight=settings.popularity_weight), cand.popularity)
            if best_key is None or key > best_key:
                best_key, best = key, (kind, cand)
    if best is None or best_key is None:
        return None
    kind, cand = best
    return kind, cand


# --- disambiguation (the /rd-add capture gate) ---------------------------
CandidateOutcome = Literal["picked", "ambiguous", "no_match"]


@dataclass(slots=True, frozen=True)
class CandidatePick:
    """Outcome of narrowing a candidate list for capture: pick one, refuse, or nothing fit."""

    outcome: CandidateOutcome
    cand: Candidate | None = None
    candidates: tuple[Candidate, ...] = ()  # populated on 'ambiguous' — the list to show


def _latest_anchor(c: Candidate) -> date | None:
    """The date to order 'latest' on: the full release date, else Jan-1 of the year, else None."""
    if c.release_date is not None:
        return c.release_date
    return date(c.year, 1, 1) if c.year is not None else None


def _pick_latest(pool: Sequence[Candidate]) -> Candidate | None:
    """Newest by date (score breaks a same-date tie). Undated candidates are ignored while any
    dated one exists — so 'latest' returns a real release, falling back to undated only when the
    whole pool is undated (handled by the caller, which then runs the dominance test)."""
    dated = [(a, c) for c in pool if (a := _latest_anchor(c)) is not None]
    if not dated:
        return None
    return max(dated, key=lambda ac: (ac[0], ac[1].score))[1]


def select_candidate(
    cands: Sequence[Candidate],
    *,
    latest: bool = False,
    want_year: int | None = None,
    id_pick: dict[str, str] | None = None,
    floor: float = MATCH_FLOOR,
    dominance: float = _DOMINANCE,
) -> CandidatePick:
    """Choose one candidate for capture, or refuse and surface the list.

    Authority order: an explicit ``id_pick`` (exact canonical id) → ``want_year`` narrowing →
    ``latest`` (newest dated) → a dominance test on scores. Explicit selectors are user-directed
    and never return ``ambiguous``; only the fall-through dominance path does (the "too close to
    call, you pick" case). ``no_match`` means nothing cleared the floor or satisfied a selector.

    ``want_year``/``latest`` pick among the **contenders** — matches within ``dominance`` of the
    top score — not everything above the floor. Otherwise "latest" would happily grab a newer but
    *weak* match (a same-year promo/featurette whose title merely contains the query) over the real
    film. The floor still gates the whole thing; the band just keeps a narrowing selector honest.
    """
    # an explicit id is fully user-directed — scan the whole list; the floor doesn't apply.
    if id_pick:
        for c in cands:
            if c.canonical_id == id_pick.get(c.id_key):
                return CandidatePick("picked", cand=c)
        return CandidatePick("no_match")

    pool = [c for c in cands if c.score >= floor]
    if not pool:
        return CandidatePick("no_match")
    top = max(c.score for c in pool)
    contenders = [c for c in pool if c.score >= top - dominance]  # the real, close-scoring matches

    if want_year is not None:
        contenders = [c for c in contenders if c.year == want_year]
        if not contenders:
            return CandidatePick("no_match")

    if latest and (chosen := _pick_latest(contenders)) is not None:
        return CandidatePick("picked", cand=chosen)

    # a single contender means the top clearly leads (everything else is >dominance below it) →
    # auto-pick; two or more close matches are genuinely ambiguous → surface them.
    ranked = sorted(contenders, key=lambda c: c.score, reverse=True)
    if len(ranked) == 1:
        return CandidatePick("picked", cand=ranked[0])
    return CandidatePick("ambiguous", candidates=tuple(ranked))


async def capture_candidates(
    client: httpx.AsyncClient,
    query: str,
    settings: Settings,
    *,
    kind_hint: MediaKind | None,
    limit: int = 8,
) -> list[tuple[MediaKind, Candidate]]:
    """Ranked ``(kind, candidate)`` matches for the capture path: one kind when hinted, else a
    cross-kind sweep. Mirrors what :func:`lookup` searches but returns the whole list (not just
    the winner) so the caller can disambiguate before persisting."""
    from release_tracker.matching import candidates_for, rank_candidate

    kinds = (kind_hint,) if kind_hint is not None else DETECT_KINDS

    async def for_kind(kind: MediaKind) -> list[tuple[MediaKind, Candidate]]:
        found = await candidates_for(
            client,
            Entity.create(query, kind),
            settings,
            limit=limit,
            weight=settings.popularity_weight,
        )
        return [(kind, c) for c in found]

    # An unhinted sweep is three independent kind searches; running them concurrently turns
    # what was a chain of round trips into roughly one, which is what makes type-ahead viable.
    batches = await asyncio.gather(*(for_kind(k) for k in kinds))
    out = [pair for batch in batches for pair in batch]
    out.sort(key=lambda kc: rank_candidate(kc[1], weight=settings.popularity_weight), reverse=True)
    return out


# --- tech -----------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _TechPolicy:
    """The per-category scaffold every tech answer carries, dated or not."""

    info: TechInfo
    region: str
    notes: tuple[str, ...]


def _tech_policy(query: str, settings: Settings, region: str | None) -> _TechPolicy:
    """Category, the region this lookup is scoped to, and how to reason about price.

    Shared by the dated and the date-less paths on purpose. Knowing *when* the Xperia ships
    says nothing about *what it costs*, so the predecessor-MSRP guidance stays relevant even
    on a hit — and region matters more once there is a date, not less.
    """
    info = tech_info(classify_tech(query))
    # The ANY/* sentinel in RDT_REGIONS means "region doesn't gate me — I use a VPN". That is
    # true of a stream and categorically false of a device: you can't VPN a phone into your
    # hands. So tech reads past the sentinel to a real market rather than scoping a launch
    # date to "ANY", which would be no scope at all.
    home = next((r for r in settings.regions if r not in REGION_WILDCARD), None)
    reg = (region or home or _DEFAULT_TECH_REGION).upper()
    price_note = (
        "Anchor price to the predecessor's MSRP in this region, but adjust for component / "
        "part-cost swings (DRAM/NAND, panels, silicon node, tariffs, FX) — last-gen price is a "
        "weak prior here."
        if info.price_volatile
        else (
            "Anchor price to the predecessor's MSRP in this region; "
            "part-cost swings are minor here."
        )
    )
    return _TechPolicy(
        info=info,
        region=reg,
        notes=(
            f"Region is a HARD constraint for tech: launch date and price are per-country "
            f"(tariffs/taxes/FX/carrier deals) and can't be VPN-bypassed — scoped to {reg}.",
            f"Category: {info.label}. Prefer sources: {', '.join(info.preferred_sources)}.",
            info.note,
            price_note,
        ),
    )


async def _tech_lookup(
    client: httpx.AsyncClient, query: str, settings: Settings, region: str | None
) -> RdReport:
    """A device: Wikidata's dates if it knows the thing, the search scaffold if not.

    The floor matters more here than elsewhere. ``wbsearchentities`` matches every item in
    Wikidata — people, concepts, ships — and a Wikidata candidate has no popularity to break
    ties with, so title similarity is doing all the work. Below the floor we would rather
    say "go and search" than hand back some unrelated item's release date.
    """
    cands = await _search_kind(client, query, MediaKind.TECH, settings)
    top = cands[0] if cands else None
    if top is None or top.score < MATCH_FLOOR:
        return _tech_report(query, settings, region)
    return await report_for_candidate(client, query, MediaKind.TECH, top, settings, region=region)


def _tech_report(query: str, settings: Settings, region: str | None) -> RdReport:
    """The date-less tech answer: the policy scaffold and nothing more.

    Reached when Wikidata has no item for the device, which is the common case — it knows a
    small fraction of consumer tech. This never carries dates; it tells the /rd skill *how*
    to web-search (region-scoped, preferred domains) and *how* to reason about price.
    """
    policy = _tech_policy(query, settings, region)
    return RdReport(
        query=query,
        found=False,
        kind=MediaKind.TECH,
        matched_title=query,
        category=policy.info.label,
        region=policy.region,
        preferred_sources=policy.info.preferred_sources,
        notes=(
            "No Wikidata item for this device — no structured date, so web-search it.",
            *policy.notes,
        ),
    )


def _tech_claims(obs: list[ReleaseObservation], policy: _TechPolicy) -> tuple[list[Claim], str]:
    """Dated answers for a device — the requested market first, other markets as context.

    Region is not decoration here. A phone launches and is priced per country, so a US date
    is not an answer for someone in DE; the claims stay labelled by market rather than being
    collapsed into one "release date" that silently belongs to somebody else's country.
    """
    dated = [o for o in obs if o.release_date is not None]
    if not dated:
        return [], "Wikidata has the device but no release date on it yet."

    def rank(o: ReleaseObservation) -> tuple[int, date]:
        # the user's market first, then worldwide, then everyone else's
        home = 0 if o.region == policy.region else (1 if o.region == "WW" else 2)
        assert o.release_date is not None
        return home, o.release_date

    claims = [
        Claim(
            "Release" if o.region in (policy.region, "WW") else f"Release ({o.region})",
            o.release_date,
            o.precision,
            "confirmed" if o.certainty is Certainty.CONFIRMED else "speculative",
            o.confidence,
            None,
            f"Wikidata{' · window' if o.date_end is not None else ''}",
            o.region,
        )
        for o in sorted(dated, key=rank)
    ]
    markets = {o.region for o in dated}
    note = (
        f"Dated for {policy.region} specifically."
        if policy.region in markets
        else (
            f"No {policy.region} date on Wikidata — showing "
            f"{', '.join(sorted(markets))}. Confirm the local launch before relying on it."
        )
    )
    return claims, note


async def _none() -> None:
    """An already-resolved None — lets a disabled source slot into asyncio.gather cleanly."""
    return None


# --- JustWatch wrong-title guards (year-sanity + a VOD date can't precede the cinema run) ----
def justwatch_year_mismatch(avail: JustWatchAvailability, film_year: int | None) -> str | None:
    """Year-sanity on a JustWatch match: the matched title's year must sit within ±1 of the
    film's (when known), and its earliest VOD can't predate the release year by more than a year
    (a buy/rent never precedes the film). Either failing means a same-name collision — returns a
    short reason for the note, else None.

    Anchors on the *matched* title's own year when the film's is unknown (not yet dated in TMDB),
    so an absurdly old VOD date — e.g. a 2001 offer surfacing for a 2026 film — is still caught.

    The symmetric check does not apply to a season: JustWatch reports the *show's* first year
    while ``film_year`` is the *season's*, and a long-running series legitimately puts decades
    between them. Season 3 of a 2021 show airing in 2025 is not a collision, and treating it as
    one discarded the offers for every season past the second.
    """
    if (
        avail.season is None
        and film_year is not None
        and avail.year is not None
        and abs(avail.year - film_year) > 1
    ):
        return f"matched title year {avail.year} is far from the film's {film_year}"
    # A show cannot begin after one of its own seasons aired; that direction still catches a
    # collision with a same-named *newer* title.
    if avail.season is not None and film_year is not None and avail.year is not None:
        if avail.year > film_year + 1:
            return f"matched show year {avail.year} postdates the season's {film_year}"
        return None
    anchor = film_year if film_year is not None else avail.year
    vod = avail.earliest_vod
    if vod is not None and anchor is not None and vod.year < anchor - 1:
        return f"earliest VOD {vod.isoformat()} predates the {anchor} release by over a year"
    return None


# --- JustWatch wrong-title guard (a VOD date can't precede the cinema run) ----
def justwatch_predates_theatrical(
    avail: JustWatchAvailability, obs: list[ReleaseObservation]
) -> bool:
    """True when JustWatch's earliest VOD lands *before* the earliest confirmed theatrical anywhere.

    That ordering is physically impossible for the real film, so it flags a same-name collision
    (the aggregator matched a different title already on digital). No confirmed theatrical, or no
    dated VOD, means we can't make the call → not a collision (be conservative, keep the data).
    """
    vod = avail.earliest_vod
    floor = earliest_confirmed_theatrical(obs)
    return vod is not None and floor is not None and vod < floor


def _collision_note(avail: JustWatchAvailability, obs: list[ReleaseObservation]) -> str:
    floor = earliest_confirmed_theatrical(obs)
    vod = avail.earliest_vod
    assert vod is not None and floor is not None  # guaranteed by the guard that gates this call
    return (
        f"JustWatch match discarded: earliest VOD {vod.isoformat()} ({avail.earliest_vod_country}) "
        f"predates the confirmed theatrical {floor.isoformat()} — almost certainly a different "
        "title sharing the name."
    )


# --- JustWatch merge (real store offers beat estimates / predictions) ----
def merge_justwatch(
    claims: list[Claim],
    streaming: tuple[str, ...],
    predicted: str | None,
    avail: JustWatchAvailability,
) -> tuple[list[Claim], tuple[str, ...], str | None, tuple[str, ...]]:
    """Fold JustWatch ground truth into the claims/streaming: earliest real VOD date as the
    headline digital claim, and live flatrate homes in place of the streaming prediction."""
    notes: list[str] = []
    jw = avail.earliest_vod
    if jw is not None:
        tmdb_digital = next(
            (c for c in claims if c.label == "Digital" and c.when is not None), None
        )
        where = f"{avail.earliest_vod_platform} ({avail.earliest_vod_country})"
        jw_claim = Claim(
            f"Digital (earliest · {avail.earliest_vod_country})",
            jw,
            DatePrecision.EXACT,
            "confirmed",
            0.95,
            None,
            f"JustWatch · {where}",
            avail.earliest_vod_country,
        )
        if tmdb_digital is not None and tmdb_digital.when is not None:
            # TMDB's Digital type is the studio's own date for the market; a store date is derived
            # from when a *listing* appeared, so it is added beside that date, never over it — one
            # mis-dated offer would otherwise erase the only piece of ground truth on the report.
            # Any *guessed* digital still goes: a real store offer settles that much.
            claims = [c for c in claims if not _is_speculative_digital(c)]
            if jw < tmdb_digital.when:
                # genuinely earlier in some market — this is the VPN answer, so keep both lines.
                claims.append(jw_claim)
                notes.append(
                    f"JustWatch has it earlier in {avail.earliest_vod_country} "
                    f"({jw.isoformat()}, {where}) than TMDB's {tmdb_digital.when.isoformat()}."
                )
            else:
                notes.append(
                    f"JustWatch corroborates digital availability (earliest store: {where})."
                )
        else:
            # no confirmed digital from TMDB — the store offer is the best evidence there is.
            claims = [c for c in claims if not c.label.startswith("Digital")]
            claims.append(jw_claim)
    # Live flatrate homes are ground truth, so a distributor-based prediction is moot — but
    # keep the (region-scoped) `streaming` headline as-is; the full cross-region subscription
    # picture lives in the availability block, region-tagged, rather than flooding one line.
    if avail.streaming_platforms:
        predicted = None
    if avail.season is not None:
        # A season-scoped lookup must not answer with the *show's* streaming homes. TMDB's
        # watch-providers are show-level, so they would list every service that has ever
        # carried the series: Yellowjackets S4 is on Paramount+ alone, but the show line
        # claims Netflix, which carries seasons 1-2 only. Empty is a real answer here — a
        # season that has not dropped streams nowhere, and saying otherwise is the failure.
        streaming = _season_streaming(avail)
    return claims, streaming, predicted, tuple(notes)


def _season_streaming(avail: JustWatchAvailability) -> tuple[str, ...]:
    """The season's own subscription homes, canonicalised and deduped."""
    return tuple(dict.fromkeys(canonical_platform(p) for p in avail.streaming_platforms))


# JustWatch releaseType -> the claim label it belongs under. A physical date is a real, dated
# fact about a season and worth carrying, but it is not a "when can I stream this" answer.
_ANNOUNCED_LABEL: dict[str, str] = {"digital": "Digital", "physical": "Physical"}


def merge_announced(
    claims: list[Claim], avail: JustWatchAvailability
) -> tuple[list[Claim], tuple[str, ...]]:
    """Fold a season's *announced* (not-yet-live) platform release into the claims.

    Distinct from :func:`merge_justwatch`, which reads dates off offers that already exist.
    Here there is no offer to corroborate — the season has not dropped — but the platform has
    published a date, and that is exactly what a lookup on an upcoming season is asking for.
    """
    announced = avail.announced
    if announced is None:
        return claims, ()
    label = _ANNOUNCED_LABEL.get(announced.release_type)
    if label is None:  # a release type we have no channel for says nothing useful
        return claims, ()
    where = f"{announced.platform} ({announced.country})"
    if any(c.label.startswith(label) and c.when == announced.when for c in claims):
        return claims, ()  # already known from a source that got there first
    claims.append(
        Claim(
            f"{label} (announced · {announced.country})",
            announced.when,
            DatePrecision.EXACT,
            "confirmed",
            # a shade under the 0.95 a *live* offer earns: this one has not happened yet
            0.9,
            None,
            f"JustWatch · {where}",
            announced.country,
        )
    )
    season = f"season {avail.season}" if avail.season is not None else "this title"
    return claims, (f"{announced.platform} has announced {season} for {where}.",)


def _is_speculative_digital(c: Claim) -> bool:
    return c.label.startswith("Digital") and c.stance == "speculative"


# --- When To Stream merge (US PVOD corroboration + the predicted SVOD-drop date) ---
def _merge_whentostream(
    claims: list[Claim], wts: WhenToStreamHints
) -> tuple[list[Claim], tuple[str, ...]]:
    """Add the SVOD-drop claim (US subscription date + service) and corroborate the digital
    window with the US PVOD date — flagging a discrepancy rather than silently overriding."""
    notes: list[str] = []
    if wts.svod_date is not None:  # the subscription drop — not predicted by TMDB/JustWatch
        label = f"Streaming (SVOD · {wts.svod_service})" if wts.svod_service else "Streaming (SVOD)"
        basis = "WhenToStream (US)" + (f" · {wts.svod_service}" if wts.svod_service else "")
        claims.append(
            Claim(label, wts.svod_date, DatePrecision.EXACT, "confirmed", 0.8, None, basis, "US")
        )
    if wts.pvod is not None:
        digital = next(
            (c for c in claims if c.label.startswith("Digital") and c.when is not None), None
        )
        if digital is None or digital.when is None:
            # no digital date from any source — the US PVOD becomes our confirmed digital line.
            claims.append(
                Claim(
                    "Digital (US PVOD)",
                    wts.pvod,
                    DatePrecision.EXACT,
                    "confirmed",
                    0.8,
                    None,
                    "WhenToStream PVOD (US)",
                    "US",
                )
            )
        elif abs((wts.pvod - digital.when).days) <= 7:
            notes.append(
                f"WhenToStream PVOD (US) {wts.pvod.isoformat()} corroborates the digital date."
            )
        else:
            notes.append(
                f"WhenToStream PVOD (US) {wts.pvod.isoformat()} differs from the earliest digital "
                f"{digital.when.isoformat()} — US window vs earliest regional offer; verify."
            )
    return claims, tuple(notes)


# --- per-kind claim builders ---------------------------------------------
async def _movie_claims(
    client: httpx.AsyncClient,
    settings: Settings,
    tmdb_id: str | None,
    obs: list[ReleaseObservation],
) -> tuple[list[Claim], list[str], tuple[str, ...], str | None]:
    notes: list[str] = []
    claims: list[Claim] = []
    premiere = earliest_premiere(obs)  # festival/event premiere — informational, never an anchor
    theatrical = commercial_anchor(obs)  # wide/limited commercial release (US-preferred)
    digital = min(
        (o for o in obs if o.channel is ReleaseChannel.DIGITAL and o.release_date),
        key=_obs_date,
        default=None,
    )

    key = secret(settings.tmdb_api_key)
    src = TmdbSource()
    # one detail call up front: the distributor feeds both the digital-window
    # estimate and the streaming-home prediction, plus the no-theatrical fallback.
    meta = await src.movie_meta(client, key, tmdb_id) if (tmdb_id and key) else None
    studio = match_studio(meta.studios) if meta else None

    # the wide date estimated *from* a premiere — only when no commercial date exists yet.
    est_wide = (
        estimate_theatrical_from_premiere(premiere.release_date)
        if premiere and premiere.release_date and theatrical is None
        else None
    )

    if premiere and premiere.release_date:  # shown distinctly so the event date isn't lost
        claims.append(
            Claim(
                "Premiere",
                premiere.release_date,
                premiere.precision,
                "confirmed",
                0.9,
                None,
                f"TMDB festival/premiere ({premiere.region})",
                premiere.region,
            )
        )

    if theatrical and theatrical.release_date:
        claims.append(
            Claim(
                "Theatrical",
                theatrical.release_date,
                theatrical.precision,
                "confirmed",
                0.9,
                None,
                f"TMDB ({theatrical.region})",
                theatrical.region,
            )
        )
    elif est_wide is not None:  # only a premiere so far → estimate the wide release from it
        claims.append(
            Claim(
                "Theatrical (est.)",
                est_wide.when,
                DatePrecision.EXACT,
                "speculative",
                est_wide.confidence,
                est_wide.margin_days,
                est_wide.basis,
            )
        )

    if digital and digital.release_date:
        claims.append(
            Claim(
                "Digital",
                digital.release_date,
                digital.precision,
                "confirmed",
                0.9,
                None,
                f"TMDB type-4 ({digital.region})",
                digital.region,
            )
        )
    elif theatrical and theatrical.release_date:
        est = estimate_digital(theatrical.release_date, studio)
        claims.append(
            Claim(
                "Digital (est.)",
                est.when,
                DatePrecision.EXACT,
                "speculative",
                est.confidence,
                est.margin_days,
                est.basis,
            )
        )
    elif est_wide is not None:
        # CHAIN: premiere → estimated wide theatrical → digital (uncertainty compounds, so the
        # digital leg is built on a guessed theatrical and lands at low confidence).
        est = estimate_digital(est_wide.when, studio, theatrical_confirmed=False)
        claims.append(
            Claim(
                "Digital (est.)",
                est.when,
                DatePrecision.EXACT,
                "speculative",
                est.confidence,
                est.margin_days,
                f"premiere-chained: {est.basis}",
            )
        )
    elif meta and meta.primary_date:
        # nothing concrete yet — best-guess theatrical off TMDB's primary date.
        claims.append(
            Claim(
                "Theatrical (guess)",
                meta.primary_date,
                DatePrecision.EXACT,
                "speculative",
                0.3,
                30,
                f"TMDB primary date, status={meta.status}",
            )
        )
        est = estimate_digital(meta.primary_date, studio, theatrical_confirmed=False)
        claims.append(
            Claim(
                "Digital (est.)",
                est.when,
                DatePrecision.EXACT,
                "speculative",
                est.confidence,
                est.margin_days,
                est.basis,
            )
        )
    else:
        notes.append("No theatrical or digital date found.")

    if studio:
        notes.append(f"Distributor: {studio}.")

    # streaming: stage 2 (actual providers, confirmed) then stage 1 (studio
    # prediction) only when nothing is streaming yet.
    streaming = (
        _platform_names(await src.movie_platforms(client, key, tmdb_id, settings.provider_regions))
        if (tmdb_id and key)
        else ()
    )
    # learns + persists a streaming home for any distributor not in the hand table.
    predicted = (
        await learn_predicted_platform(meta.studios, settings) if (meta and not streaming) else None
    )
    return claims, notes, streaming, predicted


def _platform_names(offers: tuple[PlatformOffer, ...]) -> tuple[str, ...]:
    """The distinct service names, order preserved — the flat `streaming` headline.

    The per-market detail the sources now return lives in the availability block, which is
    region-tagged; this one line has no room for it and would just repeat a name per market.
    """
    return tuple(dict.fromkeys(o.name for o in offers))


async def _tv_claims(
    client: httpx.AsyncClient,
    settings: Settings,
    tmdb_id: str | None,
    obs: list[ReleaseObservation],
    *,
    absent: bool = False,
) -> tuple[list[Claim], tuple[str, ...], list[str]]:
    """Claims for a TV work. ``absent`` means the source already explained the empty result."""
    notes: list[str] = []
    claims: list[Claim] = []
    dated = [o for o in obs if o.release_date]
    if dated:
        o = min(dated, key=_obs_date)
        confirmed = o.certainty is Certainty.CONFIRMED
        claims.append(
            Claim(
                "Release",
                o.release_date,
                o.precision,
                "confirmed" if confirmed else "speculative",
                0.85 if confirmed else 0.5,
                None,
                o.source_name or "TMDB",
                o.region,
            )
        )
    elif missing := unavailable_for((MediaKind.TV,), settings):
        # Saying "TMDB has no date" when TMDB was never asked sends the reader off to
        # check a source that was never consulted.
        notes.extend(sorted(missing.values()))
    elif not absent:
        # Only when nothing better is known. A season the show does not carry has already
        # been explained by the source, and "no air date yet" would contradict it — the date
        # is not late, the season is not there.
        notes.append("No air date on TMDB yet.")

    streaming: tuple[str, ...] = ()
    key = secret(settings.tmdb_api_key)
    if tmdb_id and key:
        streaming = _platform_names(
            await TmdbSource().tv_platforms(client, key, tmdb_id, settings.provider_regions)
        )
    return claims, streaming, notes


_COARSE = (DatePrecision.YEAR, DatePrecision.QUARTER)


async def _game_claims(
    client: httpx.AsyncClient,
    settings: Settings,
    igdb_id: str | None,
    obs: list[ReleaseObservation],
) -> tuple[list[Claim], str | None, list[str]]:
    notes: list[str] = []
    claims: list[Claim] = []
    price = next((str(o.price) for o in obs if o.price is not None), None)
    dated = [o for o in obs if o.release_date]
    if dated:
        # prefer an exact date; otherwise the soonest coarse one, made precise.
        o = min(dated, key=lambda x: (x.precision is not DatePrecision.EXACT, _obs_date(x)))
        assert o.release_date is not None
        confirmed = o.precision is DatePrecision.EXACT and o.certainty is Certainty.CONFIRMED
        # only a coarse, unconfirmed date can benefit from a studio-timing bias.
        refined = (
            await _studio_narrowed(client, settings, igdb_id, o.release_date, o.precision)
            if o.precision in _COARSE
            else None
        )
        est = refined or precise_from_coarse(o.release_date, o.precision)
        confidence = est.confidence if confirmed else round(est.confidence * 0.8, 2)
        margin = None if confirmed else (est.margin_days or 7)
        basis = est.basis if confirmed else f"{est.basis} ({o.source_name or 'store'})"
        claims.append(
            Claim(
                "Release",
                est.when,
                DatePrecision.EXACT,
                "confirmed" if confirmed else "speculative",
                confidence,
                margin,
                basis,
                o.region,
            )
        )
        if refined is not None:
            notes.append("Estimate biased toward the publisher's historical release timing.")
    elif obs:
        notes.append("Listed but no concrete date yet (TBA).")
    elif missing := unavailable_for((MediaKind.GAME,), settings):
        notes.extend(sorted(missing.values()))
    else:
        notes.append("No release info found on IGDB/Steam.")
    return claims, price, notes


async def _studio_narrowed(
    client: httpx.AsyncClient,
    settings: Settings,
    igdb_id: str | None,
    when: date,
    precision: DatePrecision,
) -> Estimate | None:
    """Mine (cached) the publisher's release-timing trend and narrow the coarse date."""
    trend = await _game_trend(client, settings, igdb_id)
    return narrow_coarse(when, precision, trend) if trend else None


async def _game_trend(
    client: httpx.AsyncClient, settings: Settings, igdb_id: str | None
) -> StudioTrend | None:
    """Publisher release-timing trend for an IGDB game, mined on demand and cached."""
    if not igdb_id:
        return None
    return await IgdbSource().studio_trend(client, settings, igdb_id)


def _obs_date(o: ReleaseObservation) -> date:
    # only called on observations already filtered to release_date is not None
    assert o.release_date is not None
    return o.release_date
