"""Command-line interface.

Tracker (capture -> enrich -> observe):
  rdt add "<title>" [--kind K] [--now]  # capture a title instantly
  rdt enrich [REF | --all]              # resolve + populate who/where/what
  rdt upcoming [--days N] [--kind K]    # date-sorted overview w/ who/where/what
  rdt card "<title>"                    # one work's full who/where/what + dates
  rdt who "<name>"                      # works a person/studio is credited on

Plumbing:
  rdt seed sync [--source local|notion] # load watchlist into the DB
  rdt pull [--all]                      # run Tier-0 pullers over watched entities
  rdt show [--region US] [--kind game]  # ranked best estimates
  rdt entities                          # list tracked entities
  rdt rd "<title>" [--track]            # one-shot lookup (/rd); --track also captures (/rd-add)
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from release_tracker import matching, views
from release_tracker.artists import (
    ArtistReport,
    add_artist,
    add_membership,
    build_report_live,
    find_artist,
    parse_link_spec,
    refresh_artist,
)
from release_tracker.config import Settings, get_settings
from release_tracker.contingency import ResolutionStatus
from release_tracker.dates_edtf import parse_edtf, to_edtf
from release_tracker.db import Database
from release_tracker.enrich import EnrichSummary, enrich_work
from release_tracker.logging import configure_logging
from release_tracker.lookup import RdReport, lookup
from release_tracker.models import (
    ArtistLink,
    BestEstimate,
    Certainty,
    Condition,
    ConsumptionState,
    CreditRole,
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
    SocialPlatform,
    SourceTier,
    WorkRelation,
)
from release_tracker.pipeline import pull_all, pull_entity
from release_tracker.resolve import best_estimates
from release_tracker.seed import LocalSeed, NotionSeed, SeedProvider
from release_tracker.sources.base import Candidate, make_client
from release_tracker.sources.ddg import WebInfo, instant_answer
from release_tracker.sources.justwatch import JustWatchAvailability
from release_tracker.sources.whentostream import WhenToStreamHints
from release_tracker.titles import season_label

app = typer.Typer(add_completion=False, help="Free-first release date tracker.")
seed_app = typer.Typer(help="Manage the entity watchlist (seed).")
resolve_app = typer.Typer(help="Find canonical ids and pin them to entities (manual matching).")
artist_app = typer.Typer(help="Artist-radar: follow creators and surface their latest content.")
edit_app = typer.Typer(help="Freeform edits: retitle, (un)credit, (un)tag, part-split a work.")
app.add_typer(seed_app, name="seed")
app.add_typer(resolve_app, name="resolve")
app.add_typer(artist_app, name="artist")
app.add_typer(edit_app, name="edit")
console = Console()


def _db() -> Database:
    settings = get_settings()
    return Database(settings.db_path)


def _today() -> date:
    return datetime.now(UTC).date()


@seed_app.command("sync")
def seed_sync(
    source: Annotated[str, typer.Option(help="local | notion")] = "local",
) -> None:
    """Load entities (and any hand-authored dates) from a seed into the DB."""
    configure_logging()
    settings = get_settings()
    provider: SeedProvider = NotionSeed() if source == "notion" else LocalSeed()
    bundle = provider.load(settings)
    db = _db()
    for entity in bundle.entities:
        db.upsert_entity(entity)
        # the user curated these by hand -> they are owned graph nodes from capture.
        db.upsert_node(
            Node(
                id=entity.id,
                node_kind=NodeKind.WORK,
                name=entity.title,
                owned=True,
                external_ids=entity.external_ids,
            )
        )
    obs_count = db.upsert_observations(bundle.observations) if bundle.observations else 0
    db.close()
    console.print(
        f"[green]Seeded[/] {len(bundle.entities)} entities, "
        f"{obs_count} manual observations from [bold]{provider.name}[/]."
    )


@app.command()
def pull(
    all_entities: Annotated[bool, typer.Option("--all", help="ignore watch flag")] = False,
    concurrency: Annotated[int, typer.Option(help="max entities in flight")] = 6,
) -> None:
    """Resolve Tier-0 dates/prices for entities and store observations."""
    configure_logging()
    settings = get_settings()
    db = _db()
    stats = asyncio.run(
        pull_all(db, settings, concurrency=concurrency, watched_only=not all_entities)
    )
    db.close()
    console.print(
        f"[green]Pulled[/] {stats.entities} entities -> "
        f"{stats.observations} observations ([yellow]{stats.errors} errors[/])."
    )


@app.command()
def show(
    region: Annotated[str | None, typer.Option(help="filter to a region code")] = None,
    kind: Annotated[str | None, typer.Option(help="filter to a MediaKind")] = None,
) -> None:
    """Show ranked best estimates per (entity, channel, region)."""
    configure_logging()
    db = _db()
    kind_filter = MediaKind(kind) if kind else None
    titles: dict[str, str] = {}
    rows: list[tuple[str, MediaKind, object]] = []
    for entity in db.iter_entities():
        if kind_filter and entity.kind is not kind_filter:
            continue
        titles[entity.id] = entity.title
        observations = list(db.iter_observations(entity.id))
        for est in best_estimates(observations):
            if region and est.region != region:
                continue
            rows.append((entity.title, entity.kind, est))
    db.close()
    _render(rows)


@app.command()
def entities() -> None:
    """List tracked entities and any discovered external IDs."""
    configure_logging()
    db = _db()
    table = Table(title="Tracked entities", show_lines=False)
    table.add_column("Title", width=40)
    table.add_column("Kind")
    table.add_column("Watch")
    table.add_column("External IDs")
    for entity in db.iter_entities():
        table.add_row(
            entity.title,
            entity.kind.value,
            "✓" if entity.watch else "·",
            ", ".join(f"{k}={v}" for k, v in entity.external_ids.items()) or "—",
        )
    db.close()
    console.print(table)


@app.command()
def rd(
    name: Annotated[str, typer.Argument(help="title to look up, e.g. 'Dune Part Two'")],
    kind: Annotated[
        str | None, typer.Option(help="movie|tv|game|tech — auto-detected if omitted")
    ] = None,
    region: Annotated[
        str | None,
        typer.Option(help="ISO country for tech (hard constraint; defaults to RDT_REGIONS[0])"),
    ] = None,
    season: Annotated[
        int | None,
        typer.Option(help="pin a specific TV season (preferred over 'Show: Season N' titles)"),
    ] = None,
    track: Annotated[
        bool,
        typer.Option("--track", "--add", help="also capture it into the tracker (state: want)"),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="emit machine-readable JSON (for the /rd skill)")
    ] = False,
) -> None:
    """One-shot lookup: confirmed + speculative release dates for a single title.

    With --track, also captures the resolved title into the tracker (the /rd-add path):
    pins its canonical id, pulls dates + enriches who/where/what, marks it 'want'. Pass
    --season N for a TV season — it resolves that season's date and, with --track, files a
    fully-wired 'Show: Season N' entry (series link + coords + subtitle), no edit needed.
    """
    configure_logging()
    settings = get_settings()
    kind_hint = MediaKind(kind) if kind else (MediaKind.TV if season is not None else None)
    report = asyncio.run(lookup(name, settings, kind_hint=kind_hint, region=region, season=season))
    tracked = (
        asyncio.run(_track_from_report(settings, name, report, season=season)) if track else False
    )
    if as_json:
        out = report.to_dict()
        if track:
            out["tracked"] = tracked
        print(json.dumps(out, indent=2))
        return
    _render_report(report)
    if track:
        if tracked:
            console.print(f"[green]Tracked[/] {report.matched_title} [dim](state: want)[/].")
        else:
            console.print(
                "[yellow]Not tracked[/] — couldn't tell what this is "
                "(unknown kind, or a resolvable title that pinned no canonical id)."
            )


async def _track_from_report(
    settings: Settings, name: str, report: RdReport, *, season: int | None = None
) -> bool:
    """Capture a looked-up title into the tracker — always, whenever we know its kind.

    This is a *tracker*, tech included, so ``/rd-add`` never refuses a capture:
    - **Resolvable kinds** (movie/tv/game) that pinned a canonical id capture *with* it, then
      pull dates + enrich who/where/what — *including date-less (TBA) ones* (the skill then
      proposes a speculative window so nothing sits date-less).
    - **Unresolvable kinds** (tech, other) have no Tier-0 source, so they capture as a bare
      entity with no ids and no auto-pull — exactly like the manual ``rdt add`` path that
      seeded e.g. Steam Frames. The skill follows up with a release window.

    The only true skip is a total miss with no kind at all; and a *resolvable* kind we
    couldn't pin is skipped too (a bare unpinned movie would be a bogus, un-enrichable stub
    — better surfaced as "not tracked" so the title can be corrected).

    With ``season``, the entry is canonical-titled ``"Show: Season N"`` and carries the
    structured coord so enrichment auto-wires the series link + subtitle (full-auto path).
    """
    if report.kind is None:
        return False
    if matching.is_resolvable(report.kind) and not report.canonical:
        return False
    # the show's matched name drives a clean "Show: Season N" title for a season capture
    title = season_label(report.matched_title or name, season) if season is not None else name
    db = _db()
    entity = Entity.create(
        title,
        report.kind,
        external_ids=dict(report.canonical),
        consumption_state=ConsumptionState.WANT,
        season=season,
    )
    db.upsert_entity(entity)
    db.upsert_node(
        Node(id=entity.id, node_kind=NodeKind.WORK, name=title, owned=True, external_ids={})
    )
    if report.canonical:  # only a pinned, resolvable entity has ids to pull dates / enrich from
        async with make_client() as client:
            await pull_entity(db, settings, entity, client=client)  # dates via the pinned ids
            await enrich_work(client, db, settings, entity)  # who/where/what
    db.close()
    return True


def _render_report(r: RdReport) -> None:
    if r.kind is MediaKind.TECH and not r.claims:
        # tech carries no engine dates — show the search/price policy scaffold.
        console.print(
            f"[bold]{r.matched_title}[/] [dim](tech · {r.category} · region {r.region})[/]"
        )
        for note in r.notes:
            console.print(f"[dim]• {note}[/]")
        return
    if not r.found and not r.claims:
        console.print(f"[yellow]No confident match[/] for '{r.query}'. {' '.join(r.notes)}")
        _render_web_info(r.web_info)
        raise typer.Exit(2)
    kind_label = r.kind.value if r.kind else "?"
    console.print(f"[bold]{r.matched_title}[/] [dim]({kind_label})[/]")
    table = Table(show_header=True, header_style="bold")
    for col in ("What", "Date", "± days", "Stance", "Conf.", "Basis"):
        table.add_column(col)
    for c in r.claims:
        label = "confirmed" if c.stance == "confirmed" else "speculative"
        stance = f"[{_stance_color(c.stance == 'confirmed')}]{label}[/]"
        table.add_row(
            c.label,
            c.when.isoformat() if c.when else "—",
            str(c.margin_days) if c.margin_days else "—",
            stance,
            f"{c.confidence:.2f}",
            c.basis,
        )
    console.print(table)
    if r.streaming:
        console.print(f"[bold]Streaming:[/] {', '.join(r.streaming)} [dim](confirmed)[/]")
    elif r.predicted_platform:
        console.print(
            f"[bold]Likely streaming home:[/] {r.predicted_platform} [yellow](predicted)[/]"
        )
    if r.price:
        console.print(f"[bold]Price:[/] {r.price}")
    _render_availability(r.availability)
    _render_whentostream(r.whentostream)
    for note in r.notes:
        console.print(f"[dim]• {note}[/]")
    if r.url:
        console.print(f"[dim]{r.url}[/]")
    _render_web_info(r.web_info)


def _render_whentostream(wts: WhenToStreamHints | None) -> None:
    """A compact corroboration line: US PVOD/SVOD dates + the source link (SVOD is also a claim)."""
    if wts is None:
        return
    bits: list[str] = []
    if wts.pvod is not None:
        bits.append(f"PVOD {wts.pvod.isoformat()}")
    if wts.svod_date is not None:
        svc = f" → {wts.svod_service}" if wts.svod_service else ""
        bits.append(f"SVOD {wts.svod_date.isoformat()}{svc}")
    if bits:
        console.print(f"[bold]When To Stream (US):[/] {' · '.join(bits)} [dim]{wts.url}[/]")


def _render_availability(avail: JustWatchAvailability | None) -> None:
    """Render the JustWatch offer scan: earliest-VOD headline + a per-region buy/rent table."""
    if avail is None:
        return
    if avail.earliest_vod is not None:
        console.print(
            f"[bold]Earliest digital:[/] {avail.earliest_vod.isoformat()} "
            f"[cyan]· {avail.earliest_vod_platform} ({avail.earliest_vod_country})[/] "
            f"[dim]← VPN here for the soonest VOD[/]"
        )
    # the actionable own/rent offers, soonest first (subscription homes get their own line).
    vod = sorted(
        (o for o in avail.offers if o.monetization in ("buy", "rent")),
        key=lambda o: (o.available_from or date.max, o.country, o.platform),
    )
    if vod:
        table = Table(show_header=True, header_style="bold", title="Buy / rent")
        for col in ("Region", "Type", "Platform", "Price", "Since"):
            table.add_column(col)
        for o in vod[:14]:
            price = f"{o.price:.2f} {o.currency}" if o.price is not None and o.currency else "—"
            table.add_row(
                o.country,
                o.monetization,
                o.platform + (f" [dim]{o.presentation}[/]" if o.presentation else ""),
                price,
                o.available_from.isoformat() if o.available_from else "—",
            )
        console.print(table)
        if len(vod) > 14:
            console.print(f"[dim]  … +{len(vod) - 14} more buy/rent offers[/]")
    # flatrate (subscription) homes, each tagged with the regions it's live in — VPN targets.
    by_platform: dict[str, list[str]] = {}
    for o in avail.offers:
        if o.monetization == "flatrate":
            by_platform.setdefault(o.platform, []).append(o.country)
    if by_platform:
        console.print("[bold]Streaming (flatrate):[/]")
        for platform, regions in sorted(by_platform.items()):
            console.print(f"  [dim]·[/] {platform} [dim]({', '.join(sorted(set(regions)))})[/]")


def _render_web_info(web: WebInfo | None) -> None:
    """Show the keyless web-context fallback (only present when the sources came up empty)."""
    if web is None:
        return
    console.print(f"[bold]Web context[/] [dim]({web.source})[/]")
    if web.abstract:
        console.print(f"  {web.abstract}")
    for text, url in web.related:
        console.print(f"  [dim]·[/] {_linked(text, url)}")
    if web.url:
        console.print(f"  [dim]{web.url}[/]")


def _render(rows: list[tuple[str, MediaKind, object]]) -> None:
    from release_tracker.models import BestEstimate  # local import for typing only

    table = Table(title="Release best-estimates", show_lines=False)
    # EDTF folds date + precision + uncertainty into one canonical token (e.g. 2026-09~);
    # Stance keeps the precise internal certainty word the qualifier can only approximate.
    for col in ("Title", "Kind", "Channel", "Region", "EDTF", "Stance", "Price", "Conf."):
        table.add_column(col)
    for title, kind, est_obj in rows:
        est = est_obj if isinstance(est_obj, BestEstimate) else None
        if est is None:
            continue
        table.add_row(
            title,
            kind.value,
            est.channel.value,
            est.region,
            to_edtf(
                est.release_date,
                est.precision,
                est.certainty,
                end=est.date_end,
                end_precision=est.precision,
            ),
            f"[{_stance_color(est.certainty in (Certainty.CONFIRMED, Certainty.DELAYED))}]"
            f"{est.certainty.value}[/]",
            str(est.price) if est.price else "—",
            f"{est.confidence:.2f}",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# tracker: capture / enrich / observe
# ---------------------------------------------------------------------------
@app.command()
def add(
    name: Annotated[str, typer.Argument(help="title to track, e.g. 'Dishonored 3'")],
    kind: Annotated[
        str | None, typer.Option(help="movie|tv|game|tech|...; detected on enrich if omitted")
    ] = None,
    season: Annotated[
        int | None,
        typer.Option(help="TV season number — titles it 'Show: Season N' + wires coords on enrich"),
    ] = None,
    part: Annotated[
        int | None, typer.Option(help="mid-season cut (Part/Vol/Cour N) within the season")
    ] = None,
    now: Annotated[bool, typer.Option("--now", help="resolve + enrich immediately")] = False,
    no_themes: Annotated[
        bool, typer.Option("--no-themes", help="skip LLM theme extraction")
    ] = False,
) -> None:
    """Capture a title instantly. Resolve + enrich later (or now with --now).

    With --season N the entry is titled 'Show: Season N' and carries structured season/part
    coords, so enrichment auto-links it to the series with a 'Season N of Show' subtitle.
    """
    configure_logging()
    settings = get_settings()
    # --season implies a TV season unless the caller said otherwise
    media = MediaKind(kind) if kind else (MediaKind.TV if season is not None else MediaKind.OTHER)
    title = season_label(name, season) if season is not None else name
    db = _db()
    entity = Entity.create(title, media, season=season, part=part)
    db.upsert_entity(entity)
    db.upsert_node(
        Node(id=entity.id, node_kind=NodeKind.WORK, name=title, owned=True, external_ids={})
    )
    console.print(f"[green]Added[/] {title} [dim]({media.value})[/]")
    if now:
        summary = asyncio.run(
            _resolve_and_enrich(db, settings, entity, include_themes=not no_themes)
        )
        _print_enrich(entity, summary)
    else:
        console.print(f'[dim]Run `rdt enrich "{title}"` to populate who/where/what.[/]')
    db.close()


@app.command()
def enrich(
    ref: Annotated[str | None, typer.Argument(help="entity id/title; omit and use --all")] = None,
    all_entities: Annotated[
        bool, typer.Option("--all", help="enrich every watched entity")
    ] = False,
    kind: Annotated[
        str | None, typer.Option(help="with --all, restrict to a MediaKind (e.g. tv)")
    ] = None,
    no_themes: Annotated[
        bool, typer.Option("--no-themes", help="skip LLM theme extraction")
    ] = False,
) -> None:
    """Resolve + populate the who/where/what graph for one entity or --all."""
    configure_logging()
    settings = get_settings()
    kind_filter = MediaKind(kind) if kind else None
    db = _db()
    if all_entities:
        targets = [
            e
            for e in db.iter_entities(watched_only=True)
            if kind_filter is None or e.kind is kind_filter
        ]
    elif ref:
        entity = _resolve_ref(db, ref)
        if entity is None:
            db.close()
            raise typer.Exit(1)
        targets = [entity]
    else:
        db.close()
        raise typer.BadParameter("pass an entity ref or --all")
    for entity in targets:
        try:
            summary = asyncio.run(
                _resolve_and_enrich(db, settings, entity, include_themes=not no_themes)
            )
            _print_enrich(entity, summary)
        except Exception as exc:  # one bad entity must not abort a 120-item batch
            console.print(f"[red]Failed[/] {entity.title}: {exc}")
    db.close()


@app.command()
def upcoming(
    days: Annotated[int | None, typer.Option(help="only releases within N days")] = None,
    kind: Annotated[str | None, typer.Option(help="filter to a MediaKind")] = None,
) -> None:
    """Compact, date-sorted overview of upcoming releases with who/where/what."""
    configure_logging()
    settings = get_settings()
    db = _db()
    kind_filter = MediaKind(kind) if kind else None
    rows = views.upcoming(db, _today(), settings, days=days, kind=kind_filter)
    db.close()
    _render_upcoming(rows, days)


@app.command()
def available(
    kind: Annotated[str | None, typer.Option(help="filter to a MediaKind")] = None,
) -> None:
    """Things out now that you haven't finished (want/watching), newest first."""
    configure_logging()
    settings = get_settings()
    db = _db()
    kind_filter = MediaKind(kind) if kind else None
    rows = views.available(db, _today(), settings, kind=kind_filter)
    db.close()
    _render_available(rows)


