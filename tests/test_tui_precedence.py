"""What a date row says about the two values behind it.

A refresh cannot overwrite a hand-authored date — a pull only clears the providers that
answered, and no source is called ``manual`` — so both claims live in the database and the
ranking picks between them on every read. That is the right design and a confusing one to
meet in silence, because a typed "2026-Q4" loses to a pulled "2026-10-15" on precision.

These pin what the row tells you about that: which value is showing, why, and what the
refresh you just ran actually moved.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from textual.widgets import Static

from release_tracker import views
from release_tracker.config import Settings, get_settings
from release_tracker.db import Database
from release_tracker.edits import MANUAL_PROVIDER
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
from release_tracker.tui.edit import EditScreen, change_note, date_note
from release_tracker.views import DateChange

TODAY = date(2026, 8, 28)


# --- the annotation itself (no screen needed) ---------------------------------------------
def test_a_row_with_only_a_pulled_date_just_names_it() -> None:
    """No second value, so nothing to choose between and nothing to mark."""
    note = date_note("2026-10-15", "")
    assert "pulled[/] 2026-10-15" in note
    assert "showing" not in note


def test_a_row_where_the_typed_date_wins_says_so() -> None:
    assert "yours is showing" in date_note("2026-10-01", "2026-10-15", mine_shown=True)


def test_a_row_where_the_typed_date_loses_says_which_and_why() -> None:
    """The whole point. Without the reason this reads as the tool having ignored you."""
    note = date_note("2026-10-15", "2026-Q4", mine_shown=False, reason="more precise")
    assert "◀" in note
    assert "more precise" in note


def test_a_row_the_refresh_moved_leads_with_the_move() -> None:
    """What changed just now is more urgent than what the current value is, and the old
    value is still on file either way."""
    note = date_note("2026-10-15", "", moved=change_note(date(2026, 9, 1), date(2026, 10, 15)))
    assert note.startswith("[yellow]moved 2026-09-01 → 2026-10-15[/]")


def test_a_channel_that_just_gained_a_date_reads_as_new_not_as_a_move() -> None:
    """`diff_estimates` reports a first appearance as ``old=None``, which is genuine news —
    a physical release finally dated. Rendering it as "moved — → …" says "moved from
    nothing", which reads as a glitch and is what this actually looked like in use."""
    assert change_note(None, date(2026, 8, 1)) == "new 2026-08-01"
    assert "moved" not in date_note("2026-08-01", "", moved=change_note(None, date(2026, 8, 1)))


def test_a_channel_the_source_stopped_carrying_reads_as_dropped() -> None:
    """The other absence, and it means the opposite — worth seeing, and worth not calling
    a move either."""
    assert change_note(date(2026, 11, 17), None) == "dropped 2026-11-17"


def test_a_row_with_nothing_to_report_stays_quiet() -> None:
    """Most rows on most refreshes. A note on every line would bury the two that moved."""
    assert date_note("", "2026-10-15") == ""


# --- wired up through the edit form -------------------------------------------------------
def _obs(
    entity_id: str,
    when: date,
    precision: DatePrecision,
    provider: str,
    *,
    channel: ReleaseChannel = ReleaseChannel.PRIMARY,
) -> ReleaseObservation:
    return ReleaseObservation(
        entity_id=entity_id,
        channel=channel,
        region="WW",
        release_date=when,
        precision=precision,
        certainty=Certainty.CONFIRMED,
        source_tier=SourceTier.OFFICIAL,
        provider=provider,
        source_name=provider,
        confidence=1.0,
        fetched_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


@pytest.fixture
def outranked(tmp_path: Path) -> tuple[Path, Entity]:
    """A work whose typed quarter is beaten by a pulled exact date."""
    path = tmp_path / "prec.db"
    db = Database(path)
    ent = Entity.create("Some Film", MediaKind.MOVIE).model_copy(
        update={"consumption_state": ConsumptionState.WANT}
    )
    db.upsert_entity(ent)
    db.upsert_node(Node(id=ent.id, node_kind=NodeKind.WORK, name=ent.title, owned=True))
    db.upsert_observations(
        [
            _obs(ent.id, date(2026, 10, 1), DatePrecision.QUARTER, MANUAL_PROVIDER),
            _obs(ent.id, date(2026, 10, 15), DatePrecision.EXACT, "tmdb"),
        ]
    )
    db.close()
    return path, ent


def _app(path: Path) -> RdtApp:
    settings: Settings = get_settings()
    return RdtApp(settings=settings, db=Database(path), today=TODAY)


def _notes(screen: EditScreen) -> list[str]:
    return [str(note.content) for note in screen.query(".row-note").results(Static)]


async def test_the_form_shows_the_pulled_value_beside_the_typed_one(
    outranked: tuple[Path, Entity],
) -> None:
    path, entity = outranked
    app = _app(path)
    async with app.run_test(size=(150, 44)) as pilot:
        app.push_screen(EditScreen(entity, views.work_card(app.db, entity)))
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, EditScreen)
        joined = " ".join(_notes(screen))
        assert "2026-10-15" in joined, "the pulled date is shown, not just hinted"
        # The reason carries the marker — it already says yours is not the one showing.
        assert "◀ more precise" in joined


async def test_the_pulled_column_is_never_the_hand_authored_value(tmp_path: Path) -> None:
    """`card.estimates` carries the winner, which is sometimes the typed one — labelling
    that "pulled" would invent a source for a date the user typed themselves."""
    path = tmp_path / "mine-wins.db"
    db = Database(path)
    ent = Entity.create("Mine Wins", MediaKind.MOVIE)
    db.upsert_entity(ent)
    db.upsert_node(Node(id=ent.id, node_kind=NodeKind.WORK, name=ent.title, owned=True))
    db.upsert_observations([_obs(ent.id, date(2026, 10, 15), DatePrecision.EXACT, MANUAL_PROVIDER)])
    db.close()

    fresh = Database(path)
    assert views.pulled_estimates(fresh, ent, MANUAL_PROVIDER) == {}
    fresh.close()


async def test_a_refresh_marks_the_rows_it_moved(outranked: tuple[Path, Entity]) -> None:
    """Landing in a form after a refresh is only useful if it says what the refresh did."""
    path, entity = outranked
    app = _app(path)
    async with app.run_test(size=(150, 44)) as pilot:
        app.push_screen(
            EditScreen(
                entity,
                views.work_card(app.db, entity),
                [
                    DateChange(
                        channel=ReleaseChannel.PRIMARY.value,
                        region="WW",
                        old=date(2026, 9, 1),
                        new=date(2026, 10, 15),
                        new_confirmed=True,
                    )
                ],
            )
        )
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, EditScreen)
        joined = " ".join(_notes(screen))
        assert "moved" in joined
        assert "2026-09-01" in joined and "2026-10-15" in joined


async def test_a_form_opened_by_e_marks_nothing_as_moved(
    outranked: tuple[Path, Entity],
) -> None:
    """`e` is a plain edit — there was no refresh, so nothing moved just now."""
    path, entity = outranked
    app = _app(path)
    async with app.run_test(size=(150, 44)) as pilot:
        app.push_screen(EditScreen(entity, views.work_card(app.db, entity)))
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, EditScreen)
        assert "moved" not in " ".join(_notes(screen))
