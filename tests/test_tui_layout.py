"""The key bar survives a small window.

Every modal here is `height: auto` inside a `max-height: 90%`, which means the modal clamps
when the terminal shrinks but its auto-height children do not — so the chrome at the bottom
of the flow gets laid out past the bottom edge and the keys vanish. They vanish exactly when
the window is too small to guess them from, which is the worst possible time.

These drive the real screens at sizes down to a degenerate 10 rows and assert the bar is
still on screen.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from textual.widgets import Static

from release_tracker import views
from release_tracker.config import Settings, get_settings
from release_tracker.db import Database
from release_tracker.drafts import Draft
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
from release_tracker.tui.app import RdtApp
from release_tracker.tui.card import CardScreen
from release_tracker.tui.draft import DraftScreen
from release_tracker.tui.edit import EditScreen
from release_tracker.tui.settings import SettingsScreen

TODAY = date(2026, 8, 28)

# A work with enough channels that the body wants more room than a short window has — the
# shape that made the bar disappear in the first place.
_CHANNELS = (
    (ReleaseChannel.PREMIERE, "GB", date(2026, 7, 6)),
    (ReleaseChannel.THEATRICAL, "US", date(2026, 7, 17)),
    (ReleaseChannel.THEATRICAL_LIMITED, "CN", date(2026, 8, 1)),
    (ReleaseChannel.DIGITAL, "US", date(2026, 8, 17)),
    (ReleaseChannel.PHYSICAL, "US", date(2026, 11, 17)),
)


@pytest.fixture
def busy(tmp_path: Path) -> tuple[Path, Entity]:
    path = tmp_path / "layout.db"
    db = Database(path)
    ent = Entity.create("The Odyssey", MediaKind.MOVIE).model_copy(
        update={"consumption_state": ConsumptionState.WANT}
    )
    db.upsert_entity(ent)
    db.upsert_node(Node(id=ent.id, node_kind=NodeKind.WORK, name=ent.title, owned=True))
    db.upsert_observations(
        [
            ReleaseObservation(
                entity_id=ent.id,
                channel=channel,
                region=region,
                release_date=when,
                precision=DatePrecision.EXACT,
                certainty=Certainty.CONFIRMED,
                source_tier=SourceTier.OFFICIAL,
                provider="tmdb",
                source_name="tmdb",
                confidence=1.0,
                fetched_at=datetime(2026, 6, 1, tzinfo=UTC),
            )
            for channel, region, when in _CHANNELS
        ]
    )
    db.close()
    return path, ent


def _app(path: Path) -> RdtApp:
    settings: Settings = get_settings()
    return RdtApp(settings=settings, db=Database(path), today=TODAY)


def _on_screen(app: RdtApp, selector: str, height: int) -> bool:
    node = app.screen.query_one(selector, Static)
    region = node.region
    return bool(node.display) and region.height > 0 and region.y + region.height <= height


SIZES = [(100, 44), (100, 24), (100, 16), (100, 12), (100, 10), (60, 14), (40, 12)]


@pytest.mark.parametrize(("width", "height"), SIZES)
async def test_the_card_keeps_its_key_bar(
    busy: tuple[Path, Entity], width: int, height: int
) -> None:
    path, entity = busy
    app = _app(path)
    async with app.run_test(size=(width, height)) as pilot:
        app.push_screen(CardScreen(entity, views.work_card(app.db, entity)))
        await pilot.pause()
        await pilot.pause()
        assert _on_screen(app, "#card-foot", height), f"card keys off screen at {width}x{height}"


@pytest.mark.parametrize(("width", "height"), SIZES)
async def test_the_edit_form_keeps_its_key_bar(
    busy: tuple[Path, Entity], width: int, height: int
) -> None:
    path, entity = busy
    app = _app(path)
    async with app.run_test(size=(width, height)) as pilot:
        app.push_screen(EditScreen(entity, views.work_card(app.db, entity)))
        await pilot.pause()
        await pilot.pause()
        assert _on_screen(app, "#edit-foot", height), f"edit keys off screen at {width}x{height}"


@pytest.mark.parametrize(("width", "height"), SIZES)
async def test_the_draft_form_keeps_its_key_bar(
    busy: tuple[Path, Entity], width: int, height: int
) -> None:
    path, _ = busy
    app = _app(path)
    async with app.run_test(size=(width, height)) as pilot:
        # The *grown* form: conditional coord rows visible and several provenance lines, which
        # is the shape that actually threatens the docked key bar. A bare draft would not.
        app.push_screen(
            DraftScreen(
                Draft(
                    title="Pluribus",
                    kind=MediaKind.TV,
                    season=2,
                    part=1,
                    reasons=(
                        "kind read off the 3 matches above (all tv)",
                        "`season:2` means this is a series",
                        "date from your `year:2027`",
                    ),
                )
            )
        )
        await pilot.pause()
        await pilot.pause()
        assert _on_screen(app, "#draft-foot", height), f"draft keys off screen at {width}x{height}"


async def test_the_chrome_lines_do_not_sit_on_top_of_each_other(
    busy: tuple[Path, Entity],
) -> None:
    """Textual pins every bottom-docked sibling to the same edge rather than stacking them,
    so the chrome is one docked container. Docking the three lines individually put them all
    at the same y, which looks like the bar is missing rather than overlapping."""
    path, entity = busy
    app = _app(path)
    async with app.run_test(size=(100, 24)) as pilot:
        app.push_screen(EditScreen(entity, views.work_card(app.db, entity)))
        await pilot.pause()
        rows = [
            app.screen.query_one(sid, Static).region.y
            for sid in ("#edit-hint", "#edit-foot", "#edit-status")
        ]
        assert rows == sorted(set(rows)), f"chrome lines overlap: {rows}"


async def test_a_short_window_still_scrolls_the_body(busy: tuple[Path, Entity]) -> None:
    """Reserving the bar must not leave the content unreachable — the body keeps a real
    height and its own scrollbar, rather than being squeezed to nothing."""
    path, entity = busy
    app = _app(path)
    async with app.run_test(size=(100, 14)) as pilot:
        app.push_screen(CardScreen(entity, views.work_card(app.db, entity)))
        await pilot.pause()
        body = app.screen.query_one("#card-body")
        assert body.region.height > 0


@pytest.mark.parametrize(("width", "height"), SIZES)
async def test_the_settings_screen_keeps_its_key_bar(
    busy: tuple[Path, Entity], width: int, height: int
) -> None:
    """The longest form in the app — every setting there is — so the one most able to push
    its own key bar off a short window."""
    path, _ = busy
    app = _app(path)
    async with app.run_test(size=(width, height)) as pilot:
        app.push_screen(SettingsScreen())
        await pilot.pause()
        await pilot.pause()
        assert _on_screen(app, "#settings-foot", height), (
            f"settings keys off screen at {width}x{height}"
        )