@app.command()
def watched(
    kind: Annotated[str | None, typer.Option(help="filter to a MediaKind")] = None,
) -> None:
    """Things you're done with (watched/dropped), most recently released first."""
    configure_logging()
    settings = get_settings()
    db = _db()
    kind_filter = MediaKind(kind) if kind else None
    rows = views.watched(db, _today(), settings, kind=kind_filter)
    db.close()
    _render_watched(rows)


@app.command()
def card(ref: Annotated[str, typer.Argument(help="entity id or title substring")]) -> None:
    """Full who/where/what + dates for one tracked work."""
    configure_logging()
    db = _db()
    entity = _resolve_ref(db, ref)
    if entity is None:
        db.close()
        raise typer.Exit(1)
    work = views.work_card(db, entity)
    media_notes = db.iter_notes(entity.id)
    db.close()
    _render_card(work)
    if media_notes:
        console.print("[bold]Notes[/]")
        for when, body in media_notes:
            console.print(f"  [dim]{when.isoformat()}[/]  {body}")


@app.command()
def who(name: Annotated[str, typer.Argument(help="person or studio name")]) -> None:
    """List the works a person/studio is credited on (one-hop walk over the graph)."""
    configure_logging()
    db = _db()
    nodes = [n for n in db.find_nodes(name) if n.node_kind in (NodeKind.PERSON, NodeKind.ORG)]
    if not nodes:
        db.close()
        console.print(f"[yellow]No person/studio[/] matching '{name}'. Enrich some works first.")
        raise typer.Exit(1)
    node = nodes[0]
    works = views.works_by_node(db, node)
    db.close()
    table = Table(title=f"{node.name} — credited on {len(works)} work(s)", show_lines=False)
    for col in ("Role", "Title", "Kind", "Owned"):
        table.add_column(col)
    for w in works:
        table.add_row(
            w.role.value,
            w.entity.title,
            w.entity.kind.value,
            "[green]✓ yours[/]" if w.owned else "[dim]world[/]",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# artist radar
# ---------------------------------------------------------------------------
@app.command()
def artists(
    sort: Annotated[str, typer.Option(help="recency | name")] = "recency",
) -> None:
    """The creator radar: followed artists, whoever posted most recently first."""
    configure_logging()
    settings = get_settings()
    db = _db()
    rows = views.artists(db, _today(), settings, sort=sort)
    db.close()
    _render_artists(rows)


@artist_app.command("add")
def artist_add(
    name: Annotated[str, typer.Argument(help="creator name")],
    link: Annotated[list[str] | None, typer.Option(help="platform:tier:url (repeatable)")] = None,
    kind: Annotated[str, typer.Option(help="person | org")] = "person",
    no_fetch: Annotated[
        bool, typer.Option("--no-fetch", help="skip fetching latest content")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="emit ArtistReport JSON (the skill)")
    ] = False,
) -> None:
    """Follow a creator + attach stratified links (the /rd-artist entrypoint)."""
    configure_logging()
    settings = get_settings()
    node_kind = NodeKind.ORG if kind.lower() == "org" else NodeKind.PERSON
    try:
        specs = [parse_link_spec(t) for t in (link or [])]
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    db = _db()

    async def go() -> ArtistReport:
        async with make_client() as client:
            node = await add_artist(
                client,
                db,
                name,
                kind=node_kind,
                links=specs,
                fetch=not no_fetch,
                settings=settings,
                today=_today(),
            )
            return await build_report_live(client, db, node, name, settings, _today())

    report = asyncio.run(go())
    db.close()
    if as_json:
        print(json.dumps(report.to_dict(), indent=2))
        return
    _render_artist_report(report)


