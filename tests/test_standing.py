"""Tests for the consumption partition once a work can be shelved.

``bucket_of`` is the one classifier the CLI views, the query language's ``is:`` field and
the TUI all read, so its two invariants — total and single-valued — are what keep those
three from disagreeing. They are asserted here by exhausting the input space rather than by
example, because a fourth bucket is exactly the kind of change that quietly puts a hole in a
partition that used to be complete.

The tail of the file covers the other half: why the stance is a persisted column rather than
a recomputed note, and the asymmetry between the upsert (which coalesces) and ``set_stance``
(which overwrites) that lets a shelved work come back.
"""

from __future__ import annotations

import itertools
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from release_tracker.contingency import Resolution, ResolutionStatus
from release_tracker.db import Database
from release_tracker.models import Bucket, ConsumptionState, Entity, MediaKind, Stance
from release_tracker.views import bucket_of

TODAY = date(2026, 9, 1)

_OUT = Resolution(ResolutionStatus.RESOLVED, date(2025, 1, 1))
_LATER = Resolution(ResolutionStatus.RESOLVED, date(2027, 1, 1))
_SOFT = Resolution(ResolutionStatus.PENDING, None)  # a date we do not trust yet
_NEVER = Resolution(ResolutionStatus.NEVER, None, "not in your region")

_RESOLUTIONS = (_OUT, _LATER, _SOFT, _NEVER)
_STANCES = (None, *Stance)


# --- the invariants, over the whole input space ------------------------------------------
def test_every_combination_lands_in_exactly_one_bucket() -> None:
    """Total and single-valued. A `StrEnum` return type gives the second for free, so what
    this really pins is the first: no input falls through to an implicit None."""
    for state, available, stance in itertools.product(ConsumptionState, _RESOLUTIONS, _STANCES):
        assert isinstance(bucket_of(state, available, TODAY, stance), Bucket)


def test_every_bucket_is_reachable() -> None:
    """An unreachable bucket is a partition with a dead branch — which is how `shelved`
    would silently fail to do anything at all."""
    reached = {
        bucket_of(state, available, TODAY, stance)
        for state, available, stance in itertools.product(ConsumptionState, _RESOLUTIONS, _STANCES)
    }
    assert reached == set(Bucket)


# --- the ordering, which is the whole design ----------------------------------------------
def test_what_you_did_with_it_outranks_what_happened_to_it() -> None:
    """A cancelled game you already played is watched, not shelved. The stance describes the
    production; the state describes you, and you are the one asking."""
    for state in (ConsumptionState.WATCHED, ConsumptionState.DROPPED, ConsumptionState.SKIPPED):
        assert bucket_of(state, _SOFT, TODAY, Stance.SHELVED) is Bucket.WATCHED


def test_a_shelved_work_leaves_the_upcoming_queue() -> None:
    """The regression this bucket exists for: before it, a cancelled work had no date that
    could ever move it, so it sat in `upcoming` forever."""
    for state in (ConsumptionState.WANT, ConsumptionState.WATCHING, ConsumptionState.UNSET):
        assert bucket_of(state, _SOFT, TODAY, Stance.SHELVED) is Bucket.SHELVED
        assert bucket_of(state, _SOFT, TODAY, None) is Bucket.UPCOMING


def test_shelved_outranks_an_elapsed_date() -> None:
    """A shelved work can still carry an old announced date. Believing the date over the
    source's own "it was cancelled" would file a film that never came out as available."""
    assert bucket_of(ConsumptionState.WANT, _OUT, TODAY, Stance.SHELVED) is Bucket.SHELVED
    assert bucket_of(ConsumptionState.WANT, _OUT, TODAY, None) is Bucket.AVAILABLE


# --- what must *not* shelve ----------------------------------------------------------------
@pytest.mark.parametrize(
    "stance", [None, Stance.RELEASED, Stance.COMING, Stance.UNCERTAIN, Stance.FINISHED]
)
def test_only_shelved_shelves(stance: Stance | None) -> None:
    """`UNKNOWN` is absent from this list on purpose — it is covered on its own below."""
    assert bucket_of(ConsumptionState.WANT, _SOFT, TODAY, stance) is not Bucket.SHELVED


def test_an_unrecognised_word_never_shelves() -> None:
    """Fail open, as `seasons.stance_of` does. A status word we do not know is not evidence
    of anything, and the cost of guessing wrong here is a row that vanishes from the queue."""
    assert bucket_of(ConsumptionState.WANT, _SOFT, TODAY, Stance.UNKNOWN) is Bucket.UPCOMING


