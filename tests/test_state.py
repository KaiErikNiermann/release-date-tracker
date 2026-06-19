"""Tests for consumption state (seed mapping + round-trip) and the notes log."""

from __future__ import annotations

from pathlib import Path

from release_tracker.db import Database
from release_tracker.models import ConsumptionState, Entity, MediaKind
from release_tracker.seed.base import parse_consumption_state


def test_notion_status_maps_to_state() -> None:
    assert parse_consumption_state("Watched/Played") is ConsumptionState.WATCHED
    assert parse_consumption_state("Currently Watching/Playing") is ConsumptionState.WATCHING
    assert parse_consumption_state("to Watch") is ConsumptionState.WANT
    assert parse_consumption_state("Want to Watch/Play") is ConsumptionState.WANT
    assert parse_consumption_state(None) is ConsumptionState.UNSET
    assert parse_consumption_state("Not started") is ConsumptionState.UNSET


def test_state_seeds_and_survives_stateless_pull(tmp_path: Path) -> None:
    db = Database(tmp_path / "s.db")
    ent = Entity.create("X", MediaKind.MOVIE, consumption_state=ConsumptionState.WATCHING)
    db.upsert_entity(ent)
    assert db.get_entity(ent.id).consumption_state is ConsumptionState.WATCHING  # type: ignore[union-attr]
    # a stateless pull (unset) must not clobber the known state
    db.upsert_entity(Entity.create("X", MediaKind.MOVIE))
    assert db.get_entity(ent.id).consumption_state is ConsumptionState.WATCHING  # type: ignore[union-attr]


def test_set_consumption_state_overrides(tmp_path: Path) -> None:
    db = Database(tmp_path / "s.db")
    ent = Entity.create("X", MediaKind.MOVIE, consumption_state=ConsumptionState.WANT)
    db.upsert_entity(ent)
    assert db.set_consumption_state(ent.id, ConsumptionState.WATCHED) is True
    assert db.get_entity(ent.id).consumption_state is ConsumptionState.WATCHED  # type: ignore[union-attr]
    # clearing back to UNSET is allowed via the direct setter
    db.set_consumption_state(ent.id, ConsumptionState.UNSET)
    assert db.get_entity(ent.id).consumption_state is ConsumptionState.UNSET  # type: ignore[union-attr]
    assert db.set_consumption_state("nonexistent", ConsumptionState.WANT) is False


def test_notes_log_roundtrip(tmp_path: Path) -> None:
    db = Database(tmp_path / "s.db")
    ent = Entity.create("X", MediaKind.MOVIE)
    db.upsert_entity(ent)
    assert db.note_counts() == {}
    db.add_note(ent.id, "production halted")
    db.add_note(ent.id, "resumed, target 2027")
    notes = db.iter_notes(ent.id)
    assert [body for _, body in notes] == [
        "resumed, target 2027",
        "production halted",
    ]  # newest first
    assert db.note_counts()[ent.id] == 2