@artist_app.command("follow")
def artist_follow(
    ref: Annotated[str, typer.Argument(help="existing person/studio (id or name)")],
) -> None:
    """Add an existing media-graph person/studio onto the radar."""
    configure_logging()
    db = _db()
    node = find_artist(db, ref)
    if node is None:
        db.close()
        console.print(f"[yellow]No person/studio[/] matching '{ref}'. Enrich a work first.")
        raise typer.Exit(1)
    db.set_followed(node.id, followed=True)
    db.close()
    console.print(
        f'[green]Following[/] {node.name}. Add links with `rdt artist add "{node.name}"`.'
    )


@artist_app.command("refresh")
def artist_refresh(
    ref: Annotated[str | None, typer.Argument(help="creator id/name; omit with --all")] = None,
    all_artists: Annotated[
        bool, typer.Option("--all", help="refresh every followed artist")
    ] = False,
) -> None:
    """Re-fetch the latest content for one artist or --all."""
    configure_logging()
    settings = get_settings()
    db = _db()
    if all_artists:
        targets = db.followed_artists()
    elif ref:
        node = find_artist(db, ref)
        targets = [node] if node else []
    else:
        db.close()
        raise typer.BadParameter("pass an artist ref or --all")

    async def go() -> None:
        async with make_client() as client:
            for node in targets:
                n = await refresh_artist(client, db, node, settings=settings, today=_today())
                console.print(f"[green]Refreshed[/] {node.name}: {n} link(s) updated.")

    asyncio.run(go())
    db.close()


