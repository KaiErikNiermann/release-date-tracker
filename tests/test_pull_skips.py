"""A source that was never asked must not look like a source that found nothing.

`_pull_entity` deletes an entity's prior rows for every provider that answered, so that a
re-pull can't leave a wrong-match ghost behind. An unconfigured source returns an empty
result exactly like a source that looked and found nothing — so before this distinction
existed, losing your TMDB key and running a refresh deleted every date TMDB had ever
written, and reported a clean run.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from release_tracker.config import Settings, get_settings
from release_tracker.db import Database
from release_tracker.models import (
    Certainty,
    DatePrecision,
    Entity,
    MediaKind,
    ReleaseChannel,
    ReleaseObservation,
    SourceTier,
)
from release_tracker.pipeline import pull_entity
from release_tracker.sources import tmdb as tmdb_module
from release_tracker.sources.tmdb import NO_KEY, TmdbSource


@pytest.fixture
def tracked(tmp_path: Path) -> tuple[Path, Entity]:
    """A film with a date TMDB fetched earlier."""
    path = tmp_path / "skips.db"
    db = Database(path)
    ent = Entity.create("Dune: Part Two", MediaKind.MOVIE, external_ids={"tmdb": "693134"})
    db.upsert_entity(ent)
    db.upsert_observations(
        [
            ReleaseObservation(
                entity_id=ent.id,
                channel=ReleaseChannel.THEATRICAL,
                region="US",
                release_date=date(2026, 3, 1),
                precision=DatePrecision.EXACT,
                certainty=Certainty.CONFIRMED,
                source_tier=SourceTier.OFFICIAL,
                provider="tmdb",
                source_name="tmdb",
                confidence=1.0,
                fetched_at=datetime(2026, 6, 1, tzinfo=UTC),
            )
        ]
    )
    db.close()
    return path, ent


def _keyless() -> Settings:
    return get_settings().model_copy(update={"tmdb_api_key": None})


async def test_a_keyless_source_reports_why_rather_than_returning_empty() -> None:
    result = await TmdbSource().pull(
        None,  # type: ignore[arg-type] - the guard returns before the client is touched
        Entity.create("Dune: Part Two", MediaKind.MOVIE),
        _keyless(),
    )
    assert result.skipped == NO_KEY
    assert result.observations == []


def test_a_source_says_up_front_whether_it_can_answer() -> None:
    """Cheap and pure, so a caller can ask before doing any work."""
    assert TmdbSource().unavailable(_keyless()) == NO_KEY
    assert TmdbSource().unavailable(get_settings().model_copy(update={"tmdb_api_key": "k"})) is None


async def test_a_keyless_pull_keeps_the_dates_it_fetched_before(
    tracked: tuple[Path, Entity], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression this exists for. Nothing went wrong — TMDB was simply never asked —
    so its previous answer is still the best one we have and must survive."""
    path, entity = tracked
    monkeypatch.setattr(tmdb_module, "NO_KEY", NO_KEY)  # keep the reason stable
    db = Database(path)
    before = db.observation_dates(entity.id)
    await pull_entity(db, _keyless(), entity)
    after = db.observation_dates(entity.id)
    db.close()

    assert before == [date(2026, 3, 1)]
    assert after == before, "a skipped source must not delete what it wrote last time"


async def test_the_skip_reason_reaches_the_caller(tracked: tuple[Path, Entity]) -> None:
    """An empty diff means something different when a source was never asked, so the
    reason has to travel far enough for a person to be told."""
    path, entity = tracked
    db = Database(path)
    stats = await pull_entity(db, _keyless(), entity)
    db.close()
    assert stats.skipped["tmdb"] == NO_KEY
    assert stats.errors == 0, "a skip is not an error — nothing failed"
