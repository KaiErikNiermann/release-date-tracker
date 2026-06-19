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
  rdt rd "<title>"                      # one-shot lookup (the /rd skill engine)
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
from release_tracker.config import Settings, get_settings
from release_tracker.db import Database
from release_tracker.enrich import EnrichSummary, enrich_work
from release_tracker.logging import configure_logging
from release_tracker.lookup import RdReport, lookup
from release_tracker.models import (
    BestEstimate,
    Certainty,
    ConsumptionState,
    DatePrecision,
    Entity,
    MediaKind,
    Node,
    NodeKind,
)
from release_tracker.pipeline import pull_all, pull_entity
from release_tracker.resolve import best_estimates
from release_tracker.seed import LocalSeed, NotionSeed, SeedProvider
from release_tracker.sources.base import Candidate, make_client

app = typer.Typer(add_completion=False, help="Free-first release date tracker.")
seed_app = typer.Typer(help="Manage the entity watchlist (seed).")
resolve_app = typer.Typer(help="Find canonical ids and pin them to entities (manual matching).")
app.add_typer(seed_app, name="seed")
app.add_typer(resolve_app, name="resolve")
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
        str | None, typer.Option(help="movie|tv|game|anime|tech — auto-detected if omitted")
    ] = None,
    region: Annotated[
        str | None,
        typer.Option(help="ISO country for tech (hard constraint; defaults to RDT_REGIONS[0])"),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="emit machine-readable JSON (for the /rd skill)")
    ] = False,
) -> None:
    """One-shot lookup: confirmed + speculative release dates for a single title."""
    configure_logging()
    settings = get_settings()
    kind_hint = MediaKind(kind) if kind else None
    report = asyncio.run(lookup(name, settings, kind_hint=kind_hint, region=region))
    if as_json:
        print(json.dumps(report.to_dict(), indent=2))
        return
    _render_report(report)


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
        raise typer.Exit(2)
    kind_label = r.kind.value if r.kind else "?"
    console.print(f"[bold]{r.matched_title}[/] [dim]({kind_label})[/]")
    table = Table(show_header=True, header_style="bold")
    for col in ("What", "Date", "± days", "Stance", "Conf.", "Basis"):
        table.add_column(col)
    for c in r.claims:
        stance = "[green]confirmed[/]" if c.stance == "confirmed" else "[yellow]speculative[/]"
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
    for note in r.notes:
        console.print(f"[dim]• {note}[/]")
    if r.url:
        console.print(f"[dim]{r.url}[/]")


def _render(rows: list[tuple[str, MediaKind, object]]) -> None:
    from release_tracker.models import BestEstimate  # local import for typing only

    table = Table(title="Release best-estimates", show_lines=False)
    for col in ("Title", "Kind", "Channel", "Region", "Date", "Prec.", "Stance", "Price", "Conf."):
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
            est.release_date.isoformat() if est.release_date else "—",
            est.precision.value,
            est.certainty.value,
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
        str | None, typer.Option(help="movie|tv|game|anime|tech|...; detected on enrich if omitted")
    ] = None,
    now: Annotated[bool, typer.Option("--now", help="resolve + enrich immediately")] = False,
    no_themes: Annotated[
        bool, typer.Option("--no-themes", help="skip LLM theme extraction")
    ] = False,
) -> None:
    """Capture a title instantly. Resolve + enrich later (or now with --now)."""
    configure_logging()
    settings = get_settings()
    media = MediaKind(kind) if kind else MediaKind.OTHER
    db = _db()
    entity = Entity.create(name, media)
    db.upsert_entity(entity)
    db.upsert_node(
        Node(id=entity.id, node_kind=NodeKind.WORK, name=name, owned=True, external_ids={})
    )
    console.print(f"[green]Added[/] {name} [dim]({media.value})[/]")
    if now:
        summary = asyncio.run(
            _resolve_and_enrich(db, settings, entity, include_themes=not no_themes)
        )
        _print_enrich(entity, summary)
    else:
        console.print(f'[dim]Run `rdt enrich "{name}"` to populate who/where/what.[/]')
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
        table.add_row(
            f"S{s.season}" if s.season is not None else "[dim]—[/]",
            s.entity.title,
            s.when.isoformat() if s.when else "—",
            "[green]✓ yours[/]" if s.owned else "[dim]world[/]",
        )
    console.print(table)