@artist_app.command("show")
def artist_show(
    name: Annotated[str, typer.Argument(help="creator id or name")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show a followed creator's links + latest content + the works of theirs you track."""
    configure_logging()
    settings = get_settings()
    db = _db()
    node = find_artist(db, name)

    async def go() -> ArtistReport:
        async with make_client() as client:
            return await build_report_live(client, db, node, name, settings, _today())

    report = asyncio.run(go())
    db.close()
    if as_json:
        print(json.dumps(report.to_dict(), indent=2))
        return
    if not report.found:
        console.print(f"[yellow]Not on the radar:[/] '{name}'. `rdt artist add \"{name}\"` first.")
        raise typer.Exit(1)
    _render_artist_report(report)


@artist_app.command("member")
def artist_member(
    person: Annotated[str, typer.Argument(help="the individual")],
    group: Annotated[str, typer.Argument(help="the group / studio / band")],
) -> None:
    """Record that a person is a member of a group (person -> group)."""
    configure_logging()
    db = _db()
    person_node, group_node = add_membership(db, person, group)
    db.close()
    console.print(f"[green]Linked[/] {person_node.name} → member of [bold]{group_node.name}[/].")


@artist_app.command("members")
def artist_members(ref: Annotated[str, typer.Argument(help="a group or a person")]) -> None:
    """List a group's members — or, if given a person, the groups they belong to."""
    configure_logging()
    db = _db()
    node = find_artist(db, ref)
    if node is None:
        db.close()
        console.print(f"[yellow]No person/group[/] matching '{ref}'.")
        raise typer.Exit(1)
    members = views.members_of(db, node)
    groups = views.groups_of(db, node)
    db.close()
    if members:
        table = Table(title=f"{node.name} — {len(members)} member(s)", show_lines=False)
        table.add_column("Member")
        table.add_column("Followed")
        for m in members:
            table.add_row(m.name, "[green]✓[/]" if m.followed else "[dim]·[/]")
        console.print(table)
    if groups:
        console.print(f"[bold]{node.name}[/] is a member of: {', '.join(g.name for g in groups)}")
    if not members and not groups:
        console.print(
            f'[dim]No memberships for {node.name}. `rdt artist member "<person>" "{node.name}"`.[/]'
        )


# --- artist renderers -----------------------------------------------------
def _linked(text: str, url: str | None) -> str:
    """Wrap cell text in an OSC-8 terminal hyperlink (degrades to plain text if unsupported)."""
    return f"[link={url}]{text}[/link]" if url else text


def _ago(when: date | None) -> str:
    if when is None:
        return ""
    days = (_today() - when).days
    if days <= 0:
        return "today"
    if days < 60:
        return f"{days}d ago"
    return f"{days // 30}mo ago"


def _platforms(platforms: tuple[SocialPlatform, ...]) -> str:
    return ", ".join(p.value for p in platforms) or "[dim]—[/]"


def _render_artists(rows: list[views.ArtistRow]) -> None:
    table = Table(title="Artist radar", show_lines=False)
    table.add_column("⟳", width=1, no_wrap=True)
    table.add_column("Artist", min_width=16, max_width=28, no_wrap=True, overflow="ellipsis")
    table.add_column("Kind", min_width=6, no_wrap=True)
    table.add_column("Last post", min_width=16, no_wrap=True)
    table.add_column("Free", max_width=22, no_wrap=True, overflow="ellipsis")
    table.add_column("Paid", max_width=16, no_wrap=True, overflow="ellipsis")
    table.add_column("Works", justify="right", no_wrap=True)
    for r in rows:
        if r.last_post is not None:
            platform, when = r.last_post
            last = _linked(f"{platform.value} [dim]{_ago(when)}[/]", r.latest_url)
        else:
            last = "[dim]—[/]"
        table.add_row(
            _fresh_dot(r.freshness),
            _linked(r.name, r.profile_url),  # click the name -> straight to their profile
            r.kind.value,
            last,  # click the last post -> the drop itself, no recommender feed
            _platforms(r.free),
            _platforms(r.paid),
            str(r.n_works) if r.n_works else "[dim]—[/]",
        )
    console.print(table)
    if rows:
        console.print(
            "[dim]⟳ how recently we checked · click a name/post to open it · "
            "`rdt artist refresh --all` to update[/]"
        )
    else:
        console.print("[dim]No followed artists. `/rd-artist <name>` or `rdt artist add`.[/]")


def _render_artist_report(report: ArtistReport) -> None:
    console.print(f"[bold]{report.name}[/] [dim]({report.kind})[/]")
    by_tier: dict[str, list[ArtistLink]] = {"free": [], "paid": [], "auxiliary": []}
    for link in report.links:
        by_tier[link.tier.value].append(link)
    labels = {"free": "Free pipeline", "paid": "Paid pipeline", "auxiliary": "Auxiliary socials"}
    for tier, label in labels.items():
        if not by_tier[tier]:
            continue
        console.print(f"[bold]{label}[/]")
        for link in by_tier[tier]:
            latest = ""
            if link.latest_url:
                drop = _linked(link.latest_title or "post", link.latest_url)
                latest = f" [dim]· latest:[/] {drop} [dim]({_ago(link.last_post_at)})[/]"
            handle = _linked(link.platform.value, link.url)  # click the platform -> the profile
            console.print(f"  [cyan]{handle}[/] [dim]{link.url}[/]{latest}")
    if report.slate:
        console.print("[bold]On the slate[/] [dim](upcoming)[/]")
        for s in report.slate:
            when = s.when.isoformat() if s.when else "TBA"
            mark = "[green]✓ tracked[/]" if s.tracked else "[dim]+ /rd-add[/]"
            title = _linked(s.title, s.url)  # click the title -> the work's page
            console.print(f"  [dim]{when}[/] [cyan]{s.role}[/] {title} [dim]({s.kind})[/] {mark}")
    if report.members:
        console.print(f"[bold]Members[/] {', '.join(n.name for n in report.members)}")
    if report.groups:
        console.print(f"[bold]Member of[/] {', '.join(n.name for n in report.groups)}")
    if report.tracked_works:
        console.print("[bold]Works you track[/]")
        for w in report.tracked_works:
            console.print(f"  [dim]{w.role.value:<10}[/] {w.entity.title} ({w.entity.kind.value})")
    for note in report.notes:
        console.print(f"[dim]• {note}[/]")


@app.command()
def seasons(show: Annotated[str, typer.Argument(help="show / franchise name")]) -> None:
    """List the tracked seasons or parts of a series, ordered (a one-hop walk)."""
    configure_logging()
    db = _db()
    nodes = db.find_nodes(show, node_kind=NodeKind.SERIES)
    if not nodes:
        db.close()
        console.print(f"[yellow]No series[/] matching '{show}'. Enrich some TV/movie works first.")
        raise typer.Exit(1)
    node = nodes[0]
    entries = views.seasons_of_series(db, node)
    db.close()
    table = Table(title=f"{node.name} — {len(entries)} tracked", show_lines=False)
    for col in ("Season", "Title", "Date", "Owned"):
        table.add_column(col)
    for s in entries:
        if s.season is None:
            label = "[dim]—[/]"
        else:
            label = f"S{s.season}" + (f" Pt{s.part}" if s.part is not None else "")
        table.add_row(
            label,
            s.entity.title,
            s.when.isoformat() if s.when else "—",
            "[green]✓ yours[/]" if s.owned else "[dim]world[/]",
        )
    console.print(table)


_WORK_RELATIONS = ", ".join(r.value for r in WorkRelation)


def _resolve_to_node(db: Database, ref: str) -> Node | None:
    """Resolve a work reference to its graph node (prefers a tracked entity)."""
    entities = db.find_entities(ref)
    entity = next((e for e in entities if e.title.lower() == ref.lower()), None) or next(
        iter(entities), None
    )
    if entity is not None:
        node = db.get_node(entity.id)
        if node is None:  # tracked but never noded -> create the work node
            node = Node(id=entity.id, node_kind=NodeKind.WORK, name=entity.title, owned=True)
            db.upsert_node(node)
        return node
    nodes = db.find_nodes(ref)
    return nodes[0] if nodes else None


@app.command()
def relate(
    work: Annotated[str, typer.Argument(help="the derivative work (id or title)")],
    relation: Annotated[str, typer.Argument(help=f"one of: {_WORK_RELATIONS}")],
    other: Annotated[str, typer.Argument(help="the work/franchise it derives from")],
) -> None:
    """Record cross-media lineage, e.g. `relate "Arcane: Noxus" spinoff "Arcane"`."""
    configure_logging()
    try:
        rel = WorkRelation(relation.lower())
    except ValueError:
        raise typer.BadParameter(f"relation must be one of: {_WORK_RELATIONS}") from None
    db = _db()
    src = _resolve_to_node(db, work)
    dst = _resolve_to_node(db, other)
    if src is None or dst is None:
        missing = work if src is None else other
        db.close()
        console.print(f"[red]No work/node matches[/] '{missing}'.")
        raise typer.Exit(1)
    # guard (a): a work can't derive from itself. This usually means `other` only matched
    # `work` as a substring because the real source isn't tracked (e.g. "Baby Driver"
    # collapsing onto "Baby Driver 2"). Refuse rather than silently write a self-loop edge.
    if src.id == dst.id:
        db.close()
        console.print(
            f"[red]Self-link refused[/]: '{other}' resolved to the same work as '{work}' "
            f"([dim]{src.name}[/]). The source probably isn't tracked yet — add it first, "
            f"or pass a more specific id."
        )
        raise typer.Exit(1)
    # guard (b): warn when `other` was an ambiguous substring hit (>1 match, none exact), so a
    # wrong lineage target isn't linked silently — the user can re-run with an explicit id.
    other_matches = db.find_entities(other)
    if len(other_matches) > 1 and not any(e.title.lower() == other.lower() for e in other_matches):
        console.print(
            f"[yellow]Note[/]: '{other}' matched {len(other_matches)} works; linked to "
            f"[bold]{dst.name}[/]. Re-run with an id if that's not the intended source."
        )
    db.upsert_edge(
        Edge(
            src_id=src.id,
            dst_id=dst.id,
            relation=RelationKind.DERIVED_FROM,
            role=rel,
            source_provider="user",
            source_tier=SourceTier.OFFICIAL,
            confidence=1.0,
            owned=True,
        )
    )
    db.close()
    console.print(f"[green]Linked[/] {src.name} [dim]({rel.value} of)[/] [bold]{dst.name}[/].")


@app.command()
def related(ref: Annotated[str, typer.Argument(help="a work (id or title)")]) -> None:
    """Show a work's cross-media lineage — what it derives from and what derives from it."""
    configure_logging()
    db = _db()
    node = _resolve_to_node(db, ref)
    if node is None:
        db.close()
        console.print(f"[yellow]No work[/] matching '{ref}'.")
        raise typer.Exit(1)
    upstream = views.derived_from(db, node.id)
    downstream = views.derivatives_of(db, node.id)
    db.close()
    console.print(f"[bold]{node.name}[/]")
    for r in upstream:
        console.print(f"  [dim]{r.relation.value} of[/] → {r.node.name}")
    for r in downstream:
        console.print(f"  [dim]← {r.relation.value}[/] {r.node.name}")
    if not upstream and not downstream:
        console.print(f'  [dim]No relations. `rdt relate "<work>" spinoff "{node.name}"`.[/]')


@app.command()
def state(
    ref: Annotated[str, typer.Argument(help="entity id or title substring")],
    value: Annotated[
        str, typer.Argument(help="unset | want | watching | watched | dropped | skipped")
    ],
) -> None:
    """Set a title's watch/play state (want/watching/watched/dropped/skipped).

    ``skipped`` = a conscious pass (not for me): kept for the preference record but out of
    the upcoming/available queues. To erase a title entirely instead, use ``rdt forget``.
    """
    configure_logging()
    db = _db()
    entity = _resolve_ref(db, ref)
    if entity is None:
        db.close()
        raise typer.Exit(1)
    try:
        new_state = ConsumptionState(value.lower())
    except ValueError:
        db.close()
        raise typer.BadParameter(
            f"state must be one of: {', '.join(s.value for s in ConsumptionState)}"
        ) from None
    db.set_consumption_state(entity.id, new_state)
    db.close()
    console.print(f"[green]Set[/] {entity.title} → [bold]{new_state.value}[/]")


@app.command()
def forget(
    ref: Annotated[str, typer.Argument(help="entity id or title substring")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="skip the confirmation prompt")] = False,
) -> None:
    """Permanently delete a tracked work — its dates, credits, and graph edges all cascade.

    Destructive and irreversible. To keep a title you've passed on (preserving the
    preference), use ``rdt state <title> skipped`` instead.
    """
    configure_logging()
    db = _db()
    entity = _resolve_ref(db, ref)
    if entity is None:
        db.close()
        raise typer.Exit(1)
    if not yes and not typer.confirm(
        f"Permanently forget '{entity.title}' ({entity.kind.value})? This cannot be undone."
    ):
        db.close()
        console.print("[dim]Aborted — nothing deleted.[/]")
        raise typer.Exit(1)
    db.delete_entity(entity.id)
    db.close()
    console.print(f"[green]Forgot[/] {entity.title} [dim]({entity.id})[/]")


@app.command()
def note(
    ref: Annotated[str, typer.Argument(help="entity id or title substring")],
    text: Annotated[
        str, typer.Argument(help="freeform note, e.g. 'production halted, resume 2027'")
    ],
) -> None:
    """Append a timestamped freeform note to a tracked work."""
    configure_logging()
    db = _db()
    entity = _resolve_ref(db, ref)
    if entity is None:
        db.close()
        raise typer.Exit(1)
    db.add_note(entity.id, text)
    db.close()
    console.print(f"[green]Noted[/] on {entity.title}.")


@app.command()
def notes(ref: Annotated[str, typer.Argument(help="entity id or title substring")]) -> None:
    """List the freeform notes for a work, newest first."""
    configure_logging()
    db = _db()
    entity = _resolve_ref(db, ref)
    if entity is None:
        db.close()
        raise typer.Exit(1)
    items = db.iter_notes(entity.id)
    db.close()
    if not items:
        console.print(f"[dim]No notes for {entity.title}.[/]")
        return
    console.print(f"[bold]{entity.title}[/] — notes")
    for when, body in items:
        console.print(f"  [dim]{when.isoformat()}[/]  {body}")


# --- freeform edit surface (the deterministic backing for /rd-edit + a future
#     web edit form). Each command is one small, idempotent, well-typed mutation
#     so both an LLM and a human can drive the same primitives. -------------------
_CREDIT_ROLES = ", ".join(r.value for r in CreditRole)


def _parse_pin(pin: str) -> tuple[str, str]:
    """'tmdb:287' -> ('tmdb', '287'); the canonical (source, source_id) key.

    Pinning a credit to a source key makes its node id ``person:tmdb:287``, so a
    hand-authored credit collapses onto the same node enrich resolves from that
    source (instead of a separate name-slug node).
    """
    source, sep, source_id = pin.partition(":")
    if not sep or not source.strip() or not source_id.strip():
        raise typer.BadParameter("--pin must be <source>:<id>, e.g. tmdb:287")
    return source.strip(), source_id.strip()


def _edit_entity(db: Database, ref: str) -> Entity | None:
    """Resolve a work for editing, printing + closing the db on failure."""
    entity = _resolve_ref(db, ref)
    if entity is None:
        db.close()
    return entity


@edit_app.command("rename")
def edit_rename(
    ref: Annotated[str, typer.Argument(help="entity id or current title substring")],
    title: Annotated[str, typer.Argument(help="the new display title")],
) -> None:
    """Retitle a work (e.g. a placeholder -> the announced name).

    The stable id never changes — only the display title — so every observation,
    edge and note stays attached. The old title is kept as a search alias.
    """
    configure_logging()
    db = _db()
    entity = _edit_entity(db, ref)
    if entity is None:
        raise typer.Exit(1)
    old = entity.title
    aliases = entity.aliases if old in entity.aliases else (*entity.aliases, old)
    db.upsert_entity(entity.model_copy(update={"title": title, "aliases": aliases}))
    if (node := db.get_node(entity.id)) is not None:  # keep the WORK node label in sync
        db.upsert_node(node.model_copy(update={"name": title}))
    db.close()
    console.print(f"[green]Renamed[/] [dim]{old}[/] → [bold]{title}[/] [dim]({entity.id})[/]")


@edit_app.command("kind")
def edit_kind(
    ref: Annotated[str, typer.Argument(help="entity id or title substring")],
    kind: Annotated[str, typer.Argument(help="movie | tv | game | book | comic | …")],
) -> None:
    """Reclassify a work's format (e.g. an anime film mis-captured as tv -> movie).

    The kind drives source routing (movie vs tv graph); the stable id is unchanged,
    so observations/edges/notes stay attached. "anime" is NOT a kind — it's an origin
    tag, so capture an anime film as movie / a series as tv (`rdt edit tag … --origin`).
    """
    configure_logging()
    try:
        new_kind = MediaKind(kind.lower())
    except ValueError:
        valid = ", ".join(k.value for k in MediaKind)
        raise typer.BadParameter(f"kind must be one of: {valid}") from None
    db = _db()
    entity = _edit_entity(db, ref)
    if entity is None:
        raise typer.Exit(1)
    if entity.kind is new_kind:
        db.close()
        console.print(f"[dim]{entity.title} is already {new_kind.value}.[/]")
        return
    old = entity.kind
    db.upsert_entity(entity.model_copy(update={"kind": new_kind}))
    db.close()
    console.print(
        f"[green]Reclassified[/] {entity.title}: [dim]{old.value}[/] → [bold]{new_kind.value}[/] "
        f'[dim](re-run `rdt enrich "{entity.title}"` to refresh the graph)[/]'
    )


@edit_app.command("alias")
def edit_alias(
    ref: Annotated[str, typer.Argument(help="entity id or title substring")],
    alias: Annotated[str, typer.Argument(help="an alternate title to make searchable")],
) -> None:
    """Add a search alias (an alternate title) without changing the display title."""
    configure_logging()
    db = _db()
    entity = _edit_entity(db, ref)
    if entity is None:
        raise typer.Exit(1)
    if alias in entity.aliases or alias == entity.title:
        db.close()
        console.print(f"[dim]{entity.title} already known as '{alias}'.[/]")
        return
    db.upsert_entity(entity.model_copy(update={"aliases": (*entity.aliases, alias)}))
    db.close()
    console.print(f"[green]Aliased[/] {entity.title} += [bold]{alias}[/]")


@edit_app.command("credit")
def edit_credit(
    ref: Annotated[str, typer.Argument(help="the work (id or title)")],
    name: Annotated[str, typer.Argument(help="the person/studio to credit")],
    role: Annotated[str, typer.Argument(help=f"one of: {_CREDIT_ROLES}")],
    org: Annotated[bool, typer.Option("--org", help="credit a studio/org, not a person")] = False,
    pin: Annotated[
        str | None,
        typer.Option(
            "--pin",
            help="canonicalize onto a source key, <source>:<id> (e.g. tmdb:287), so the "
            "credit collapses onto the same node enrich resolves",
        ),
    ] = None,
) -> None:
    """Attach a who-edge: credit a person or studio on a work (user-authored).

    By default a credit-by-name makes a name-slug node (``person:denis-villeneuve``).
    Pass --pin <source>:<id> to instead use the canonical id (``person:tmdb:287``),
    so it's the *same* node a resolved credit from that source maps to.
    """
    configure_logging()
    try:
        credit_role = CreditRole(role.lower())
    except ValueError:
        raise typer.BadParameter(f"role must be one of: {_CREDIT_ROLES}") from None
    kind = NodeKind.ORG if org else NodeKind.PERSON
    if pin is not None:
        source, source_id = _parse_pin(pin)
        node = Node.create(kind, name, source=source, source_id=source_id, owned=True)
    else:
        node = Node.create(kind, name, owned=True)
    db = _db()
    entity = _edit_entity(db, ref)
    if entity is None:
        raise typer.Exit(1)
    db.upsert_node(node)
    db.upsert_edge(
        Edge(
            src_id=node.id,
            dst_id=entity.id,
            relation=RelationKind.CREDITED_ON,
            role=credit_role,
            source_provider="user",
            source_tier=SourceTier.OFFICIAL,
            confidence=1.0,
            owned=True,
        )
    )
    db.close()
    pinned = f" [dim]→ {node.id}[/]" if pin is not None else ""
    console.print(
        f"[green]Credited[/] {name} [dim]({credit_role.value})[/] "
        f"on [bold]{entity.title}[/]{pinned}"
    )


@edit_app.command("uncredit")
def edit_uncredit(
    ref: Annotated[str, typer.Argument(help="the work (id or title)")],
    name: Annotated[str, typer.Argument(help="the credited person/studio to remove")],
) -> None:
    """Remove a who-edge: drop a person/studio's credit(s) from a work."""
    configure_logging()
    db = _db()
    entity = _edit_entity(db, ref)
    if entity is None:
        raise typer.Exit(1)
    nodes = db.find_nodes(name)
    targets = {n.id for n in nodes}
    removed = [
        e
        for e in db.edges_to(entity.id, RelationKind.CREDITED_ON)
        if e.src_id in targets and db.delete_edge(e.id)
    ]
    db.close()
    if not removed:
        console.print(f"[yellow]No credit[/] for '{name}' on {entity.title}.")
        raise typer.Exit(1)
    console.print(
        f"[green]Removed[/] {len(removed)} credit(s) for {name} from [bold]{entity.title}[/]"
    )


@edit_app.command("tag")
def edit_tag(
    ref: Annotated[str, typer.Argument(help="the work (id or title)")],
    descriptor: Annotated[str, typer.Argument(help="a genre, theme, or origin to attach")],
    theme: Annotated[
        bool, typer.Option("--theme", help="tag as a soft theme instead of a genre")
    ] = False,
    origin: Annotated[
        bool, typer.Option("--origin", help="tag as a medium/origin, e.g. anime")
    ] = False,
) -> None:
    """Attach a what-edge: tag a work with a genre (default), theme, or origin."""
    configure_logging()
    db = _db()
    entity = _edit_entity(db, ref)
    if entity is None:
        raise typer.Exit(1)
    kind = (
        DescriptorKind.ORIGIN if origin else DescriptorKind.THEME if theme else DescriptorKind.GENRE
    )
    node = Node.create(NodeKind.DESCRIPTOR, descriptor, descriptor_kind=kind, owned=True)
    db.upsert_node(node)
    db.upsert_edge(
        Edge(
            src_id=entity.id,
            dst_id=node.id,
            relation=RelationKind.EXHIBITS,
            source_provider="user",
            source_tier=SourceTier.OFFICIAL,
            confidence=1.0,
            owned=True,
        )
    )
    db.close()
    console.print(f"[green]Tagged[/] {entity.title} [dim]({kind.value})[/] [bold]{descriptor}[/]")


@edit_app.command("platform")
def edit_platform(
    ref: Annotated[str, typer.Argument(help="the work (id or title)")],
    name: Annotated[str, typer.Argument(help="a streaming/where platform, e.g. Vimeo")],
    predicted: Annotated[
        bool, typer.Option("--predicted", help="a likely/expected home, not a confirmed one")
    ] = False,
) -> None:
    """Attach a where-edge: record a platform a work is (or will likely be) available on."""
    configure_logging()
    db = _db()
    entity = _edit_entity(db, ref)
    if entity is None:
        raise typer.Exit(1)
    node = Node.create(NodeKind.PLATFORM, name, owned=not predicted)
    db.upsert_node(node)
    db.upsert_edge(
        Edge(
            src_id=entity.id,
            dst_id=node.id,
            relation=RelationKind.AVAILABLE_ON,
            # predicted -> model-tier (renders as "~name"); hand-stated -> user-authoritative
            source_provider="model" if predicted else "user",
            source_tier=SourceTier.MODEL if predicted else SourceTier.OFFICIAL,
            confidence=0.4 if predicted else 1.0,
            owned=not predicted,
        )
    )
    db.close()
    tag = " [dim](likely)[/]" if predicted else ""
    console.print(f"[green]Where[/] {entity.title} → [bold]{name}[/]{tag}")


@edit_app.command("untag")
def edit_untag(
    ref: Annotated[str, typer.Argument(help="the work (id or title)")],
    descriptor: Annotated[str, typer.Argument(help="the genre/theme to remove")],
) -> None:
    """Remove a what-edge: drop a genre/theme from a work."""
    configure_logging()
    db = _db()
    entity = _edit_entity(db, ref)
    if entity is None:
        raise typer.Exit(1)
    targets = {n.id for n in db.find_nodes(descriptor, node_kind=NodeKind.DESCRIPTOR)}
    removed = [
        e
        for e in db.edges_from(entity.id, RelationKind.EXHIBITS)
        if e.dst_id in targets and db.delete_edge(e.id)
    ]
    db.close()
    if not removed:
        console.print(f"[yellow]No tag[/] '{descriptor}' on {entity.title}.")
        raise typer.Exit(1)
    console.print(f"[green]Untagged[/] {descriptor} from [bold]{entity.title}[/]")


@edit_app.command("part")
def edit_part(
    ref: Annotated[str, typer.Argument(help="the work (id or title)")],
    season: Annotated[int, typer.Argument(help="the season number")],
    part: Annotated[
        int | None, typer.Argument(help="the mid-season cut (Part/Vol/Cour N), if split")
    ] = None,
    series: Annotated[
        str | None,
        typer.Option("--series", help="series name to link to if no series edge exists yet"),
    ] = None,
) -> None:
    """Set a work's (season, part) coordinates within its series.

    Updates the existing part_of_series edge in place; pass --series to create the
    link when a now-known split first attaches the work to a franchise.
    """
    configure_logging()
    db = _db()
    entity = _edit_entity(db, ref)
    if entity is None:
        raise typer.Exit(1)
    existing = db.edges_from(entity.id, RelationKind.PART_OF_SERIES)
    if existing:
        base = existing[0]
        dst_id, provider, tier = base.dst_id, base.source_provider, base.source_tier
    elif series is not None:
        node = Node.create(NodeKind.SERIES, series, owned=True)
        db.upsert_node(node)
        dst_id, provider, tier = node.id, "user", SourceTier.OFFICIAL
    else:
        db.close()
        console.print(f'[yellow]{entity.title}[/] has no series link — pass --series "<name>".')
        raise typer.Exit(1)
    db.upsert_edge(
        Edge(
            src_id=entity.id,
            dst_id=dst_id,
            relation=RelationKind.PART_OF_SERIES,
            ordinal=season,
            part=part,
            source_provider=provider,
            source_tier=tier,
            confidence=1.0,
            owned=True,
        )
    )
    # keep the structured coord on the entity in sync with the edge, so the puller's
    # season resolution stays authoritative (not dependent on title parsing).
    db.upsert_entity(entity.model_copy(update={"season": season, "part": part}))
    db.close()
    coord = f"S{season}" + (f" Pt{part}" if part is not None else "")
    console.print(f"[green]Set[/] {entity.title} → [bold]{coord}[/]")


@edit_app.command("date")
def edit_date(
    ref: Annotated[str, typer.Argument(help="the work (id or title)")],
    edtf: Annotated[
        str,
        typer.Argument(
            help="EDTF date: 2026 (year), 2026-09 (month), 2026-34 (Q2), 2026-09-18 (day); "
            "trailing ? = uncertain, ~ = approximate (e.g. 2026-09~)"
        ),
    ],
    channel: Annotated[
        str,
        typer.Option("--channel", help="primary|theatrical|digital|streaming|… (default primary)"),
    ] = "primary",
) -> None:
    """Hand-author a release date in EDTF — partial and uncertain dates welcome.

    EDTF folds precision and uncertainty into one token, so a "we only know the month,
    and it's a guess" date is just ``2026-09~``. Stored as a manual observation on the
    given channel (re-running replaces the prior manual date for that channel).
    """
    configure_logging()
    db = _db()
    entity = _edit_entity(db, ref)
    if entity is None:
        raise typer.Exit(1)
    try:
        parsed = parse_edtf(edtf)
    except ValueError as exc:
        db.close()
        raise typer.BadParameter(str(exc)) from None
    try:
        chan = ReleaseChannel(channel.lower())
    except ValueError:
        db.close()
        raise typer.BadParameter(f"unknown channel {channel!r}") from None
    confirmed = parsed.certainty is Certainty.CONFIRMED
    canonical = to_edtf(
        parsed.when,
        parsed.precision,
        parsed.certainty,
        end=parsed.end,
        end_precision=parsed.end_precision,
    )
    db.delete_channel_observations(entity.id, "manual", chan)
    db.upsert_observation(
        ReleaseObservation(
            entity_id=entity.id,
            channel=chan,
            region="WW",
            release_date=parsed.when,
            date_end=parsed.end,  # set for an EDTF interval (a release window), else None
            precision=parsed.precision,
            certainty=parsed.certainty,
            source_tier=SourceTier.OFFICIAL if confirmed else SourceTier.RUMOR,
            provider="manual",
            source_name="Manual (EDTF)",
            source_quote=canonical,
            confidence=1.0 if confirmed else 0.5,
            fetched_at=datetime.now(UTC),
        )
    )
    db.close()
    console.print(
        f"[green]Dated[/] {entity.title} [{chan.value}] → [bold]{canonical}[/] "
        f"({parsed.certainty.value})"
    )


@edit_app.command("cleardate")
def edit_cleardate(
    ref: Annotated[str, typer.Argument(help="the work (id or title)")],
    channel: Annotated[
        str,
        typer.Argument(help="channel to clear: primary|theatrical|digital|streaming|premiere|…"),
    ],
    predicted: Annotated[
        bool,
        typer.Option(
            "--predicted",
            help="clear only model predictions (provider=model) — e.g. a stale digital estimate",
        ),
    ] = False,
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="clear only this provider's rows (manual|tmdb|model|…)"),
    ] = None,
) -> None:
    """Remove a stale/wrong date from a channel so it stops driving the best-estimate.

    The common case is a stale *prediction* (a digital window estimated off the wrong anchor):
    ``rdt edit cleardate "<ref>" digital --predicted`` drops just the model rows and lets a
    confirmed pull or a hand-authored date surface. Omit the flags to wipe the channel entirely.
    """
    configure_logging()
    if predicted and provider is not None and provider != "model":
        raise typer.BadParameter("--predicted is shorthand for --provider model; don't combine")
    db = _db()
    entity = _edit_entity(db, ref)
    if entity is None:
        raise typer.Exit(1)
    try:
        chan = ReleaseChannel(channel.lower())
    except ValueError:
        db.close()
        raise typer.BadParameter(f"unknown channel {channel!r}") from None
    which = "model" if predicted else provider
    removed = db.clear_observations(entity.id, chan, provider=which)
    db.close()
    scope = f" (provider={which})" if which else ""
    if removed:
        console.print(
            f"[green]Cleared[/] {removed} {chan.value} date(s){scope} from {entity.title}."
        )
    else:
        console.print(f"[yellow]No {chan.value} dates{scope}[/] to clear on {entity.title}.")


