"""The anti-drift check: `rdt find` and the library predicate must agree, always.

The whole reason the query language lives in `release_tracker.query` rather than in a
frontend is that the CLI and the TUI should be incapable of disagreeing about what a
query means. This asserts that mechanically for the CLI half, so a divergence fails the
build instead of being discouraged in a comment.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from release_tracker import cli, query, views
from release_tracker.config import get_settings
from release_tracker.db import Database
from release_tracker.models import (
    Certainty,
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
    SourceTier,
)

runner = CliRunner()
TODAY = date(2026, 6, 1)


def _work(
    db: Database,
    title: str,
    kind: MediaKind,
    when: date,
    *,
    state: ConsumptionState,
    director: str | None = None,
    cast: tuple[str, ...] = (),
    genres: tuple[str, ...] = (),
    themes: tuple[str, ...] = (),
) -> Entity:
    ent = Entity.create(title, kind)
    ent = ent.model_copy(update={"consumption_state": state})
    db.upsert_entity(ent)
    db.upsert_node(Node(id=ent.id, node_kind=NodeKind.WORK, name=title, owned=True))
    db.upsert_observations(
        [
            ReleaseObservation(
                entity_id=ent.id,
                channel=(
                    ReleaseChannel.DIGITAL if kind is MediaKind.MOVIE else ReleaseChannel.PRIMARY
                ),
                region="US",
                release_date=when,
                precision=DatePrecision.EXACT,
                certainty=Certainty.CONFIRMED,
                source_tier=SourceTier.OFFICIAL,
                provider="test",
                source_name="test",
                confidence=1.0,
                fetched_at=datetime.now(UTC),
            )
        ]
    )
    for name, role in ((director, CreditRole.DIRECTOR), *((c, CreditRole.CAST) for c in cast)):
        if name is None:
            continue
        node = Node.create(NodeKind.PERSON, name, source="test", source_id=name)
        db.upsert_node(node)
        db.upsert_edge(
            Edge(
                src_id=node.id,  # credited_on runs person -> work
                dst_id=ent.id,
                relation=RelationKind.CREDITED_ON,
                role=role,
                source_provider="test",
                source_tier=SourceTier.AGGREGATOR,
            )
        )
    for names, dkind, tier in (
        (genres, DescriptorKind.GENRE, SourceTier.AGGREGATOR),
        (themes, DescriptorKind.THEME, SourceTier.MODEL),
    ):
        for name in names:
            node = Node.create(NodeKind.DESCRIPTOR, name, descriptor_kind=dkind)
            db.upsert_node(node)
            db.upsert_edge(
                Edge(
                    src_id=ent.id,
                    dst_id=node.id,
                    relation=RelationKind.EXHIBITS,
                    source_provider="test",
                    source_tier=tier,
                )
            )
    return ent


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "q.db"
    db = Database(path)
    _work(
        db,
        "Dune: Part Three",
        MediaKind.MOVIE,
        date(2026, 12, 18),
        state=ConsumptionState.WANT,
        director="Denis Villeneuve",
        cast=("Timothee Chalamet", "Zendaya", "Josh O'Connor"),
        genres=("Science Fiction", "Adventure"),
        themes=("destiny", "holy war"),
    )
    _work(
        db,
        "Weapons",
        MediaKind.MOVIE,
        date(2025, 9, 8),
        state=ConsumptionState.WATCHED,
        director="Zach Cregger",
        genres=("Horror", "Mystery"),
        themes=("community paranoia",),
    )
    _work(
        db,
        "Reacher: Season 4",
        MediaKind.TV,
        date(2026, 2, 1),
        state=ConsumptionState.WATCHING,
        cast=("Alan Ritchson",),
        genres=("Action & Adventure",),
    )
    _work(
        db,
        "Hollow Knight: Silksong",
        MediaKind.GAME,
        date(2025, 9, 4),
        state=ConsumptionState.DROPPED,
        genres=("Adventure",),
    )
    db.close()
    monkeypatch.setattr(cli, "_db", lambda: Database(path))
    monkeypatch.setattr(cli, "_today", lambda: TODAY)
    return path


def _cli_rows(expr: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Run `rdt find` and capture the rows it actually rendered."""
    captured: list[views.TrackRow] = []

    def capture(rows: list[views.TrackRow], _expr: str) -> None:
        captured.extend(rows)

    monkeypatch.setattr(cli, "_render_find", capture)
    result = runner.invoke(cli.app, ["find", expr])
    assert result.exit_code == 0, result.output
    return sorted(r.entity_id for r in captured)


def _library_rows(expr: str, path: Path) -> list[str]:
    db = Database(path)
    rows = views.track_rows(db, TODAY, get_settings())
    db.close()
    return sorted(r.entity_id for r in query.filter_rows(query.parse(expr), rows))


QUERIES = [
    "",
    "dune",
    "kind:movie",
    "kind:movie is:watched",
    "director:villeneuve",
    "cast:villeneuve",
    'cast:"Josh O\'Connor"',
    "genre:horror",
    "tag:destiny",
    "theme:destiny",
    "genre:destiny",
    "-genre:horror",
    "year:2026",
    "year:2025..2026 kind:game",
    "is:available",
    "is:upcoming",
    "state:watching",
    "is:watched",
    "kind:tv cast:ritchson",
    "nosuchfield:x",
]


@pytest.mark.parametrize("expr", QUERIES)
def test_cli_find_matches_the_library_predicate(
    expr: str, seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _cli_rows(expr, monkeypatch) == _library_rows(expr, seeded)


def test_the_fixture_actually_discriminates(seeded: Path) -> None:
    """Guard against the parity test passing because everything returns nothing."""
    assert len(_library_rows("", seeded)) == 4
    assert len(_library_rows("kind:movie", seeded)) == 2
    assert len(_library_rows("director:villeneuve", seeded)) == 1
    assert _library_rows("cast:villeneuve", seeded) == []
    assert len(_library_rows("-genre:horror", seeded)) == 3


def test_bucket_filters_are_disjoint_and_exhaustive(seeded: Path) -> None:
    buckets = [_library_rows(f"is:{b}", seeded) for b in ("available", "upcoming", "watched")]
    flat = [rid for b in buckets for rid in b]
    assert sorted(flat) == _library_rows("", seeded)  # exhaustive
    assert len(flat) == len(set(flat))  # disjoint


def test_view_commands_accept_the_same_filter(seeded: Path) -> None:
    for command in ("upcoming", "available", "watched"):
        result = runner.invoke(cli.app, [command, "kind:movie"])
        assert result.exit_code == 0, result.output