@app.command()
def state(
    ref: Annotated[str, typer.Argument(help="entity id or title substring")],
    value: Annotated[str, typer.Argument(help="unset | want | watching | watched | dropped")],
) -> None:
    """Set a title's watch/play state (want/watching/watched/dropped)."""
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


_FRESH_COLOR: dict[str, str] = {"fresh": "green", "aging": "yellow", "stale": "red"}


def _fmt_cell(cell: views.DateCell | None) -> str:
    """A date colored by stance: green = confirmed, yellow = speculative."""
    if cell is None or cell.when is None:
        return "[dim]—[/]"
    text = _fmt_when(cell.when, cell.precision)
    return f"[green]{text}[/]" if cell.confirmed else f"[yellow]{text}[/]"


def _fresh_dot(freshness: views.Freshness | None) -> str:
    return f"[{_FRESH_COLOR[freshness]}]●[/]" if freshness else "[dim]·[/]"


def _title_cell(row: views.TrackRow) -> str:
    return f"{row.title} [yellow]*[/]" if row.has_notes else row.title


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
    prev_month: tuple[int, int] | None = None
    for r in rows:
        month = (r.pivot_when.year, r.pivot_when.month) if r.pivot_when else (9999, 12)
        if prev_month is not None and month != prev_month:
            table.add_section()  # cluster the spreadsheet by month
        prev_month = month
        table.add_row(
            _fmt_cell(r.theatrical),
            _fmt_cell(r.digital),
            _fresh_dot(r.freshness),
            _title_cell(r),
            r.kind.value,
            *_wcw_cells(r),
        )
    console.print(table)
    if rows:
        console.print(
            "[dim]dates: [green]confirmed[/] / [yellow]speculative[/]   "
            "⟳ [green]●[/]fresh [yellow]●[/]aging [red]●[/]stale   [yellow]*[/]=notes[/]"
        )
    else:
        console.print("[dim]No upcoming releases. `rdt add` then `rdt enrich`.[/]")


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
        genres = [t.name for t in card.tags if not t.predicted]
        themes = [t.name for t in card.tags if t.predicted]
        if genres:
            console.print(f"[bold]Genre[/] {', '.join(genres)}")
        if themes:
            console.print(f"[bold]Themes[/] [dim]{', '.join(themes)} (model-derived)[/]")


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
    kind: Annotated[str, typer.Option(help="movie|tv|game|anime")] = "movie",
    limit: Annotated[int, typer.Option()] = 6,
) -> None:
    """Hardened candidate search for a raw query (no entity needed)."""
    configure_logging()
    settings = get_settings()
    media = MediaKind(kind)

    async def go() -> list[Candidate]:
        from release_tracker.sources import sources_for

        async with make_client() as client:
            out: list[Candidate] = []
            for src in sources_for(media):
                found = await src.search_candidates(client, query, media, settings, limit=limit)
                for c in found:
                    c.score = matching.score_candidate(query, None, c, media)
                out.extend(found)
        out.sort(key=lambda c: c.score, reverse=True)
        return out

    console.print(_candidate_table(f"Candidates for '{query}' ({kind})", asyncio.run(go())))


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

    async def go() -> list[Candidate]:
        async with make_client() as client:
            return await matching.candidates_for(
                client, entity, settings, hint_year=hint, limit=limit
            )

    cands = asyncio.run(go())
    db.close()
    console.print(_candidate_table(f"{entity.title} (hint year: {hint or '—'})", cands))
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