@edit_app.command("contingency")
def edit_contingency(
    ref: Annotated[str, typer.Argument(help="the work (id or title)")],
    dim: Annotated[
        str, typer.Argument(help="dimension: region|platform|os|language|tech|<custom>")
    ],
    value: Annotated[str, typer.Argument(help="the value, e.g. ps5 / linux / en / DE")],
    channel: Annotated[str, typer.Option("--channel", help="release channel")] = "primary",
    date_edtf: Annotated[
        str | None, typer.Option("--date", help="EDTF date for this facet; omit for undated (TBA)")
    ] = None,
) -> None:
    """Tag an availability facet on a work (platform/OS/language/region/custom), optionally dated.

    A dated facet ("PS5 version out 2026-03") gates availability for that profile; an undated
    one ("PS5 SKU exists, date TBD") surfaces as pending. Region is the native column; every
    other dimension is an extensible contingency tag.
    """
    configure_logging()
    db = _db()
    entity = _edit_entity(db, ref)
    if entity is None:
        raise typer.Exit(1)
    try:
        chan = ReleaseChannel(channel.lower())
    except ValueError:
        db.close()
        raise typer.BadParameter(f"unknown channel {channel!r}") from None
    parsed = None
    if date_edtf:
        try:
            parsed = parse_edtf(date_edtf)
        except ValueError as exc:
            db.close()
            raise typer.BadParameter(str(exc)) from None
    confirmed = parsed is not None and parsed.certainty is Certainty.CONFIRMED
    dim_l = dim.strip().lower()
    region = value.strip().upper() if dim_l == "region" else "WW"
    conts = {} if dim_l == "region" else {dim_l: value.strip().lower()}
    db.upsert_observation(
        ReleaseObservation(
            entity_id=entity.id,
            channel=chan,
            region=region,
            contingencies=conts,
            release_date=parsed.when if parsed else None,
            date_end=parsed.end if parsed else None,
            precision=parsed.precision if parsed else DatePrecision.TBA,
            certainty=parsed.certainty if parsed else Certainty.ESTIMATED,
            source_tier=SourceTier.OFFICIAL if confirmed else SourceTier.RUMOR,
            provider="manual",
            source_name="Manual (contingency)",
            confidence=1.0 if confirmed else 0.5,
            fetched_at=datetime.now(UTC),
        )
    )
    db.close()
    when = f" → {to_edtf(parsed.when, parsed.precision, parsed.certainty)}" if parsed else " (TBA)"
    console.print(f"[green]Facet[/] {entity.title}: [bold]{dim_l}={value}[/]{when}")


