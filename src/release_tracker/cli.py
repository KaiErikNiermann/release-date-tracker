"""Command-line interface.

rdt seed sync [--source local|notion]   # load watchlist into the DB
rdt pull [--all]                        # run Tier-0 pullers over watched entities
rdt show [--region US] [--kind game]    # ranked best estimates
rdt entities                            # list tracked entities
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from release_tracker import matching
from release_tracker.config import get_settings
from release_tracker.db import Database
from release_tracker.logging import configure_logging
from release_tracker.lookup import RdReport, lookup
from release_tracker.models import Entity, MediaKind
from release_tracker.pipeline import pull_all
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
    table.add_column("Title")
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
