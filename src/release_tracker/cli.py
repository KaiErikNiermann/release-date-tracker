"""Command-line interface.

rdt seed sync [--source local|notion]   # load watchlist into the DB
rdt pull [--all]                        # run Tier-0 pullers over watched entities
rdt show [--region US] [--kind game]    # ranked best estimates
rdt entities                            # list tracked entities
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from release_tracker.config import get_settings
from release_tracker.db import Database
from release_tracker.logging import configure_logging
from release_tracker.models import MediaKind
from release_tracker.pipeline import pull_all
from release_tracker.resolve import best_estimates
from release_tracker.seed import LocalSeed, NotionSeed, SeedProvider

app = typer.Typer(add_completion=False, help="Free-first release date tracker.")
seed_app = typer.Typer(help="Manage the entity watchlist (seed).")
app.add_typer(seed_app, name="seed")
console = Console()


def _db() -> Database:
    settings = get_settings()
    return Database(settings.db_path)


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


if __name__ == "__main__":
    app()
