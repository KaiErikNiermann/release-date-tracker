"""Tests for the UI-agnostic read models (upcoming / work_card / works_by_node)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from release_tracker import views
from release_tracker.db import Database
from release_tracker.models import (
    Certainty,
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


def _seed_work(db: Database, title: str, when: date, *, confirmed: bool = True) -> Entity:
    ent = Entity.create(title, MediaKind.MOVIE)
    db.upsert_entity(ent)
    db.upsert_node(Node(id=ent.id, node_kind=NodeKind.WORK, name=title, owned=True))
    db.upsert_observation(
        ReleaseObservation(
            entity_id=ent.id,
            channel=ReleaseChannel.THEATRICAL,
            release_date=when,
            precision=DatePrecision.EXACT,
            certainty=Certainty.CONFIRMED if confirmed else Certainty.ESTIMATED,
            source_tier=SourceTier.AGGREGATOR,
            provider="tmdb",
            fetched_at=datetime.now(UTC),
        )
    )
    return ent


def _credit(db: Database, work: Entity, person: Node, role: CreditRole) -> None:
    db.upsert_node(person)
    db.upsert_edge(
        Edge(
            src_id=person.id,
            dst_id=work.id,
            relation=RelationKind.CREDITED_ON,
            role=role,
            source_provider="tmdb",
            source_tier=SourceTier.AGGREGATOR,
        )
    )


def test_upcoming_is_future_only_and_date_sorted(tmp_path: Path) -> None:
    db = Database(tmp_path / "v.db")
    today = date(2026, 6, 1)
    _seed_work(db, "Past Film", date(2026, 1, 1))
    _seed_work(db, "Soon Film", date(2026, 7, 1))
    _seed_work(db, "Later Film", date(2026, 12, 1))
    rows = views.upcoming(db, today)
    assert [r.title for r in rows] == ["Soon Film", "Later Film"]  # past dropped, sorted


def test_upcoming_days_window_and_kind_filter(tmp_path: Path) -> None:
    db = Database(tmp_path / "v.db")
    today = date(2026, 6, 1)
    _seed_work(db, "Within", date(2026, 6, 20))
    _seed_work(db, "Beyond", date(2026, 9, 1))
    assert [r.title for r in views.upcoming(db, today, days=30)] == ["Within"]
    assert views.upcoming(db, today, kind=MediaKind.GAME) == []


def test_upcoming_surfaces_who_and_flagged_themes(tmp_path: Path) -> None:
    db = Database(tmp_path / "v.db")
    today = date(2026, 6, 1)
    work = _seed_work(db, "Dune", date(2026, 7, 1))
    _credit(
        db,
        work,
        Node.create(NodeKind.PERSON, "Denis Villeneuve", source="tmdb", source_id="1"),
        CreditRole.DIRECTOR,
    )
    # a sourced genre and a model theme
    genre = Node.create(NodeKind.DESCRIPTOR, "Sci-Fi", descriptor_kind=DescriptorKind.GENRE)
    theme = Node.create(NodeKind.DESCRIPTOR, "destiny", descriptor_kind=DescriptorKind.THEME)
    db.upsert_node(genre)
    db.upsert_node(theme)
    db.upsert_edge(
        Edge(
            src_id=work.id,
            dst_id=genre.id,
            relation=RelationKind.EXHIBITS,
            source_provider="tmdb",
            source_tier=SourceTier.AGGREGATOR,
        )
    )
    db.upsert_edge(
        Edge(
            src_id=work.id,
            dst_id=theme.id,
            relation=RelationKind.EXHIBITS,
            source_provider="openai",
            source_tier=SourceTier.MODEL,
        )
    )
    row = views.upcoming(db, today)[0]
    assert row.who == ("Denis Villeneuve",)
    # sourced genre first, flagged theme after
    assert [(t.name, t.predicted) for t in row.what] == [("Sci-Fi", False), ("destiny", True)]


def test_works_by_node_one_hop(tmp_path: Path) -> None:
    db = Database(tmp_path / "v.db")
    person = Node.create(NodeKind.PERSON, "Denis Villeneuve", source="tmdb", source_id="1")
    for title in ("Dune", "Arrival"):
        w = _seed_work(db, title, date(2026, 7, 1))
        _credit(db, w, person, CreditRole.DIRECTOR)
    works = views.works_by_node(db, db.get_node(person.id))  # type: ignore[arg-type]
    assert sorted(w.entity.title for w in works) == ["Arrival", "Dune"]
    assert all(w.owned for w in works)  # the works themselves are user-owned


def test_seasons_of_series_orders_by_ordinal(tmp_path: Path) -> None:
    db = Database(tmp_path / "v.db")
    show = Node.create(NodeKind.SERIES, "Severance", source="tmdb", source_id="95396")
    db.upsert_node(show)
    for title, season, when in (
        ("Severance: Season 3", 3, date(2027, 1, 1)),  # inserted out of order
        ("Severance: Season 2", 2, date(2025, 1, 17)),
    ):
        work = _seed_work(db, title, when)
        db.upsert_edge(
            Edge(
                src_id=work.id,
                dst_id=show.id,
                relation=RelationKind.PART_OF_SERIES,
                ordinal=season,
                source_provider="tmdb",
                source_tier=SourceTier.AGGREGATOR,
            )
        )
    stored = db.get_node(show.id)
    assert stored is not None
    entries = views.seasons_of_series(db, stored)
    assert [(e.season, e.entity.title) for e in entries] == [
        (2, "Severance: Season 2"),
        (3, "Severance: Season 3"),
    ]
    assert entries[0].when == date(2025, 1, 17)  # ordinal + date survive the round-trip
    # and the work card exposes the season number
    card = views.work_card(db, entries[1].entity)
    assert card.season == 3
    assert card.series == ("Severance",)


def test_work_card_groups_who_where_what(tmp_path: Path) -> None:
    db = Database(tmp_path / "v.db")
    work = _seed_work(db, "Dune", date(2026, 7, 1))
    _credit(
        db,
        work,
        Node.create(NodeKind.PERSON, "Hans Zimmer", source="tmdb", source_id="9"),
        CreditRole.COMPOSER,
    )
    plat = Node.create(NodeKind.PLATFORM, "HBO Max")
    db.upsert_node(plat)
    db.upsert_edge(
        Edge(
            src_id=work.id,
            dst_id=plat.id,
            relation=RelationKind.AVAILABLE_ON,
            source_provider="tmdb",
            source_tier=SourceTier.AGGREGATOR,
        )
    )
    card = views.work_card(db, work)
    assert card.credits[0].name == "Hans Zimmer"
    assert card.platforms[0].name == "HBO Max"
    assert card.platforms[0].predicted is False
    assert len(card.estimates) >= 1
