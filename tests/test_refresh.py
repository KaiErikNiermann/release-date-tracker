"""Tests for the batch date-refresh surface: target selection, the before/after diff,
collision-safe capture (dedupe by canonical id), and JustWatch persistence.

Everything here is deterministic against a temp db — the live re-pull that `refresh_entities`
performs is network I/O, so we test the pieces around it (selection, persistence, diff, dedup),
not the HTTP fan-out itself.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from release_tracker import cli, views
from release_tracker.config import Settings, get_settings
from release_tracker.db import Database
from release_tracker.lookup import RdReport
from release_tracker.models import (
    BestEstimate,
    Certainty,
    ConsumptionState,
    DatePrecision,
    Entity,
    MediaKind,
    Node,
    NodeKind,
    ReleaseChannel,
    ReleaseObservation,
    SourceTier,
)
from release_tracker.pipeline import persist_availability
from release_tracker.sources import API_PROVIDERS
from release_tracker.sources.justwatch import JustWatchAvailability


def _settings() -> Settings:
    return get_settings()


def _seed(
    db: Database,
    title: str,
    when: date,
    *,
    kind: MediaKind = MediaKind.MOVIE,
    state: ConsumptionState = ConsumptionState.WANT,
) -> Entity:
    ent = Entity.create(title, kind, consumption_state=state)
    db.upsert_entity(ent)
    db.upsert_node(Node(id=ent.id, node_kind=NodeKind.WORK, name=title, owned=True))
    db.upsert_observation(
        ReleaseObservation(
            entity_id=ent.id,
            channel=ReleaseChannel.THEATRICAL,
            release_date=when,
            precision=DatePrecision.EXACT,
            certainty=Certainty.CONFIRMED,
            source_tier=SourceTier.AGGREGATOR,
            provider="tmdb",
            fetched_at=datetime.now(UTC),
        )
    )
    return ent


# --- find_entity_by_external_id ----------------------------------------------
def test_find_entity_by_external_id(tmp_path: Path) -> None:
    db = Database(tmp_path / "f.db")
    e = Entity.create("X", MediaKind.MOVIE, external_ids={"tmdb": "42"})
    db.upsert_entity(e)
    found = db.find_entity_by_external_id("tmdb", "42")
    assert found is not None and found.id == e.id
    assert db.find_entity_by_external_id("tmdb", "99") is None
    assert db.find_entity_by_external_id("igdb", "42") is None  # right value, wrong key


# --- collision-safe capture (the duplicate-entity regression) ----------------
def test_capture_entity_dedupes_by_external_id(tmp_path: Path) -> None:
    db = Database(tmp_path / "c.db")
    existing = Entity.create(
        "Odyssey",
        MediaKind.MOVIE,
        external_ids={"tmdb": "1368337"},
        consumption_state=ConsumptionState.WATCHED,
    )
    db.upsert_entity(existing)
    db.upsert_node(Node(id=existing.id, node_kind=NodeKind.WORK, name="Odyssey", owned=True))
    # re-capture under a DIFFERENT typed title but the same tmdb id
    report = RdReport(
        query="The Odyssey",
        found=True,
        kind=MediaKind.MOVIE,
        matched_title="The Odyssey",
        canonical={"tmdb": "1368337"},
    )
    ent = cli._capture_entity(db, "The Odyssey", report, None)  # pyright: ignore[reportPrivateUsage]
    assert ent.id == existing.id  # same entity — no duplicate slug minted
    assert ent.external_ids["tmdb"] == "1368337"
    assert ent.consumption_state is ConsumptionState.WATCHED  # existing state preserved


def test_capture_entity_new_uses_canonical_title(tmp_path: Path) -> None:
    db = Database(tmp_path / "c2.db")
    report = RdReport(
        query="the end of oak street",
        found=True,
        kind=MediaKind.MOVIE,
        matched_title="The End of Oak Street",
        canonical={"tmdb": "1101383"},
    )
    ent = cli._capture_entity(db, "the end of oak street", report, None)  # pyright: ignore[reportPrivateUsage]
    assert ent.title == "The End of Oak Street"  # canonical title, not the typed one
    assert ent.consumption_state is ConsumptionState.WANT


# --- JustWatch persistence ---------------------------------------------------
def test_persist_availability_writes_digital_and_survives_pull(tmp_path: Path) -> None:
    db = Database(tmp_path / "j.db")
    ent = Entity.create("Mando", MediaKind.MOVIE, external_ids={"tmdb": "1228710"})
    db.upsert_entity(ent)
    avail = JustWatchAvailability(
        object_id=1,
        title="Mando",
        year=2026,
        offers=(),
        earliest_vod=date(2026, 5, 20),
        earliest_vod_country="DE",
        earliest_vod_platform="Apple TV Store",
    )
    assert persist_availability(db, ent, avail) == 1
    jw = [o for o in db.iter_observations(ent.id) if o.provider == "justwatch"]
    assert len(jw) == 1
    assert jw[0].channel is ReleaseChannel.DIGITAL
    assert jw[0].release_date == date(2026, 5, 20)
    assert jw[0].certainty is Certainty.CONFIRMED
    # a subsequent Tier-0 pull clears only API providers -> the justwatch row survives
    db.delete_observations(ent.id, API_PROVIDERS)
    assert [o for o in db.iter_observations(ent.id) if o.provider == "justwatch"]


def test_persist_availability_replaces_prior_row(tmp_path: Path) -> None:
    db = Database(tmp_path / "j2.db")
    ent = Entity.create("Film", MediaKind.MOVIE, external_ids={"tmdb": "1"})
    db.upsert_entity(ent)
    persist_availability(
        db,
        ent,
        JustWatchAvailability(1, "Film", 2026, (), date(2026, 6, 1), "US", "Apple TV"),
    )
    persist_availability(
        db,
        ent,
        JustWatchAvailability(1, "Film", 2026, (), date(2026, 5, 1), "DE", "Amazon"),
    )
    jw = [o for o in db.iter_observations(ent.id) if o.provider == "justwatch"]
    assert len(jw) == 1 and jw[0].release_date == date(2026, 5, 1)  # replaced, not duplicated


def test_persist_availability_no_vod_writes_nothing(tmp_path: Path) -> None:
    db = Database(tmp_path / "j3.db")
    ent = Entity.create("Film", MediaKind.MOVIE, external_ids={"tmdb": "1"})
    db.upsert_entity(ent)
    avail = JustWatchAvailability(1, "Film", 2026, (), None, None, None)
    assert persist_availability(db, ent, avail) == 0


# --- refresh_targets selection -----------------------------------------------
def test_refresh_targets_kind_filter(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    _seed(db, "AMovie", date(2026, 8, 1), kind=MediaKind.MOVIE)
    _seed(db, "AGame", date(2026, 8, 1), kind=MediaKind.GAME)
    targets = views.refresh_targets(db, date(2026, 1, 1), _settings(), kind=MediaKind.MOVIE)
    assert [e.title for e in targets] == ["AMovie"]


def test_refresh_targets_state_filter(tmp_path: Path) -> None:
    db = Database(tmp_path / "t2.db")
    _seed(db, "Want", date(2026, 8, 1), state=ConsumptionState.WANT)
    _seed(db, "Done", date(2026, 8, 1), state=ConsumptionState.WATCHED)
    targets = views.refresh_targets(db, date(2026, 1, 1), _settings(), state=ConsumptionState.WANT)
    assert [e.title for e in targets] == ["Want"]


def test_refresh_targets_days_window(tmp_path: Path) -> None:
    db = Database(tmp_path / "t3.db")
    today = date(2026, 6, 1)
    _seed(db, "Soon", date(2026, 6, 20))
    _seed(db, "Far", date(2026, 12, 1))
    targets = views.refresh_targets(db, today, _settings(), days=30)
    assert [e.title for e in targets] == ["Soon"]


def test_refresh_targets_since_until(tmp_path: Path) -> None:
    db = Database(tmp_path / "t4.db")
    _seed(db, "Jul", date(2026, 7, 15))
    _seed(db, "Sep", date(2026, 9, 15))
    _seed(db, "Nov", date(2026, 11, 15))
    targets = views.refresh_targets(
        db, date(2026, 1, 1), _settings(), since=date(2026, 8, 1), until=date(2026, 10, 1)
    )
    assert [e.title for e in targets] == ["Sep"]


def test_refresh_targets_no_filter_returns_all(tmp_path: Path) -> None:
    db = Database(tmp_path / "t5.db")
    _seed(db, "One", date(2026, 8, 1))
    _seed(db, "Two", date(2026, 9, 1))
    assert len(views.refresh_targets(db, date(2026, 1, 1), _settings())) == 2


# --- _resolve_refs dedupe ----------------------------------------------------
def test_resolve_refs_dedupes_and_skips_missing(tmp_path: Path) -> None:
    db = Database(tmp_path / "r.db")
    alpha = _seed(db, "Alpha", date(2026, 8, 1))
    _seed(db, "Beta", date(2026, 8, 1))
    got = cli._resolve_refs(db, ["Alpha", alpha.id, "Nope"])  # pyright: ignore[reportPrivateUsage]
    assert {e.id for e in got} == {alpha.id}  # title + id collapse to one; missing dropped


# --- diff_estimates ----------------------------------------------------------
def _est(
    channel: ReleaseChannel, when: date, region: str = "US", *, confirmed: bool = False
) -> BestEstimate:
    return BestEstimate(
        entity_id="e",
        channel=channel,
        region=region,
        release_date=when,
        certainty=Certainty.CONFIRMED if confirmed else Certainty.PREDICTED,
    )


def test_diff_estimates_reports_moved_channel() -> None:
    before = [_est(ReleaseChannel.DIGITAL, date(2026, 8, 6))]
    after = [_est(ReleaseChannel.DIGITAL, date(2026, 8, 10))]
    diffs = views.diff_estimates(before, after)
    assert len(diffs) == 1
    assert diffs[0].old == date(2026, 8, 6) and diffs[0].new == date(2026, 8, 10)


def test_diff_estimates_no_change_is_empty() -> None:
    est = [_est(ReleaseChannel.DIGITAL, date(2026, 8, 6))]
    assert views.diff_estimates(est, est) == []


def test_diff_estimates_flags_new_confirmed() -> None:
    before = [_est(ReleaseChannel.DIGITAL, date(2026, 8, 12))]
    after = [_est(ReleaseChannel.DIGITAL, date(2026, 5, 20), region="DE", confirmed=True)]
    d = views.diff_estimates(before, after)[0]
    assert d.new == date(2026, 5, 20) and d.new_confirmed is True
