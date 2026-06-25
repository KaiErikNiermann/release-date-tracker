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
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import httpx

from release_tracker.config import Settings
from release_tracker.deltas import (
    Estimate,
    estimate_digital,
    estimate_theatrical_from_premiere,
    match_studio,
    precise_from_coarse,
)
from release_tracker.logging import get_logger
from release_tracker.matching import score_candidate
from release_tracker.models import (
    Certainty,
    DatePrecision,
    Entity,
    MediaKind,
    ReleaseChannel,
    ReleaseObservation,
)
from release_tracker.platforms import learn_predicted_platform
from release_tracker.resolve import commercial_anchor, earliest_premiere
from release_tracker.sources import justwatch, sources_for
from release_tracker.sources.base import Candidate, SourceResult, make_client
from release_tracker.sources.ddg import WebInfo, instant_answer
from release_tracker.sources.igdb import IgdbSource
from release_tracker.sources.justwatch import JustWatchAvailability
from release_tracker.sources.tmdb import TmdbSource
from release_tracker.sources.whentostream import WhenToStreamHints
from release_tracker.sources.whentostream import hints as wts_hints
from release_tracker.sources.wiki import WikiHints, wiki_hints
from release_tracker.tech import classify_tech, looks_like_tech, tech_info
from release_tracker.trends import StudioTrend, narrow_coarse

log = get_logger("lookup")

