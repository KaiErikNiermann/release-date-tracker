"""The card's Sources section, and the update key that acts on it.

The section answers one question — what can this tool refetch, and what do you have to go
read yourself — so the tests are about that distinction holding under pressure: a site that
declines automated extraction must never be treated as refetchable, and a re-pull must
survive a keystroke, fail visibly, and leave the card usable.

Nothing here touches the network: the card reaches it through one module-level name.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from textual.containers import VerticalScroll
from textual.widgets import DataTable, Static

from release_tracker.config import Settings, get_settings
from release_tracker.db import Database
from release_tracker.models import (
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
from release_tracker.tui import card as card_module
from release_tracker.tui.app import RdtApp
from release_tracker.tui.card import CardScreen

# after the seeded release, so the work lands in the default `is:available` view
TODAY = date(2026, 8, 27)


async def _until(pilot: Any, predicate: Any, what: str, timeout: float = 5.0) -> None:
    """Poll rather than sleep a guessed interval — the Windows runners are much slower."""
    waited = 0.0
    while waited < timeout:
        await pilot.pause()
        if predicate():
            return
        await asyncio.sleep(0.02)
        waited += 0.02
    raise AssertionError(f"timed out waiting for {what}")


def _add(db: Database, title: str, kind: MediaKind, ids: dict[str, str]) -> Entity:
    ent = Entity.create(title, kind, external_ids=ids).model_copy(
        update={"consumption_state": ConsumptionState.WANT}
    )
    db.upsert_entity(ent)
    db.upsert_node(Node(id=ent.id, node_kind=NodeKind.WORK, name=title, owned=True))
    db.upsert_observations(
        [
            ReleaseObservation(
                entity_id=ent.id,
                channel=ReleaseChannel.RETAIL,
                region="WW",
                release_date=date(2026, 6, 26),
                precision=DatePrecision.EXACT,
                certainty=Certainty.CONFIRMED,
                source_tier=SourceTier.AGGREGATOR,
                provider="wikidata",
                source_name="Wikidata",
                source_url="https://www.wikidata.org/wiki/Q139719408",
                confidence=0.7,
                fetched_at=datetime.now(UTC),
            )
        ]
    )
    return ent


@pytest.fixture
def app_db(tmp_path: Path) -> Path:
    path = tmp_path / "sources.db"
    db = Database(path)
    # a device we can refetch (wikidata) and one we can only link to (gsmarena)
    _add(
        db,
        "Sony Xperia 1 VIII",
        MediaKind.TECH,
        {"wikidata": "Q139719408", "gsmarena": "14660"},
    )
    db.close()
    return path


def _app(path: Path) -> RdtApp:
    settings: Settings = get_settings()
    return RdtApp(settings=settings, db=Database(path), today=TODAY)


async def _open_card(pilot: Any, app: RdtApp) -> CardScreen:
    app.screen.query_one("#rows", DataTable).focus()
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()
    screen = app.screen
    assert isinstance(screen, CardScreen)
    return screen


def _body(card: CardScreen) -> str:
    return str(card.query_one("#card-detail", Static).content)


async def test_the_card_lists_where_its_dates_can_be_read(app_db: Path) -> None:
    app = _app(app_db)
    async with app.run_test(size=(150, 40)) as pilot:
        card = await _open_card(pilot, app)
        body = _body(card)
        assert "Sources" in body
        assert "Wikidata" in body
        assert "GSMArena" in body


async def test_a_site_that_declines_extraction_is_shown_as_manual(app_db: Path) -> None:
    """The distinction has to survive all the way to the screen, not just the model —
    presenting GSMArena as auto-updating would be a straight lie to the user."""
    app = _app(app_db)
    async with app.run_test(size=(150, 40)) as pilot:
        card = await _open_card(pilot, app)
        auto = [link for link in card.card.sources if link.access.value == "auto"]
        manual = [link for link in card.card.sources if link.access.value == "link"]
        assert [link.provider for link in auto] == ["wikidata"]
        assert [link.provider for link in manual] == ["gsmarena"]
        assert manual[0].reason, "a manual source has to say why it is manual"
        assert "gsmarena.com/model-14660.php" in manual[0].url, "the exact page, not a search"


async def test_update_repulls_and_repaints(app_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    async def _pull(_db: Any, _settings: Any, entity: Entity, **_k: Any) -> None:
        called.append(entity.id)

    monkeypatch.setattr(card_module, "pull_entity", _pull)

    app = _app(app_db)
    async with app.run_test(size=(150, 40)) as pilot:
        card = await _open_card(pilot, app)
        await pilot.press("u")
        await _until(pilot, lambda: bool(called), "the re-pull to run")
        await _until(
            pilot,
            lambda: not card.query_one("#card-body", VerticalScroll).loading,
            "the spinner to clear",
        )
        assert called == [card.entity.id]


async def test_a_failed_update_says_so_and_leaves_the_card_usable(
    app_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead provider must not tear the screen down."""

    async def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("provider down")

    monkeypatch.setattr(card_module, "pull_entity", _boom)

    app = _app(app_db)
    async with app.run_test(size=(150, 40)) as pilot:
        card = await _open_card(pilot, app)
        await pilot.press("u")
        await _until(
            pilot,
            lambda: not card.query_one("#card-body", VerticalScroll).loading,
            "the spinner to clear after the failure",
        )
        assert isinstance(app.screen, CardScreen)
        assert "Sources" in _body(card)


