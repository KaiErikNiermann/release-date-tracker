"""Capturing tech when Wikidata has never heard of it.

Wikidata is the only structured source for tech and it knows a small fraction of consumer
devices — 253 smartphone models carry a release date, 15 of them from 2025 or later. So a
*miss is the expected case*, not an error, and it must never stop the user tracking the
thing. These tests pin that: the tracker has always let tech in as a bare entity, and
giving tech a candidate list must not quietly take that away.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import httpx
import pytest

from release_tracker.capture import run_capture
from release_tracker.config import Settings
from release_tracker.db import Database
from release_tracker.lookup import RdReport
from release_tracker.models import Entity, MediaKind
from release_tracker.sources.base import Candidate
from release_tracker.sources.wikidata import WikidataSource


async def _no_candidates(
    _self: object,
    _client: httpx.AsyncClient,
    _query: str,
    _kind: MediaKind,
    _settings: Settings,
    *,
    limit: int = 6,
) -> list[Candidate]:
    """Wikidata drawing a blank — what "Poco X7" really returns."""
    del limit
    return []


async def _one_candidate(
    _self: object,
    _client: httpx.AsyncClient,
    _query: str,
    _kind: MediaKind,
    _settings: Settings,
    *,
    limit: int = 6,
) -> list[Candidate]:
    del limit
    return [
        Candidate(
            source="wikidata",
            id_key="wikidata",
            canonical_id="Q139719408",
            title="Sony Xperia 1 VIII",
            extra="smartphone model by Sony",
            url="https://www.wikidata.org/wiki/Q139719408",
        )
    ]


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "capture.db")


@pytest.mark.asyncio
async def test_tech_wikidata_never_heard_of_is_still_tracked(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression this whole design is arranged around.

    Making tech resolvable must not let `capture_work`'s "an unpinned entity of this kind
    is a bogus stub" rule apply to it. That rule is right for a movie — TMDB coverage is
    near-total, so no id means the wrong title — and wrong for a gadget, where no id just
    means Wikidata is thin.
    """
    monkeypatch.setattr(WikidataSource, "search_candidates", _no_candidates)

    outcome = await run_capture(db, Settings(), "Poco X7", kind_hint=MediaKind.TECH)

    assert outcome.tracked, "a Wikidata miss must still track the device"
    assert outcome.entity is not None
    assert outcome.entity.kind is MediaKind.TECH
    assert outcome.entity.external_ids == {}, "nothing to pin, and nothing invented"
    assert db.get_entity(outcome.entity.id) is not None


@pytest.mark.asyncio
async def test_a_tracked_miss_still_carries_the_search_policy(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A miss is not a dead end: the report still says which region and which sources to
    read, which is the only guidance the user gets for a device we cannot date."""
    monkeypatch.setattr(WikidataSource, "search_candidates", _no_candidates)

    outcome = await run_capture(db, Settings(), "Poco X7", kind_hint=MediaKind.TECH)

    assert outcome.report is not None
    assert outcome.report.kind is MediaKind.TECH
    assert outcome.report.region
    assert outcome.report.preferred_sources


@pytest.mark.asyncio
async def test_a_movie_miss_stays_untracked(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the rule, so the fallback can't be over-applied: an unpinned movie
    is an un-enrichable stub and is better surfaced as "not tracked" than silently added."""

    async def _nothing(
        _client: httpx.AsyncClient,
        _query: str,
        _settings: Settings,
        *,
        kind_hint: MediaKind | None,
        limit: int = 8,
    ) -> list[tuple[MediaKind, Candidate]]:
        del kind_hint, limit
        return []

    monkeypatch.setattr("release_tracker.capture.capture_candidates", _nothing)
    monkeypatch.setattr(
        "release_tracker.capture.lookup",
        _report_stub(MediaKind.MOVIE),
    )

    outcome = await run_capture(db, Settings(), "Nonexistent Film", kind_hint=MediaKind.MOVIE)

    assert not outcome.tracked
    assert outcome.entity is None


def _report_stub(kind: MediaKind) -> Callable[..., Coroutine[Any, Any, RdReport]]:
    """A `lookup` that finds nothing but does know the kind."""

    async def _lookup(*_a: object, **_k: object) -> RdReport:
        return RdReport(query="q", found=False, kind=kind, matched_title="q")

    return _lookup


@pytest.mark.asyncio
async def test_a_wikidata_hit_pins_the_qid(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of the coin — a hit is pinned, so the puller uses the id directly
    instead of searching, and the card can deep-link from it."""
    monkeypatch.setattr(WikidataSource, "search_candidates", _one_candidate)

    async def _no_pull(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr("release_tracker.capture.pull_entity", _no_pull)
    monkeypatch.setattr("release_tracker.capture.enrich_work", _no_pull)
    monkeypatch.setattr("release_tracker.lookup.pull_entity", _no_pull, raising=False)

    outcome = await run_capture(db, Settings(), "Xperia 1 VIII", kind_hint=MediaKind.TECH)

    assert outcome.tracked
    entity: Entity | None = outcome.entity
    assert entity is not None
    assert entity.external_ids.get("wikidata") == "Q139719408"