# Kinds we can auto-detect (each has at least one Tier-0 source).
_DETECT_KINDS: tuple[MediaKind, ...] = (MediaKind.MOVIE, MediaKind.TV, MediaKind.GAME)
# below this title-similarity we don't trust the match — caller should web-search.
_MATCH_FLOOR = 0.4
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
    if kind_hint is MediaKind.TECH:
        return _tech_report(query, settings, region)

    async with make_client() as client:
        if kind_hint is not None:
            cands = await _search_kind(client, query, kind_hint, settings)
            picked = (kind_hint, cands[0]) if cands else None
        else:
            picked = await _detect(client, query, settings)
            # nothing solid from the media DBs, but the title clearly names a
            # gadget? treat it as tech rather than forcing a bad film/game match.
            if looks_like_tech(query) and (picked is None or picked[1].score < _TECH_OVERRIDE):
                return _tech_report(query, settings, region)

        if picked is None or picked[1].score < _MATCH_FLOOR:
            return RdReport(
                query=query,
                found=False,
                notes=("No confident match in TMDB/IGDB/Steam — web_info attached as fallback.",),
                web_info=await instant_answer(client, query),
                wiki_hints=await wiki_hints(client, query, want_facets=True),
            )

        kind, cand = picked
        # keep the raw query as the title (so "Show: Season 5" still resolves the
        # season) but pin the canonical id we just chose so pullers don't re-search.
        # An explicit `season` (the `--season` path) is authoritative over title parsing.
        entity = Entity.create(
            query, kind, external_ids={cand.id_key: cand.canonical_id}, season=season
        )

        results: list[SourceResult] = []
        for src in sources_for(kind):
            try:
                results.append(await src.pull(client, entity, settings))
            except Exception as exc:
                log.warning("lookup.pull_error", source=src.name, error=str(exc))
        observations = [obs for r in results for obs in r.observations]
        canonical: dict[str, str] = {cand.id_key: cand.canonical_id}
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
                claims, streaming, notes = await _tv_claims(client, settings, tmdb_id, observations)
                price = None
            case _:  # game / tech
                claims, price, notes = await _game_claims(
                    client, settings, canonical.get("igdb"), observations
                )
                streaming = ()

        # JustWatch (film/TV) + When To Stream (movies): fetched concurrently. JustWatch gives the
        # global-earliest real VOD date + live flatrate homes; When To Stream corroborates the US
        # digital window and adds the predicted SVOD-drop date + service.
        avail: JustWatchAvailability | None = None
        wts: WhenToStreamHints | None = None
        if kind in (MediaKind.MOVIE, MediaKind.TV):
            jw_task = (
                justwatch.availability(
                    client, cand.title, kind, countries=settings.justwatch_regions, year=cand.year
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
            if avail is not None:
                claims, streaming, predicted, extra = _merge_justwatch(
                    list(claims), streaming, predicted, avail
                )
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
            notes=tuple(notes),
            # matched the title but no dates surfaced — same gap a manual search would fill
            web_info=None if claims else await instant_answer(client, query),
            # always pin the Wikipedia page; mine its facets only when sources were sparse
            wiki_hints=await wiki_hints(client, query, want_facets=not claims),
            availability=avail,
            whentostream=wts,
        )


# --- candidate selection --------------------------------------------------
async def _search_kind(
    client: httpx.AsyncClient, query: str, kind: MediaKind, settings: Settings
) -> list[Candidate]:
    from release_tracker.matching import candidates_for

    return await candidates_for(client, Entity.create(query, kind), settings, limit=5)


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
    for kind in _DETECT_KINDS:
        for cand in await _search_kind(client, query, kind, settings):
            key = (score_candidate(query, None, cand, kind), cand.popularity)
            if best_key is None or key > best_key:
                best_key, best = key, (kind, cand)
    if best is None or best_key is None:
        return None
    kind, cand = best
    cand.score = best_key[0]
    return kind, cand


# --- tech (search-first, no Tier-0 DB) -----------------------------------
def _tech_report(query: str, settings: Settings, region: str | None) -> RdReport:
    """Build the policy scaffold for a tech lookup: category, region, search guidance.

    There is no structured tech source, so this never carries dates — it tells the
    /rd skill *how* to web-search (region-scoped, preferred domains) and *how* to
    reason about price (predecessor anchor, adjusted for part-cost swings).
    """
    category = classify_tech(query)
    info = tech_info(category)
    reg = (region or (settings.regions[0] if settings.regions else _DEFAULT_TECH_REGION)).upper()
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
    notes = (
        f"Tech has no Tier-0 DB — web-search scoped to {reg}. Region is a HARD constraint for "
        "tech: launch date and price are per-country (tariffs/taxes/FX/carrier deals) and can't "
        "be VPN-bypassed.",
        f"Category: {info.label}. Prefer sources: {', '.join(info.preferred_sources)}.",
        info.note,
        price_note,
    )
    return RdReport(
        query=query,
        found=False,
        kind=MediaKind.TECH,
        matched_title=query,
        category=info.label,
        region=reg,
        preferred_sources=info.preferred_sources,
        notes=notes,
    )


async def _none() -> None:
    """An already-resolved None — lets a disabled source slot into asyncio.gather cleanly."""
    return None


# --- JustWatch merge (real store offers beat estimates / predictions) ----
def _merge_justwatch(
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
        if tmdb_digital is not None and tmdb_digital.when is not None and tmdb_digital.when <= jw:
            # TMDB already has an equal/earlier confirmed digital — keep it, just drop any guess.
            claims = [c for c in claims if not _is_speculative_digital(c)]
            notes.append(f"JustWatch corroborates digital availability (earliest store: {where}).")
        else:
            # JustWatch is the earliest confirmed VOD — it supersedes any TMDB/estimate digital.
            claims = [c for c in claims if not c.label.startswith("Digital")]
            claims.append(
                Claim(
                    f"Digital (earliest · {avail.earliest_vod_country})",
                    jw,
                    DatePrecision.EXACT,
                    "confirmed",
                    0.95,
                    None,
                    f"JustWatch · {where}",
                    avail.earliest_vod_country,
                )
            )
    # Live flatrate homes are ground truth, so a distributor-based prediction is moot — but
    # keep the (region-scoped) `streaming` headline as-is; the full cross-region subscription
    # picture lives in the availability block, region-tagged, rather than flooding one line.
    if avail.streaming_platforms:
        predicted = None
    return claims, streaming, predicted, tuple(notes)


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

    key = settings.tmdb_api_key
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
        await src.movie_platforms(client, key, tmdb_id, settings.regions)
        if (tmdb_id and key)
        else ()
    )
    # learns + persists a streaming home for any distributor not in the hand table.
    predicted = (
        await learn_predicted_platform(meta.studios, settings) if (meta and not streaming) else None
    )
    return claims, notes, streaming, predicted


async def _tv_claims(
    client: httpx.AsyncClient,
    settings: Settings,
    tmdb_id: str | None,
    obs: list[ReleaseObservation],
) -> tuple[list[Claim], tuple[str, ...], list[str]]:
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
    else:
        notes.append("No air date on TMDB yet.")

    streaming: tuple[str, ...] = ()
    key = settings.tmdb_api_key
    if tmdb_id and key:
        streaming = await TmdbSource().tv_platforms(client, key, tmdb_id, settings.regions)
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