@edit_app.command("condition")
def edit_condition(
    name: Annotated[str, typer.Argument(help="the external blocker, e.g. 'EAC Linux support'")],
    status: Annotated[str, typer.Argument(help="resolved | pending | never")],
    edtf: Annotated[str | None, typer.Argument(help="EDTF date (required for 'resolved')")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
) -> None:
    """Author/update an external blocker condition (shared across every work it gates)."""
    configure_logging()
    try:
        st = ResolutionStatus(status.lower())
    except ValueError:
        raise typer.BadParameter("status must be resolved|pending|never") from None
    resolve_date: date | None = None
    precision = DatePrecision.TBA
    if st is ResolutionStatus.RESOLVED:
        if not edtf:
            raise typer.BadParameter("a 'resolved' condition needs a date (EDTF)")
        try:
            parsed = parse_edtf(edtf)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None
        resolve_date, precision = parsed.when, parsed.precision
    db = _db()
    node = Node.create(NodeKind.CONDITION, name, owned=True)
    db.upsert_node(node)
    db.upsert_condition(
        Condition(
            node_id=node.id,
            status=st.value,
            resolve_date=resolve_date,
            precision=precision,
            note=note,
        )
    )
    db.close()
    when = f" ({resolve_date.isoformat()})" if resolve_date else ""
    console.print(f"[green]Condition[/] [bold]{name}[/] → {st.value}{when}")


@edit_app.command("blocked-by")
def edit_blocked_by(
    ref: Annotated[str, typer.Argument(help="the work (id or title)")],
    condition: Annotated[str, typer.Argument(help="the blocker condition name")],
) -> None:
    """Record that a work's availability is BLOCKED_BY an external condition (find-or-create it)."""
    configure_logging()
    db = _db()
    entity = _edit_entity(db, ref)
    if entity is None:
        raise typer.Exit(1)
    node = Node.create(NodeKind.CONDITION, condition, owned=True)
    db.upsert_node(node)
    db.upsert_edge(
        Edge(
            src_id=entity.id,
            dst_id=node.id,
            relation=RelationKind.BLOCKED_BY,
            source_provider="user",
            source_tier=SourceTier.OFFICIAL,
            confidence=1.0,
            owned=True,
        )
    )
    db.close()
    console.print(f"[green]Blocked[/] {entity.title} [dim]← needs[/] [bold]{condition}[/]")


@edit_app.command("unblock")
def edit_unblock(
    ref: Annotated[str, typer.Argument(help="the work (id or title)")],
    condition: Annotated[str, typer.Argument(help="the blocker to drop")],
) -> None:
    """Drop a BLOCKED_BY link (the condition node itself is left intact for other works)."""
    configure_logging()
    db = _db()
    entity = _edit_entity(db, ref)
    if entity is None:
        raise typer.Exit(1)
    node_id = Node.make_id(NodeKind.CONDITION, condition)
    removed = sum(
        db.delete_edge(e.id)
        for e in db.edges_from(entity.id, RelationKind.BLOCKED_BY)
        if e.dst_id == node_id
    )
    db.close()
    console.print(f"[green]Unblocked[/] {entity.title}: dropped {removed} link(s) to {condition}")


@app.command()
def stale(
    days: Annotated[
        int | None, typer.Option(help="older than N days (default: RDT_STALE_DAYS)")
    ] = None,
) -> None:
    """Speculative dates not refreshed recently — candidates to re-`pull`/`enrich`."""
    configure_logging()
    settings = get_settings()
    threshold = days if days is not None else settings.stale_days
    today = _today()
    db = _db()
    flagged: list[tuple[int, str, object]] = []
    for entity in db.iter_entities():
        for est in best_estimates(db.iter_observations(entity.id)):
            speculative = est.certainty in (Certainty.PREDICTED, Certainty.ESTIMATED)
            if speculative and est.fetched_at and (today - est.fetched_at.date()).days > threshold:
                flagged.append(((today - est.fetched_at.date()).days, entity.title, est))
    db.close()
    flagged.sort(key=lambda t: t[0], reverse=True)
    table = Table(title=f"Stale speculative dates (> {threshold}d)", show_lines=False)
    for col in ("Age", "Title", "Channel", "Date", "Certainty"):
        table.add_column(col)
    for age, title, est in flagged:
        assert isinstance(est, BestEstimate)
        table.add_row(
            f"{age}d",
            title,
            est.channel.value,
            est.release_date.isoformat() if est.release_date else "—",
            est.certainty.value,
        )
    console.print(table)
    if not flagged:
        console.print("[dim]No stale speculative dates. `rdt pull --all` keeps them fresh.[/]")


# --- tracker helpers ------------------------------------------------------
async def _resolve_and_enrich(
    db: Database, settings: Settings, entity: Entity, *, include_themes: bool
) -> EnrichSummary:
    entity = await _ensure_resolved(db, settings, entity)
    async with make_client() as client:
        return await enrich_work(client, db, settings, entity, include_themes=include_themes)


async def _ensure_resolved(db: Database, settings: Settings, entity: Entity) -> Entity:
    """Make sure the entity has a resolvable kind + canonical ids before enrichment."""
    if not matching.is_resolvable(entity.kind):
        # captured without a (usable) kind — auto-detect via the rd lookup, then re-key.
        report = await lookup(entity.title, settings)
        if report.found and report.kind and matching.is_resolvable(report.kind):
            entity = _rekey(db, entity, report.kind, report.canonical)
    await pull_entity(db, settings, entity)
    return db.get_entity(entity.id) or entity


def _rekey(db: Database, old: Entity, new_kind: MediaKind, canonical: dict[str, str]) -> Entity:
    """Replace an unresolved capture with a correctly-kinded entity (id changes)."""
    new = Entity.create(
        old.title,
        new_kind,
        external_ids=dict(canonical),
        notes=old.notes,
        watch=old.watch,
        notion_page_id=old.notion_page_id,
    )
    if new.id != old.id:
        db.delete_entity(old.id)
    db.upsert_entity(new)
    console.print(f"[dim]Detected[/] {old.title} → [bold]{new_kind.value}[/]")
    return new


def _print_enrich(entity: Entity, s: EnrichSummary) -> None:
    if not s.resolved:
        console.print(
            f"[yellow]Could not resolve[/] {entity.title} — "
            f'try `rdt resolve show "{entity.title}"` to pin an id.'
        )
        return
    console.print(
        f"[green]Enriched[/] {entity.title}: {s.people} people, {s.orgs} orgs, "
        f"{s.genres} genres, {s.themes} themes, {s.platforms} platforms, {s.series} series."
    )


def _fmt_tag(tag: views.TagLine) -> str:
    return f"[dim]~{tag.name}[/]" if tag.predicted else tag.name


def _fmt_when(when: date | None, precision: DatePrecision) -> str:
    """Render a date at its own precision, so the string conveys how firm it is."""
    if when is None:
        return "—"
    match precision:
        case DatePrecision.YEAR:
            return str(when.year)
        case DatePrecision.QUARTER:
            return f"{when.year} Q{(when.month - 1) // 3 + 1}"
        case DatePrecision.MONTH:
            return when.strftime("%Y-%m")
        case _:
            return when.isoformat()


def _stance_color(confirmed: bool) -> str:
    """Configurable confirmed/speculative color (color-blind-safe cyan/orange by default)."""
    s = get_settings()
    return s.confirmed_color if confirmed else s.speculative_color


def _fresh_color(freshness: views.Freshness) -> str:
    """Configurable freshness-dot color for the fresh/aging/stale ramp."""
    s = get_settings()
    return {"fresh": s.fresh_color, "aging": s.aging_color, "stale": s.stale_color}[freshness]


def _legend_dots() -> str:
    """The fresh/aging/stale legend swatch, using the configured colors."""
    s = get_settings()
    return f"[{s.fresh_color}]●[/]fresh [{s.aging_color}]●[/]aging [{s.stale_color}]●[/]stale"


def _fmt_cell(cell: views.DateCell | None) -> str:
    """A date colored by stance (confirmed vs speculative); colors are configurable.

    A window renders as ``start-end`` (e.g. 2027-2029), preserving the ambiguity.
    """
    if cell is None or cell.when is None:
        return "[dim]—[/]"
    text = _fmt_when(cell.when, cell.precision)
    if cell.end is not None:
        text = f"{text}–{_fmt_when(cell.end, cell.precision)}"  # noqa: RUF001
    return f"[{_stance_color(cell.confirmed)}]{text}[/]"


def _fresh_dot(freshness: views.Freshness | None) -> str:
    return f"[{_fresh_color(freshness)}]●[/]" if freshness else "[dim]·[/]"


def _title_cell(row: views.TrackRow) -> str:
    title = f"{row.title} [yellow]*[/]" if row.has_notes else row.title
    if row.blockers:  # an unsatisfied profile / unresolved blocker — flagged, never hidden
        title += f" [red]⛔[/] [dim]{row.blockers[0]}[/]"
    return title


def _wcw(table: Table) -> None:
    """The shared who/where/what columns."""
    table.add_column("Who", max_width=16, no_wrap=True, overflow="ellipsis")
    table.add_column("Where", max_width=12, no_wrap=True, overflow="ellipsis")
    table.add_column("What", max_width=40, no_wrap=True, overflow="ellipsis")


def _wcw_cells(r: views.TrackRow) -> tuple[str, str, str]:
    return (
        ", ".join(r.who) or "[dim]—[/]",
        ", ".join(r.where) or "[dim]—[/]",
        ", ".join(_fmt_tag(t) for t in r.what) or "[dim]—[/]",
    )


def _render_upcoming(rows: list[views.TrackRow], days: int | None) -> None:
    title = "Upcoming releases" + (f" · next {days}d" if days else "")
    region = (get_settings().regions or ("US",))[0]
    table = Table(title=title, show_lines=False)
    table.add_column(f"Theatrical {region}", min_width=10, no_wrap=True)
    table.add_column("Digital", min_width=10, no_wrap=True)
    table.add_column("⟳", width=1, no_wrap=True)
    table.add_column("Title", min_width=18, max_width=30, no_wrap=True, overflow="ellipsis")
    table.add_column("Kind", min_width=5, no_wrap=True)
    _wcw(table)
    today = _today()
    prev_month: tuple[int, int] | None = None
    for r in rows:
        # a stale past guess is no firmer than no date at all — both land in the TBA tail
        firm = r.pivot_when if (r.pivot_when and r.pivot_when >= today) else None
        month = (firm.year, firm.month) if firm else (9999, 12)
        if prev_month is not None and month != prev_month:
            table.add_section()  # cluster the spreadsheet by month
        prev_month = month
        # "no date yet" only for the truly date-less; a released-but-blocked/stale row still
        # shows its real (past) date — it sorts to the tail but isn't pretending to be undated.
        no_date = r.pivot_when is None
        table.add_row(
            "[dim]no date[/]" if no_date else _fmt_cell(r.theatrical),
            "[dim]yet[/]" if no_date else _fmt_cell(r.digital),
            _fresh_dot(r.freshness),
            _title_cell(r),
            r.kind.value,
            *_wcw_cells(r),
        )
    console.print(table)
    if rows:
        console.print(
            f"[dim]dates: [{_stance_color(True)}]confirmed[/] / "
            f"[{_stance_color(False)}]speculative[/]   "
            "[dim]no date yet[/] = announced but not datable   "
            f"⟳ {_legend_dots()}   [yellow]*[/]=notes[/]"
        )
    else:
        console.print("[dim]No upcoming releases. `rdt add` then `rdt enrich`.[/]")


def _watched_state_label(state: ConsumptionState) -> str:
    """Color-code the finished states in the watched view."""
    match state:
        case ConsumptionState.DROPPED:
            return "[red]dropped[/]"
        case ConsumptionState.SKIPPED:
            return "[yellow]skipped[/]"
        case _:
            return state.value


def _render_watched(rows: list[views.TrackRow]) -> None:
    table = Table(title="Watched · done", show_lines=False)
    table.add_column("Released", min_width=10, no_wrap=True)
    table.add_column("Title", min_width=18, max_width=32, no_wrap=True, overflow="ellipsis")
    table.add_column("Kind", min_width=5, no_wrap=True)
    table.add_column("State", min_width=8, no_wrap=True)
    _wcw(table)
    for r in rows:
        state = _watched_state_label(r.state)
        table.add_row(
            r.pivot_when.isoformat() if r.pivot_when else "[dim]—[/]",
            _title_cell(r),
            r.kind.value,
            state,
            *_wcw_cells(r),
        )
    console.print(table)
    if rows:
        console.print(f"[dim]{len(rows)} finished   [yellow]*[/]=notes[/]")
    else:
        console.print("[dim]Nothing finished yet. `rdt state <title> watched`.[/]")


def _render_available(rows: list[views.TrackRow]) -> None:
    table = Table(title="Available now · unwatched", show_lines=False)
    table.add_column("⟳", width=1, no_wrap=True)
    table.add_column("Title", min_width=18, max_width=32, no_wrap=True, overflow="ellipsis")
    table.add_column("Kind", min_width=5, no_wrap=True)
    table.add_column("State", min_width=8, no_wrap=True)
    _wcw(table)
    for r in rows:
        table.add_row(
            _fresh_dot(r.freshness),
            _title_cell(r),
            r.kind.value,
            r.state.value,
            *_wcw_cells(r),
        )
    console.print(table)
    if rows:
        console.print(
            "[dim]⟳ data freshness   [yellow]*[/]=notes   `rdt state <title> watched` to clear[/]"
        )
    else:
        console.print("[dim]Nothing out + unwatched. Set state with `rdt state <title> want`.[/]")


def _render_card(card: views.WorkCard) -> None:
    e = card.entity
    if card.series:
        name = ", ".join(card.series)
        label = f"Season {card.season} of {name}" if card.season is not None else name
        series = f" [dim]· {label}[/]"
    else:
        series = ""
    console.print(f"[bold]{e.title}[/] [dim]({e.kind.value})[/]{series}")
    _render([(e.title, e.kind, est) for est in card.estimates])
    if card.credits:
        console.print("[bold]Who[/]")
        for c in card.credits:
            owned = " [green](yours)[/]" if c.owned else ""
            console.print(f"  [dim]{c.role.value:<10}[/] {c.name}{owned}")
    if card.platforms:
        where = ", ".join(f"[dim]~{p.name}[/]" if p.predicted else p.name for p in card.platforms)
        console.print(f"[bold]Where[/] {where}")
    if card.tags:
        # bucket by descriptor kind (a user-authored theme is OFFICIAL-tier, not a genre);
        # predicted themes get a "~" prefix, matching the platform convention above.
        origins = [t.name for t in card.tags if t.kind is DescriptorKind.ORIGIN]
        if origins:
            console.print(f"[bold]Origin[/] {', '.join(origins)}")
        genres = [t.name for t in card.tags if t.kind is DescriptorKind.GENRE]
        themes = [
            f"~{t.name}" if t.predicted else t.name
            for t in card.tags
            if t.kind is DescriptorKind.THEME
        ]
        if genres:
            console.print(f"[bold]Genre[/] {', '.join(genres)}")
        if themes:
            console.print(f"[bold]Themes[/] [dim]{', '.join(themes)} (~ = model-derived)[/]")
    if card.derived_from:
        for r in card.derived_from:
            console.print(f"[bold]Derived from[/] {r.node.name} [dim]({r.relation.value})[/]")
    if card.derivatives:
        grouped = ", ".join(f"{r.node.name} [dim]({r.relation.value})[/]" for r in card.derivatives)
        console.print(f"[bold]Derivatives[/] {grouped}")
    if card.blockers:
        console.print("[bold]Blocked by[/]")
        for b in card.blockers:
            if b.status == "resolved":
                state = (
                    f"[{_stance_color(True)}]✓ {b.when.isoformat() if b.when else 'resolved'}[/]"
                )
            elif b.status == "never":
                state = "[red]✗ never[/]"
            else:
                state = f"[{_stance_color(False)}]⏳ pending[/]"
            console.print(f"  {state} {b.name}")


# ---------------------------------------------------------------------------
# resolve: manual canonical-id matching
# ---------------------------------------------------------------------------
def _resolve_ref(db: Database, ref: str) -> Entity | None:
    """Resolve an entity reference (id or title substring), printing on ambiguity."""
    matches = db.find_entities(ref)
    if not matches:
        console.print(f"[red]No entity matches[/] '{ref}'.")
        return None
    if len(matches) > 1:
        console.print(f"[yellow]Ambiguous[/] '{ref}' — matches:")
        for e in matches[:10]:
            console.print(f"  [dim]{e.id}[/]  {e.title} ({e.kind.value})")
        console.print("Be more specific or pass the full id.")
        return None
    return matches[0]


def _candidate_table(title: str, cands: list[Candidate]) -> Table:
    table = Table(title=title, show_lines=False)
    for col in ("#", "Score", "Source", "id_key", "Canonical ID", "Title", "Year", "Info"):
        table.add_column(col)
    for i, c in enumerate(cands, 1):
        table.add_row(
            str(i),
            f"{c.score:.2f}",
            c.source,
            c.id_key,
            c.canonical_id,
            c.title,
            str(c.year) if c.year else "—",
            c.extra or "",
        )
    return table


def _apply_pins(db: Database, entity: Entity, pairs: dict[str, str]) -> None:
    db.merge_external_ids(entity.id, pairs)
    # drop stale rows for the affected providers so the next pull is clean
    providers = {"tmdb" if k == "tmdb" else "igdb" if k == "igdb" else "steam" for k in pairs}
    db.delete_observations(entity.id, tuple(providers))
    console.print(f"[green]Pinned[/] {pairs} → [bold]{entity.title}[/] (stale rows cleared).")


def _parse_pairs(tokens: list[str]) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for tok in tokens:
        if "=" not in tok:
            raise typer.BadParameter(f"expected key=value, got '{tok}'")
        key, _, value = tok.partition("=")
        pairs[key.strip()] = value.strip()
    return pairs


@resolve_app.command("list")
def resolve_list(
    include_released: Annotated[bool, typer.Option("--include-released")] = False,
) -> None:
    """Show entities still needing a canonical id (upcoming + unresolved)."""
    configure_logging()
    db = _db()
    work = matching.build_worklist(db, _today(), include_released=include_released)
    table = Table(title=f"Needs resolution ({len(work)})", show_lines=False)
    for col in ("Title", "Kind", "Need", "Have", "Known date"):
        table.add_column(col)
    for e in work:
        dates = db.observation_dates(e.id)
        nxt = min((d for d in dates), default=None)
        table.add_row(
            e.title,
            e.kind.value,
            matching.required_id_key(e.kind) or "—",
            ", ".join(f"{k}={v}" for k, v in e.external_ids.items()) or "—",
            nxt.isoformat() if nxt else "—",
        )
    db.close()
    console.print(table)


@resolve_app.command("search")
def resolve_search(
    query: str,
    kind: Annotated[str, typer.Option(help="movie|tv|game")] = "movie",
    limit: Annotated[int, typer.Option()] = 6,
) -> None:
    """Hardened candidate search for a raw query (no entity needed)."""
    configure_logging()
    settings = get_settings()
    media = MediaKind(kind)

    async def go() -> tuple[list[Candidate], WebInfo | None]:
        from release_tracker.sources import sources_for

        async with make_client() as client:
            out: list[Candidate] = []
            for src in sources_for(media):
                found = await src.search_candidates(client, query, media, settings, limit=limit)
                for c in found:
                    c.score = matching.score_candidate(query, None, c, media)
                out.extend(found)
            out.sort(key=lambda c: c.score, reverse=True)
            # nothing matched the structured sources — same gap a manual search would fill
            web = await instant_answer(client, query) if not out else None
        return out, web

    cands, web = asyncio.run(go())
    console.print(_candidate_table(f"Candidates for '{query}' ({kind})", cands))
    _render_web_info(web)


@resolve_app.command("show")
def resolve_show(
    ref: str,
    limit: Annotated[int, typer.Option()] = 6,
) -> None:
    """Show ranked canonical candidates for one entity (by id or title substring)."""
    configure_logging()
    settings = get_settings()
    db = _db()
    entity = _resolve_ref(db, ref)
    if entity is None:
        db.close()
        raise typer.Exit(1)
    hint = matching.year_hint(db.observation_dates(entity.id), _today())

    async def go() -> tuple[list[Candidate], WebInfo | None]:
        async with make_client() as client:
            cands = await matching.candidates_for(
                client, entity, settings, hint_year=hint, limit=limit
            )
            web = await instant_answer(client, entity.title) if not cands else None
            return cands, web

    cands, web = asyncio.run(go())
    db.close()
    console.print(_candidate_table(f"{entity.title} (hint year: {hint or '—'})", cands))
    _render_web_info(web)
    console.print(
        f'[dim]Pin with:[/] rdt resolve pin "{entity.title}" '
        f"{matching.required_id_key(entity.kind)}=<id>"
    )


@resolve_app.command("pin")
def resolve_pin(
    ref: str,
    pairs: Annotated[list[str], typer.Argument(help="key=value, e.g. tmdb=693134")],
) -> None:
    """Manually pin canonical ids to an entity, e.g. `pin "Blade" igdb=1234 steam_appid=5678`."""
    configure_logging()
    db = _db()
    entity = _resolve_ref(db, ref)
    if entity is None:
        db.close()
        raise typer.Exit(1)
    _apply_pins(db, entity, _parse_pairs(pairs))
    db.close()


@resolve_app.command("run")
def resolve_run(
    include_released: Annotated[bool, typer.Option("--include-released")] = False,
    limit: Annotated[int, typer.Option()] = 6,
) -> None:
    """Interactively walk the worklist: show candidates, pick the id to pin."""
    configure_logging()
    settings = get_settings()
    db = _db()
    today = _today()
    work = matching.build_worklist(db, today, include_released=include_released)
    if not work:
        console.print("[green]Nothing to resolve.[/] All upcoming entities have canonical ids.")
        db.close()
        return
    console.print(
        f"[bold]{len(work)}[/] to resolve. [dim]At each: number(s) to pin, "
        "'m key=val' manual, Enter to skip, 'q' to quit.[/]\n"
    )
    for entity in work:
        hint = matching.year_hint(db.observation_dates(entity.id), today)

        async def go(e: Entity = entity, h: int | None = hint) -> list[Candidate]:
            async with make_client() as client:
                return await matching.candidates_for(client, e, settings, hint_year=h, limit=limit)

        cands = asyncio.run(go())
        console.print(_candidate_table(f"{entity.title} ({entity.kind.value})", cands))
        choice = typer.prompt(f"{entity.title}", default="", show_default=False).strip()
        if choice.lower() == "q":
            break
        if not choice:
            continue
        if choice.lower().startswith("m"):
            _apply_pins(db, entity, _parse_pairs(choice[1:].split()))
            continue
        if "=" in choice:
            _apply_pins(db, entity, _parse_pairs(choice.split()))
            continue
        try:
            picks = [cands[int(n) - 1] for n in choice.replace(",", " ").split()]
        except (ValueError, IndexError):
            console.print("[yellow]Unrecognised; skipping.[/]")
            continue
        _apply_pins(db, entity, {c.id_key: c.canonical_id for c in picks})
    db.close()
    console.print("[dim]Done. Run `rdt pull` to refresh with the pinned ids.[/]")


if __name__ == "__main__":
    app()
