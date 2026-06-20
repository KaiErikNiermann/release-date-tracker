"""Tests for the UI-agnostic read models (upcoming / work_card / works_by_node)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from release_tracker import views
from release_tracker.config import Settings, get_settings
from release_tracker.db import Database
from release_tracker.models import (
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
    SourceTier,
    WorkRelation,
)


def _settings() -> Settings:
    return get_settings()


def _seed_work(
    db: Database,
    title: str,
    when: date,
    *,
    confirmed: bool = True,
    digital: date | None = None,
    digital_confirmed: bool = True,
    state: ConsumptionState = ConsumptionState.UNSET,
    fetched_at: datetime | None = None,
) -> Entity:
    ent = Entity.create(title, MediaKind.MOVIE, consumption_state=state)
    db.upsert_entity(ent)
    db.upsert_node(Node(id=ent.id, node_kind=NodeKind.WORK, name=title, owned=True))
    stamp = fetched_at or datetime.now(UTC)
    db.upsert_observation(
        ReleaseObservation(
            entity_id=ent.id,
            channel=ReleaseChannel.THEATRICAL,
            release_date=when,
            precision=DatePrecision.EXACT,
            certainty=Certainty.CONFIRMED if confirmed else Certainty.ESTIMATED,
            source_tier=SourceTier.AGGREGATOR,
            provider="tmdb",
            fetched_at=stamp,
        )
    )
    if digital is not None:
        db.upsert_observation(
            ReleaseObservation(
                entity_id=ent.id,
                channel=ReleaseChannel.DIGITAL,
                release_date=digital,
                precision=DatePrecision.EXACT,
                certainty=Certainty.CONFIRMED if digital_confirmed else Certainty.PREDICTED,
                source_tier=SourceTier.AGGREGATOR if digital_confirmed else SourceTier.MODEL,
                provider="tmdb" if digital_confirmed else "model",
                fetched_at=stamp,
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


def _seed_undated(db: Database, title: str, *, state: ConsumptionState) -> Entity:
    """A tracked work with no observations at all — not even enough to speculate a date."""
    ent = Entity.create(title, MediaKind.MOVIE, consumption_state=state)
    db.upsert_entity(ent)
    db.upsert_node(Node(id=ent.id, node_kind=NodeKind.WORK, name=title, owned=True))
    return ent


def test_upcoming_is_future_dated_then_no_date_tail(tmp_path: Path) -> None:
    db = Database(tmp_path / "v.db")
    today = date(2026, 6, 1)
    # confirmed-out (digital) + want -> available, NOT upcoming
    _seed_work(db, "Out", date(2025, 1, 1), digital=date(2025, 3, 1), state=ConsumptionState.WANT)
    _seed_work(db, "Soon Film", date(2026, 7, 1), state=ConsumptionState.WANT)
    _seed_work(db, "Later Film", date(2026, 12, 1), state=ConsumptionState.WANT)
    _seed_work(db, "Done", date(2026, 8, 1), state=ConsumptionState.WATCHED)  # finished -> hidden
    _seed_undated(db, "No Date Yet", state=ConsumptionState.WANT)  # -> the TBA tail, last
    rows = views.upcoming(db, today, _settings())
    # future-dated soonest-first, then the no-date tail; out/finished excluded
    assert [r.title for r in rows] == ["Soon Film", "Later Film", "No Date Yet"]
    assert rows[-1].pivot_when is None  # the tail row carries no date


def test_watched_view_lists_finished_newest_first(tmp_path: Path) -> None:
    db = Database(tmp_path / "v.db")
    today = date(2026, 6, 1)
    _seed_work(db, "Old Watch", date(2024, 1, 1), state=ConsumptionState.WATCHED)
    _seed_work(db, "New Watch", date(2025, 9, 1), state=ConsumptionState.WATCHED)
    _seed_work(db, "Dropped It", date(2025, 1, 1), state=ConsumptionState.DROPPED)
    _seed_work(db, "Still Want", date(2026, 9, 1), state=ConsumptionState.WANT)  # excluded
    rows = views.watched(db, today, _settings())
    assert [r.title for r in rows] == [
        "New Watch",
        "Dropped It",
        "Old Watch",
    ]  # newest release first


def test_no_limbo_every_active_row_lands_in_exactly_one_bucket(tmp_path: Path) -> None:
    # the partition is the whole point: nothing is "neither available nor upcoming".
    db = Database(tmp_path / "v.db")
    today = date(2026, 6, 1)
    _seed_work(db, "Out", date(2025, 1, 1), digital=date(2025, 3, 1), state=ConsumptionState.WANT)
    _seed_work(db, "Future", date(2026, 9, 1), state=ConsumptionState.WANT)
    _seed_undated(db, "Undated", state=ConsumptionState.WANT)
    _seed_work(db, "Finished", date(2025, 1, 1), state=ConsumptionState.WATCHED)
    s = _settings()
    buckets = {
        t: n
        for n, view in (
            ("avail", views.available),
            ("up", views.upcoming),
            ("watch", views.watched),
        )
        for t in [r.title for r in view(db, today, s)]
    }
    assert buckets == {"Out": "avail", "Future": "up", "Undated": "up", "Finished": "watch"}


def test_upcoming_days_window_and_kind_filter(tmp_path: Path) -> None:
    db = Database(tmp_path / "v.db")
    today = date(2026, 6, 1)
    _seed_work(db, "Within", date(2026, 6, 20))
    _seed_work(db, "Beyond", date(2026, 9, 1))
    assert [r.title for r in views.upcoming(db, today, _settings(), days=30)] == ["Within"]
    assert views.upcoming(db, today, _settings(), kind=MediaKind.GAME) == []


def test_upcoming_dual_dates_carry_stance(tmp_path: Path) -> None:
    db = Database(tmp_path / "v.db")
    today = date(2026, 6, 1)
    # confirmed theatrical + speculative (PREDICTED) digital, digital still ahead
    _seed_work(db, "Dune", date(2026, 2, 6), digital=date(2026, 7, 1), digital_confirmed=False)
    (row,) = views.upcoming(db, today, _settings())
    assert row.theatrical is not None and row.theatrical.confirmed is True
    assert row.digital is not None and row.digital.confirmed is False
    assert row.pivot_when == date(2026, 7, 1)  # digital governs (default channel)


def test_available_partition_by_state_and_pivot(tmp_path: Path) -> None:
    db = Database(tmp_path / "v.db")
    today = date(2026, 6, 1)
    # out (digital passed) + want -> available
    _seed_work(
        db, "Out & Want", date(2025, 1, 1), digital=date(2025, 3, 1), state=ConsumptionState.WANT
    )
    # out + watched -> hidden
    _seed_work(
        db, "Out & Done", date(2025, 1, 1), digital=date(2025, 3, 1), state=ConsumptionState.WATCHED
    )
    # upcoming digital + want -> not available (still upcoming)
    _seed_work(
        db, "Upcoming", date(2026, 7, 1), digital=date(2026, 9, 1), state=ConsumptionState.WANT
    )
    titles = [r.title for r in views.available(db, today, _settings())]
    assert titles == ["Out & Want"]


def _obs(
    entity_id: str, channel: ReleaseChannel, when: date, *, confirmed: bool
) -> ReleaseObservation:
    return ReleaseObservation(
        entity_id=entity_id,
        channel=channel,
        release_date=when,
        precision=DatePrecision.EXACT,
        certainty=Certainty.CONFIRMED if confirmed else Certainty.ESTIMATED,
        source_tier=SourceTier.AGGREGATOR if confirmed else SourceTier.MODEL,
        provider="tmdb" if confirmed else "model",
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_speculative_past_date_is_not_available(tmp_path: Path) -> None:
    # a speculative estimate that happens to be in the past must NOT mark a work
    # available — we don't actually know it released (the Steam-Frame/ONTOS bug).
    db = Database(tmp_path / "v.db")
    today = date(2026, 6, 19)
    ent = Entity.create("Speculative", MediaKind.GAME, consumption_state=ConsumptionState.WANT)
    db.upsert_entity(ent)
    db.upsert_node(Node(id=ent.id, node_kind=NodeKind.WORK, name=ent.title, owned=True))
    db.upsert_observation(_obs(ent.id, ReleaseChannel.DIGITAL, date(2026, 6, 18), confirmed=False))
    assert [r.title for r in views.available(db, today, _settings())] == []


def test_confirmed_date_preferred_over_earlier_speculative(tmp_path: Path) -> None:
    # ONTOS: a speculative early date + a confirmed later one. The confirmed date is
    # the pivot, so it's UPCOMING (not falsely available off the speculative past date).
    db = Database(tmp_path / "v.db")
    today = date(2026, 6, 19)
    ent = Entity.create("Ontos", MediaKind.GAME, consumption_state=ConsumptionState.WANT)
    db.upsert_entity(ent)
    db.upsert_node(Node(id=ent.id, node_kind=NodeKind.WORK, name=ent.title, owned=True))
    db.upsert_observation(_obs(ent.id, ReleaseChannel.DIGITAL, date(2026, 1, 1), confirmed=False))
    db.upsert_observation(_obs(ent.id, ReleaseChannel.DIGITAL, date(2026, 12, 31), confirmed=True))
    assert [r.title for r in views.available(db, today, _settings())] == []
    up = views.upcoming(db, today, _settings())
    assert [(r.title, r.pivot_when) for r in up] == [("Ontos", date(2026, 12, 31))]


def test_theatrical_release_is_not_digitally_available(tmp_path: Path) -> None:
    # digital preference: a film that's out in theaters but has no digital date must
    # NOT be "available" (you consume digitally, it isn't streamable/buyable yet).
    db = Database(tmp_path / "v.db")
    today = date(2026, 6, 20)
    _seed_work(db, "Theatrical Only", date(2026, 5, 1), state=ConsumptionState.WANT)  # no digital
    _seed_work(db, "Coming to Cinemas", date(2026, 12, 1), state=ConsumptionState.WANT)  # future
    _seed_work(
        db, "Now Digital", date(2026, 5, 1), digital=date(2026, 6, 5), state=ConsumptionState.WANT
    )
    # only the one with an elapsed, confirmed *digital* date is available.
    assert [r.title for r in views.available(db, today, _settings())] == ["Now Digital"]
    # the future theatrical film still surfaces in upcoming (display falls back across
    # channels) — strict availability did not make it vanish.
    assert "Coming to Cinemas" in {r.title for r in views.upcoming(db, today, _settings())}


def test_coarse_year_does_not_shadow_a_precise_month(tmp_path: Path) -> None:
    # the real ONTOS shape: a curated month (Oct) plus vague aggregator/store "2026"
    # years. The precise month must govern the pivot, not a year's Jan-1/Dec-31 artifact.
    db = Database(tmp_path / "v.db")
    today = date(2026, 6, 19)
    ent = Entity.create("Ontos", MediaKind.GAME, consumption_state=ConsumptionState.WANT)
    db.upsert_entity(ent)
    db.upsert_node(Node(id=ent.id, node_kind=NodeKind.WORK, name=ent.title, owned=True))

    def _o(channel: ReleaseChannel, when: date, precision: DatePrecision, tier: SourceTier) -> None:
        db.upsert_observation(
            ReleaseObservation(
                entity_id=ent.id,
                channel=channel,
                release_date=when,
                precision=precision,
                certainty=Certainty.ESTIMATED,
                source_tier=tier,
                provider=f"{channel.value}-{when.isoformat()}",
                fetched_at=datetime(2026, 6, 19, tzinfo=UTC),
            )
        )

    _o(ReleaseChannel.PRIMARY, date(2026, 10, 6), DatePrecision.MONTH, SourceTier.RUMOR)
    _o(ReleaseChannel.PRIMARY, date(2026, 1, 1), DatePrecision.YEAR, SourceTier.AGGREGATOR)
    _o(ReleaseChannel.STEAM, date(2026, 1, 1), DatePrecision.YEAR, SourceTier.FIRST_PARTY_STORE)

    assert [r.title for r in views.available(db, today, _settings())] == []
    up = views.upcoming(db, today, _settings())
    assert [(r.title, r.pivot_when) for r in up] == [("Ontos", date(2026, 10, 6))]


def test_freshness_buckets(tmp_path: Path) -> None:
    s = _settings()
    today = date(2026, 6, 1)
    base = datetime(2026, 6, 1, tzinfo=UTC)
    assert views.freshness(base - timedelta(days=max(s.fresh_days - 1, 0)), today, s) == "fresh"
    assert views.freshness(base - timedelta(days=s.fresh_days + 1), today, s) == "aging"
    assert views.freshness(base - timedelta(days=s.stale_days + 1), today, s) == "stale"
    assert views.freshness(None, today, s) is None


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
    row = views.upcoming(db, today, _settings())[0]
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


def test_seasons_order_by_part_within_a_season(tmp_path: Path) -> None:
    # a mid-season split: Part 2 must sort after Part 1 of the same season
    db = Database(tmp_path / "v.db")
    show = Node.create(NodeKind.SERIES, "Stranger Things", source="tmdb", source_id="66732")
    db.upsert_node(show)
    for title, part, when in (
        ("Stranger Things: Season 5, Part 2", 2, date(2026, 12, 25)),  # out of order
        ("Stranger Things: Season 5, Part 1", 1, date(2026, 11, 26)),
    ):
        work = _seed_work(db, title, when)
        db.upsert_edge(
            Edge(
                src_id=work.id,
                dst_id=show.id,
                relation=RelationKind.PART_OF_SERIES,
                ordinal=5,
                part=part,
                source_provider="tmdb",
                source_tier=SourceTier.AGGREGATOR,
            )
        )
    stored = db.get_node(show.id)
    assert stored is not None
    entries = views.seasons_of_series(db, stored)
    assert [(e.season, e.part) for e in entries] == [(5, 1), (5, 2)]


def test_derived_from_and_derivatives_walk(tmp_path: Path) -> None:
    db = Database(tmp_path / "v.db")
    parent = _seed_work(db, "Arcane", date(2021, 11, 6))
    spinoff = _seed_work(db, "Arcane: Noxus", date(2027, 1, 1))
    db.upsert_edge(
        Edge(
            src_id=spinoff.id,
            dst_id=parent.id,
            relation=RelationKind.DERIVED_FROM,
            role=WorkRelation.SPINOFF,
            source_provider="user",
            source_tier=SourceTier.OFFICIAL,
            owned=True,
        )
    )
    up = views.derived_from(db, spinoff.id)
    assert [(r.node.name, r.relation) for r in up] == [("Arcane", WorkRelation.SPINOFF)]
    down = views.derivatives_of(db, parent.id)
    assert [(r.node.name, r.relation) for r in down] == [("Arcane: Noxus", WorkRelation.SPINOFF)]
    # and the parent's card surfaces its derivative
    card = views.work_card(db, parent)
    assert [r.node.name for r in card.derivatives] == ["Arcane: Noxus"]
    assert views.work_card(db, spinoff).derived_from[0].node.name == "Arcane"


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


def test_region_gating_blocks_a_non_profile_only_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "v.db")
    today = date(2026, 6, 1)
    ent = Entity.create("RegionLocked", MediaKind.MOVIE, consumption_state=ConsumptionState.WANT)
    db.upsert_entity(ent)
    db.upsert_node(Node(id=ent.id, node_kind=NodeKind.WORK, name=ent.title, owned=True))
    # a confirmed, elapsed digital release — but only in RO (Romania)
    db.upsert_observation(
        ReleaseObservation(
            entity_id=ent.id,
            channel=ReleaseChannel.DIGITAL,
            region="RO",
            release_date=date(2025, 1, 1),
            precision=DatePrecision.EXACT,
            certainty=Certainty.CONFIRMED,
            source_tier=SourceTier.AGGREGATOR,
            provider="tmdb",
            fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    # US profile: NOT available (no RO in profile) -> flagged in upcoming, not hidden
    monkeypatch.setenv("RDT_REGIONS", "US,DE,GB")
    us = Settings()
    assert [r.title for r in views.available(db, today, us)] == []
    blocked = [r for r in views.upcoming(db, today, us) if r.title == "RegionLocked"]
    assert blocked and blocked[0].blockers == ("not available for your profile",)
    # RO profile: available
    monkeypatch.setenv("RDT_REGIONS", "RO")
    assert [r.title for r in views.available(db, today, Settings())] == ["RegionLocked"]


def _released_game(db: Database, title: str) -> Entity:
    ent = Entity.create(title, MediaKind.GAME, consumption_state=ConsumptionState.WANT)
    db.upsert_entity(ent)
    db.upsert_node(Node(id=ent.id, node_kind=NodeKind.WORK, name=title, owned=True))
    db.upsert_observation(
        ReleaseObservation(
            entity_id=ent.id,
            channel=ReleaseChannel.PRIMARY,
            region="WW",
            release_date=date(2025, 1, 1),
            precision=DatePrecision.EXACT,
            certainty=Certainty.CONFIRMED,
            source_tier=SourceTier.FIRST_PARTY_STORE,
            provider="igdb",
        )
    )
    return ent


def test_shared_condition_blocks_then_unblocks_every_dependent(tmp_path: Path) -> None:
    db = Database(tmp_path / "v.db")
    today = date(2026, 6, 1)
    cond = Node.create(NodeKind.CONDITION, "EAC Linux", owned=True)
    db.upsert_node(cond)
    for title in ("GameA", "GameB"):
        ent = _released_game(db, title)
        db.upsert_edge(
            Edge(
                src_id=ent.id,
                dst_id=cond.id,
                relation=RelationKind.BLOCKED_BY,
                source_provider="user",
                source_tier=SourceTier.OFFICIAL,
                owned=True,
            )
        )
    s = _settings()
    # pending shared condition -> both released games are NOT available, flagged in upcoming
    assert views.available(db, today, s) == []
    up = {r.title: r.blockers for r in views.upcoming(db, today, s)}
    assert up == {"GameA": ("EAC Linux",), "GameB": ("EAC Linux",)}
    # resolve the ONE shared condition -> BOTH unblock (the graph payoff)
    db.upsert_condition(
        Condition(node_id=cond.id, status="resolved", resolve_date=date(2026, 3, 1))
    )
    assert sorted(r.title for r in views.available(db, today, s)) == ["GameA", "GameB"]


def test_never_condition_keeps_a_work_permanently_unavailable(tmp_path: Path) -> None:
    db = Database(tmp_path / "v.db")
    today = date(2026, 6, 1)
    ent = _released_game(db, "VanguardLocked")
    cond = Node.create(NodeKind.CONDITION, "Vanguard Linux", owned=True)
    db.upsert_node(cond)
    db.upsert_condition(Condition(node_id=cond.id, status="never"))
    db.upsert_edge(
        Edge(
            src_id=ent.id,
            dst_id=cond.id,
            relation=RelationKind.BLOCKED_BY,
            source_provider="user",
            source_tier=SourceTier.OFFICIAL,
            owned=True,
        )
    )
    s = _settings()
    assert views.available(db, today, s) == []  # never -> never available
    (row,) = views.upcoming(db, today, s)
    assert row.blockers == ("Vanguard Linux",)