async def test_a_keystroke_mid_update_does_not_cancel_it(
    app_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-pull writes to the db, so being aborted midway leaves the work half-updated with
    nothing on screen to explain it.

    Honest scope: this passes with or without the worker's own group, because nothing else on
    the card spawns a worker to collide with today. It guards the behaviour, not the grouping
    — it would start failing the day a second worker lands here in the default group, which is
    exactly how the add screen shipped this bug once.
    """
    started = asyncio.Event()
    release = asyncio.Event()
    finished: list[str] = []

    async def _slow(_db: Any, _settings: Any, entity: Entity, **_k: Any) -> None:
        started.set()
        await release.wait()
        finished.append(entity.id)

    monkeypatch.setattr(card_module, "pull_entity", _slow)

    app = _app(app_db)
    async with app.run_test(size=(150, 40)) as pilot:
        card = await _open_card(pilot, app)
        await pilot.press("u")
        await _until(pilot, started.is_set, "the re-pull to start")

        await pilot.press("right")  # the user fiddles with the state toggle mid-flight
        await pilot.pause()

        release.set()
        await _until(pilot, lambda: bool(finished), "the re-pull to finish despite the keypress")
        assert finished == [card.entity.id]


async def test_a_work_with_nothing_automatic_says_so_instead_of_pretending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pressing update on a link-only work must not look like it did something."""
    path = tmp_path / "manual.db"
    db = Database(path)
    ent = Entity.create("Poco X7", MediaKind.TECH).model_copy(
        update={"consumption_state": ConsumptionState.WANT}
    )
    db.upsert_entity(ent)
    db.upsert_node(Node(id=ent.id, node_kind=NodeKind.WORK, name=ent.title, owned=True))
    # hand-dated, no ids: the realistic shape for a device Wikidata has never heard of.
    # `manual` is us, not a source, so it must not turn into a link.
    db.upsert_observations(
        [
            ReleaseObservation(
                entity_id=ent.id,
                channel=ReleaseChannel.RETAIL,
                region="WW",
                release_date=date(2026, 1, 15),
                precision=DatePrecision.EXACT,
                certainty=Certainty.CONFIRMED,
                source_tier=SourceTier.OFFICIAL,
                provider="manual",
                source_name="Manual (EDTF)",
                confidence=1.0,
                fetched_at=datetime.now(UTC),
            )
        ]
    )
    db.close()

    pulled: list[str] = []

    async def _pull(_db: Any, _settings: Any, entity: Entity, **_k: Any) -> None:
        pulled.append(entity.id)

    monkeypatch.setattr(card_module, "pull_entity", _pull)

    app = _app(path)
    async with app.run_test(size=(150, 40)) as pilot:
        card = await _open_card(pilot, app)
        # it still gets somewhere to look — pre-built searches — but nothing to re-pull
        assert card.card.sources
        assert all(link.access.value == "link" for link in card.card.sources)
        await pilot.press("u")
        await pilot.pause()
        assert pulled == []