def test_a_series_that_ended_is_not_shelved() -> None:
    """`FINISHED` means it ran and stopped; its episodes released and its rows are ordinary.
    Only a work that will never arrive is shelved."""
    assert bucket_of(ConsumptionState.WANT, _OUT, TODAY, Stance.FINISHED) is Bucket.AVAILABLE


# --- the old behaviour, unchanged where no stance is known ---------------------------------
@pytest.mark.parametrize(
    ("state", "available", "want"),
    [
        (ConsumptionState.WANT, _OUT, Bucket.AVAILABLE),
        (ConsumptionState.WATCHING, _OUT, Bucket.AVAILABLE),
        (ConsumptionState.UNSET, _OUT, Bucket.UPCOMING),  # no stated intent: never available
        (ConsumptionState.WANT, _LATER, Bucket.UPCOMING),
        (ConsumptionState.WANT, _SOFT, Bucket.UPCOMING),
        (ConsumptionState.WANT, _NEVER, Bucket.UPCOMING),
        (ConsumptionState.WATCHED, _OUT, Bucket.WATCHED),
    ],
)
def test_the_partition_is_unchanged_without_a_stance(
    state: ConsumptionState, available: Resolution, want: Bucket
) -> None:
    """Every existing row has `stance = None`, so the default must classify as it always did."""
    assert bucket_of(state, available, TODAY) is want
    assert bucket_of(state, available, TODAY, None) is want


# --- the TV projection ----------------------------------------------------------------------
def test_the_tv_stance_maps_onto_the_shared_vocabulary() -> None:
    """`ShowStance` asks a finer question ("is season N+1 coming") than the persisted word.
    Nothing TMDB says about a series shelves it — a finished show still released."""
    from release_tracker.seasons import ShowStance

    assert ShowStance.FINISHED.stance is Stance.FINISHED
    assert ShowStance.CONFIRMED_NEXT.stance is Stance.COMING
    assert ShowStance.UNCERTAIN.stance is Stance.UNCERTAIN
    assert ShowStance.UNKNOWN.stance is Stance.UNKNOWN
    assert all(s.stance is not Stance.SHELVED for s in ShowStance)


# --- persistence: the reason this is a column and not a computed note ------------------------
def test_a_stateless_capture_cannot_erase_a_stance(tmp_path: Path) -> None:
    """`upsert_entity` COALESCEs it, as it does the coords: a capture that asked no source
    leaves `stance=None`, and that silence must not read as "it is fine now"."""
    db = Database(tmp_path / "s.db")
    entity = Entity.create("Scalebound", MediaKind.GAME)
    db.upsert_entity(entity)
    db.set_stance(entity.id, Stance.SHELVED)

    db.upsert_entity(entity)  # a re-capture, still carrying stance=None
    stored = db.get_entity(entity.id)
    assert stored is not None
    assert stored.stance is Stance.SHELVED
    db.close()


def test_a_pull_can_take_a_stance_back(tmp_path: Path) -> None:
    """The other half, and why `set_stance` overwrites where the upsert coalesces. Silksong
    spent years looking dead; a source that later says otherwise has to be able to win."""
    db = Database(tmp_path / "s.db")
    entity = Entity.create("Hollow Knight: Silksong", MediaKind.GAME)
    db.upsert_entity(entity)
    db.set_stance(entity.id, Stance.SHELVED)

    db.set_stance(entity.id, Stance.RELEASED)
    stored = db.get_entity(entity.id)
    assert stored is not None
    assert stored.stance is Stance.RELEASED

    db.set_stance(entity.id, None)  # and back to "nobody has said"
    stored = db.get_entity(entity.id)
    assert stored is not None
    assert stored.stance is None
    db.close()


def test_migration_adds_the_column_to_a_pre_stance_db(tmp_path: Path) -> None:
    """Opening an older db must add `stance` and leave every existing row intact."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE entities (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL,
            aliases TEXT NOT NULL DEFAULT '[]', external_ids TEXT NOT NULL DEFAULT '{}',
            notion_page_id TEXT, notes TEXT, watch INTEGER NOT NULL DEFAULT 1,
            consumption_state TEXT NOT NULL DEFAULT 'unset',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        INSERT INTO entities (id, title, kind, created_at, updated_at)
        VALUES ('game-x-abc','Scalebound','game','2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    db = Database(path)
    cols = {r[1] for r in db._conn.execute("PRAGMA table_info(entities)")}  # pyright: ignore[reportPrivateUsage]
    assert "stance" in cols
    survivor = db.get_entity("game-x-abc")
    assert survivor is not None
    assert survivor.title == "Scalebound"
    assert survivor.stance is None  # nobody has been asked yet, which is not UNKNOWN
    db.close()
