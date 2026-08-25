"""Behavioural tests for the Textual front end, driven through Textual's Pilot.

Assertions are about behaviour, not pixels: what the table holds, where focus is, and
what actually reached the database. No snapshot baselines — the framework moves fast
enough that SVG diffs would be noise, and none of them would have caught the bugs these
do (the state-toggle flush in particular).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path

import pytest
from textual.widgets import DataTable, Input

from release_tracker import query, views
from release_tracker.config import Settings, get_settings
from release_tracker.db import Database
from release_tracker.models import (
    Bucket,
    Certainty,
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
)
from release_tracker.tui.app import RdtApp
from release_tracker.tui.browse import COLUMNS, BrowseScreen
from release_tracker.tui.card import CardScreen, StateToggle
from release_tracker.tui.state import with_bucket
from release_tracker.views import TrackRow

TODAY = date(2026, 6, 1)
_COLUMN_INDEX = {name: i for i, name in enumerate(COLUMNS)}


def _seed(db: Database) -> dict[str, Entity]:
    """Two out-and-unwatched works, one upcoming, one finished."""
    made: dict[str, Entity] = {}
    spec = [
        ("Sinners", MediaKind.MOVIE, date(2025, 6, 1), ConsumptionState.WANT, "Horror"),
        ("Weapons", MediaKind.MOVIE, date(2025, 9, 8), ConsumptionState.WANT, "Horror"),
        ("Dune: Part Three", MediaKind.MOVIE, date(2026, 12, 18), ConsumptionState.WANT, "Sci-Fi"),
        ("The Long Walk", MediaKind.MOVIE, date(2025, 10, 20), ConsumptionState.WATCHED, "Horror"),
    ]
    for title, kind, when, state, genre in spec:
        ent = Entity.create(title, kind).model_copy(update={"consumption_state": state})
        db.upsert_entity(ent)
        db.upsert_node(Node(id=ent.id, node_kind=NodeKind.WORK, name=title, owned=True))
        db.upsert_observations(
            [
                ReleaseObservation(
                    entity_id=ent.id,
                    channel=ReleaseChannel.DIGITAL,
                    region="US",
                    release_date=when,
                    precision=DatePrecision.EXACT,
                    certainty=Certainty.CONFIRMED,
                    source_tier=SourceTier.OFFICIAL,
                    provider="test",
                    source_name="test",
                    confidence=1.0,
                    fetched_at=datetime.now(UTC),
                )
            ]
        )
        node = Node.create(NodeKind.DESCRIPTOR, genre, descriptor_kind=DescriptorKind.GENRE)
        db.upsert_node(node)
        db.upsert_edge(
            Edge(
                src_id=ent.id,
                dst_id=node.id,
                relation=RelationKind.EXHIBITS,
                source_provider="test",
                source_tier=SourceTier.AGGREGATOR,
            )
        )
        person = Node.create(NodeKind.PERSON, f"Dir {title}", source="t", source_id=title)
        db.upsert_node(person)
        db.upsert_edge(
            Edge(
                src_id=person.id,
                dst_id=ent.id,
                relation=RelationKind.CREDITED_ON,
                role=CreditRole.DIRECTOR,
                source_provider="test",
                source_tier=SourceTier.AGGREGATOR,
            )
        )
        made[title] = ent
    return made


@pytest.fixture
def app_db(tmp_path: Path) -> tuple[Path, dict[str, Entity]]:
    path = tmp_path / "tui.db"
    db = Database(path)
    made = _seed(db)
    db.close()
    return path, made


def _app(path: Path) -> RdtApp:
    settings: Settings = get_settings()
    return RdtApp(settings=settings, db=Database(path), today=TODAY)


def _browse(app: RdtApp) -> BrowseScreen:
    screen = app.screen
    assert isinstance(screen, BrowseScreen)
    return screen


def _titles(app: RdtApp) -> list[str]:
    return [r.title for r in _browse(app)._visible]  # pyright: ignore[reportPrivateUsage]


async def test_boots_on_available_and_shows_only_that_bucket(
    app_db: tuple[Path, dict[str, Entity]],
) -> None:
    path, _ = app_db
    app = _app(path)
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        assert app.screen.query_one("#query", Input).value.strip() == "is:available"
        # The Long Walk is watched, so it belongs to the watched bucket, not available.
        assert sorted(_titles(app)) == ["Sinners", "Weapons"]
        assert isinstance(app.screen.focused, Input)  # just start typing


async def test_typing_filters_live_and_agrees_with_the_library(
    app_db: tuple[Path, dict[str, Entity]],
) -> None:
    path, _ = app_db
    app = _app(path)
    async with app.run_test(size=(150, 40)) as pilot:
        bar = app.screen.query_one("#query", Input)
        bar.value = "genre:horror"
        await pilot.pause()
        expected = {
            r.entity_id for r in query.filter_rows(query.parse("genre:horror"), app.snapshot.rows)
        }
        rows: list[TrackRow] = _browse(app)._visible  # pyright: ignore[reportPrivateUsage]
        assert {r.entity_id for r in rows} == expected
        assert app.screen.query_one("#rows", DataTable).row_count == len(expected)


async def test_bucket_keys_rewrite_the_query_string(
    app_db: tuple[Path, dict[str, Entity]],
) -> None:
    """The tabs are sugar over the language — one source of truth, and it teaches the syntax."""
    path, _ = app_db
    app = _app(path)
    async with app.run_test(size=(150, 40)) as pilot:
        bar = app.screen.query_one("#query", Input)
        bar.value = "is:available genre:horror"
        app.screen.query_one("#rows", DataTable).focus()
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        assert bar.value == "is:watched genre:horror"  # other terms survive
        assert _titles(app) == ["The Long Walk"]


async def test_tab_accepts_the_top_completion(app_db: tuple[Path, dict[str, Entity]]) -> None:
    path, _ = app_db
    app = _app(path)
    async with app.run_test(size=(150, 40)) as pilot:
        bar = app.screen.query_one("#query", Input)
        bar.focus()
        bar.value = "genre:hor"
        bar.cursor_position = len(bar.value)
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert bar.value == "genre:Horror"


async def test_enter_opens_the_card_with_the_state_toggle_focused(
    app_db: tuple[Path, dict[str, Entity]],
) -> None:
    """Requirement: the modal exists for the toggle, so the toggle must take focus."""
    path, _ = app_db
    app = _app(path)
    async with app.run_test(size=(150, 40)) as pilot:
        app.screen.query_one("#rows", DataTable).focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, CardScreen)
        assert isinstance(app.screen.focused, StateToggle)


async def test_toggling_state_auto_applies_after_the_debounce(
    app_db: tuple[Path, dict[str, Entity]],
) -> None:
    path, _ = app_db
    app = _app(path)
    async with app.run_test(size=(150, 40)) as pilot:
        app.screen.query_one("#rows", DataTable).focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        card = app.screen
        assert isinstance(card, CardScreen)
        entity_id = card.entity.id
        before = card.entity.consumption_state
        await pilot.press("right")
        await pilot.pause(0.6)  # past the 0.4s debounce
        db = Database(path)
        after = db.get_entity(entity_id)
        db.close()
        assert after is not None
        assert after.consumption_state is not before


async def test_escaping_immediately_still_saves_the_toggle(
    app_db: tuple[Path, dict[str, Entity]],
) -> None:
    """The flush path: toggle then escape faster than the debounce must not lose the change."""
    path, _ = app_db
    app = _app(path)
    async with app.run_test(size=(150, 40)) as pilot:
        app.screen.query_one("#rows", DataTable).focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        card = app.screen
        assert isinstance(card, CardScreen)
        entity_id, before = card.entity.id, card.entity.consumption_state
        await pilot.press("right")
        await pilot.press("escape")  # no pause — beat the debounce deliberately
        await pilot.pause()
        db = Database(path)
        after = db.get_entity(entity_id)
        db.close()
        assert after is not None
        assert after.consumption_state is not before


async def test_state_change_patches_the_row_without_a_full_rebuild(
    app_db: tuple[Path, dict[str, Entity]],
) -> None:
    path, made = app_db
    app = _app(path)
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        calls = {"n": 0}
        original = views.track_rows

        def counting(*a: object, **k: object) -> object:
            calls["n"] += 1
            return original(*a, **k)  # pyright: ignore[reportArgumentType, reportCallIssue]

        import release_tracker.tui.state as tui_state

        tui_state.views.track_rows = counting  # pyright: ignore[reportAttributeAccessIssue]
        try:
            app.set_state(made["Sinners"], ConsumptionState.WATCHED)
            await pilot.pause()
        finally:
            tui_state.views.track_rows = original  # pyright: ignore[reportAttributeAccessIssue]
        assert calls["n"] == 0  # patched one row, did not rebuild the snapshot
        row = next(r for r in app.snapshot.rows if r.entity_id == made["Sinners"].id)
        assert row.state is ConsumptionState.WATCHED
        assert row.bucket is Bucket.WATCHED  # and it moved buckets


def test_with_bucket_is_idempotent_and_preserves_other_terms() -> None:
    assert with_bucket("", Bucket.AVAILABLE) == "is:available"
    assert with_bucket("is:available x", Bucket.WATCHED) == "is:watched x"
    twice = with_bucket(with_bucket("tag:horror", Bucket.UPCOMING), Bucket.UPCOMING)
    assert twice == "is:upcoming tag:horror"


def _painted(app: RdtApp) -> list[str]:
    """Text actually rendered to the screen, as opposed to text a widget merely holds."""
    import re

    return re.findall(r">([^<>]{2,})</text>", app.export_screenshot())


async def test_query_text_is_actually_visible(app_db: tuple[Path, dict[str, Entity]]) -> None:
    """Regression: #query and #hint were both `dock: top`, so the hint painted over the
    query text. Docking several widgets to one edge overlaps them; it does not stack them.
    Every other test passed throughout, because they all asserted data and never pixels."""
    path, _ = app_db
    app = _app(path)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        bar = app.screen.query_one("#query", Input)
        bar.focus()
        await pilot.press(*"genre:horror")
        await pilot.pause()
        assert any("genre:horror" in span for span in _painted(app))


async def test_header_widgets_do_not_overlap(app_db: tuple[Path, dict[str, Entity]]) -> None:
    path, _ = app_db
    app = _app(path)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        regions = [
            app.screen.query_one(sel).region for sel in ("#query", "#hint", "#rows", "#status")
        ]
        for first, second in pairwise(regions):
            assert first.y + first.height <= second.y, f"{first} overlaps {second}"


async def test_typing_continues_the_prefilled_query(
    app_db: tuple[Path, dict[str, Entity]],
) -> None:
    """The bar starts pinned to a bucket, so the caret must sit at the end of it."""
    path, _ = app_db
    app = _app(path)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        bar = app.screen.query_one("#query", Input)
        assert bar.cursor_position == len(bar.value)
        bar.focus()
        await pilot.press(*"genre:horror")
        await pilot.pause()
        assert bar.value == "is:available genre:horror"


async def _type(pilot: object, app: RdtApp, text: str) -> BrowseScreen:
    """Clear the prefilled bucket query and type `text` into the bar, key by key."""
    screen = _browse(app)
    screen.set_query("")
    screen.bar.focus()
    await pilot.press(*text)  # pyright: ignore[reportAttributeAccessIssue]
    await pilot.pause()  # pyright: ignore[reportAttributeAccessIssue]
    return screen


async def test_a_half_typed_value_previews_instead_of_showing_nothing(
    app_db: tuple[Path, dict[str, Entity]],
) -> None:
    """`is:u` is not a filter anyone means — it matches no flag, so it emptied the table.

    It now stands for the `is:upcoming` it is one keystroke away from: the completion is
    painted as the bar's dim tail and the table shows what accepting it would give.
    """
    path, _ = app_db
    app = _app(path)
    async with app.run_test(size=(140, 24)) as pilot:
        screen = await _type(pilot, app, "is:u")
        assert screen.bar.value == "is:u"  # nothing was written into the bar
        assert screen.bar.ghost == "is:upcoming"
        assert screen.effective_query == "is:upcoming"
        assert _titles(app) == ["Dune: Part Three"]
        # ...and the tail is on screen, painted apart from the typed text (the cursor
        # sits on its first character, so it lands in a span of its own)
        painted = _painted(app)
        assert "is:u" in painted and "coming" in painted


async def test_tab_walks_the_preview_through_the_candidates(
    app_db: tuple[Path, dict[str, Entity]],
) -> None:
    path, _ = app_db
    app = _app(path)
    async with app.run_test(size=(140, 24)) as pilot:
        screen = await _type(pilot, app, "is:a")
        assert screen.effective_query == "is:aging"  # first of the two `a` flags
        await pilot.press("tab")
        await pilot.pause()
        assert screen.bar.value == "is:a"  # still a preview, not a commitment
        assert screen.effective_query == "is:available"
        assert sorted(_titles(app)) == ["Sinners", "Weapons"]


async def test_the_preview_is_scoped_to_what_was_typed(
    app_db: tuple[Path, dict[str, Entity]],
) -> None:
    """`is:a` must not walk through `dated` — the typed `a` has ruled it out already."""
    path, _ = app_db
    app = _app(path)
    async with app.run_test(size=(140, 24)) as pilot:
        screen = await _type(pilot, app, "is:a")
        walk = screen._walk  # pyright: ignore[reportPrivateUsage]
        assert walk is not None
        assert [p.label for p in walk.picks] == ["aging", "available"]


async def test_space_takes_the_previewed_completion(
    app_db: tuple[Path, dict[str, Entity]],
) -> None:
    """Space ends the token, so it has to mean "keep what I can see"."""
    path, _ = app_db
    app = _app(path)
    async with app.run_test(size=(140, 24)) as pilot:
        screen = await _type(pilot, app, "is:u ")
        assert screen.bar.value == "is:upcoming "
        assert _titles(app) == ["Dune: Part Three"]


async def test_leaving_the_bar_commits_the_preview(
    app_db: tuple[Path, dict[str, Entity]],
) -> None:
    """The tail stops being painted on blur, so the filter must stop being invisible."""
    path, _ = app_db
    app = _app(path)
    async with app.run_test(size=(140, 24)) as pilot:
        screen = await _type(pilot, app, "is:u")
        await pilot.press("enter")  # focus moves to the table
        await pilot.pause()
        assert screen.bar.value == "is:upcoming"
        assert _titles(app) == ["Dune: Part Three"]


async def test_ctrl_backspace_deletes_the_word_to_the_left(
    app_db: tuple[Path, dict[str, Entity]],
) -> None:
    """Textual binds ctrl+backspace to delete_right_word; nobody means that."""
    path, _ = app_db
    app = _app(path)
    async with app.run_test(size=(140, 24)) as pilot:
        screen = await _type(pilot, app, "kind:movie genre:horror")
        await pilot.press("ctrl+backspace")
        await pilot.pause()
        assert screen.bar.value == "kind:movie genre:"
        await pilot.press("ctrl+backspace", "ctrl+backspace")
        await pilot.pause()
        assert screen.bar.value == "kind:"  # `:` is a word boundary, so `genre:` was two


async def test_tab_is_focus_movement_once_there_is_nothing_to_complete(
    app_db: tuple[Path, dict[str, Entity]],
) -> None:
    """Taking tab for completion left it dead on the table — it has to mean *something*."""
    path, _ = app_db
    app = _app(path)
    async with app.run_test(size=(140, 24)) as pilot:
        await pilot.pause()
        screen = _browse(app)
        for key in ("shift+tab", "tab"):
            screen.table.focus()
            await pilot.pause()
            await pilot.press(key)
            await pilot.pause()
            assert app.screen.focused is screen.bar, key


async def _tab_walk(pilot: object, app: RdtApp, start: str, presses: int) -> list[str]:
    """Tab `presses` times from `start`, collecting the query each press leaves in force.

    The *effective* query, not the bar's value: a completion that merely extends what was
    typed is previewed as the bar's dim tail rather than spliced in, and the tail is what
    the table is filtered by.
    """
    screen = _browse(app)
    bar = screen.query_one("#query", Input)
    bar.focus()
    bar.value = start
    bar.cursor_position = len(start)
    screen._walk = None  # pyright: ignore[reportPrivateUsage]
    seen: list[str] = []
    for _ in range(presses):
        await pilot.press("tab")  # pyright: ignore[reportAttributeAccessIssue]
        await pilot.pause()  # pyright: ignore[reportAttributeAccessIssue]
        seen.append(screen.effective_query)
    return seen


async def test_tab_cycles_through_the_completions(
    app_db: tuple[Path, dict[str, Entity]],
) -> None:
    """Tab used to fill the first match and then stall — the list has to be walkable."""
    path, _ = app_db
    app = _app(path)
    async with app.run_test(size=(140, 24)) as pilot:
        await pilot.pause()
        seen = await _tab_walk(pilot, app, "is:", 4)
        assert len(set(seen)) == 4, seen  # four presses, four distinct completions
        assert all(v.startswith("is:") for v in seen)


async def test_tab_cycle_wraps_and_shift_tab_walks_back(
    app_db: tuple[Path, dict[str, Entity]],
) -> None:
    path, _ = app_db
    app = _app(path)
    async with app.run_test(size=(140, 24)) as pilot:
        await pilot.pause()
        screen = _browse(app)
        first = (await _tab_walk(pilot, app, "is:", 1))[0]
        total = len(query.suggest("is:", 3, app.snapshot.vocab, limit=40))
        for _ in range(total):  # all the way round
            await pilot.press("tab")
            await pilot.pause()
        assert screen.effective_query == first  # wrapped back to the start
        await pilot.press("shift+tab")
        await pilot.pause()
        assert screen.effective_query != first  # and steps back off it


async def test_typing_ends_the_walk(app_db: tuple[Path, dict[str, Entity]]) -> None:
    path, _ = app_db
    app = _app(path)
    async with app.run_test(size=(140, 24)) as pilot:
        await pilot.pause()
        bar = app.screen.query_one("#query", Input)
        await _tab_walk(pilot, app, "is:", 2)
        await pilot.press("space", *"gen")
        await pilot.pause()
        before = bar.value
        assert before.endswith(" gen")  # space took the previewed completion with it
        await pilot.press("tab")
        await pilot.pause()
        assert bar.value == f"{before}re:"  # a fresh walk on the new token, not the old one


async def test_a_sole_completion_hands_off_to_the_next_stage(
    app_db: tuple[Path, dict[str, Entity]],
) -> None:
    """`gen` -> `genre:` has nowhere to cycle, so it must hand off to the values.

    The field name is spliced in outright (a lone candidate is not worth previewing) and
    the preview moves straight on to what can follow the colon, so the first tab already
    lands on a value and the second walks to the next one.
    """
    path, _ = app_db
    app = _app(path)
    async with app.run_test(size=(140, 24)) as pilot:
        await pilot.pause()
        seen = await _tab_walk(pilot, app, "gen", 2)
        assert all(s.startswith("genre:") and s != "genre:" for s in seen), seen
        assert seen[0] != seen[1]


async def test_state_cell_restyles_when_the_state_changes(
    app_db: tuple[Path, dict[str, Entity]],
) -> None:
    """The TUI rendered the state as a bare string, so it never picked up any colour.

    The CLI styled it via a helper that stayed private in cli.py — exactly the drift
    render.py exists to prevent, so both now go through render.state_label.
    """
    from textual.coordinate import Coordinate

    path, made = app_db
    app = _app(path)
    async with app.run_test(size=(150, 30)) as pilot:
        await pilot.pause()
        screen = _browse(app)
        bar = screen.query_one("#query", Input)
        table = screen.table
        state_col = _COLUMN_INDEX["State"]
        target = made["Sinners"]

        seen: dict[ConsumptionState, tuple[str, tuple[str, ...]]] = {}
        for state in (
            ConsumptionState.WATCHING,
            ConsumptionState.DROPPED,
            ConsumptionState.SKIPPED,
        ):
            app.set_state(app.db.get_entity(target.id) or target, state)
            bar.value = ""  # every bucket, so the row stays on screen as it moves
            await pilot.pause()
            visible: list[TrackRow] = screen._visible  # pyright: ignore[reportPrivateUsage]
            row_index = next(i for i, r in enumerate(visible) if r.entity_id == target.id)
            cell = table.get_cell_at(Coordinate(row_index, state_col))
            seen[state] = (str(cell), tuple(str(s.style) for s in cell.spans))

        assert seen[ConsumptionState.WATCHING] == ("watching", ("bold",))
        assert seen[ConsumptionState.DROPPED] == ("dropped", ("red",))
        assert seen[ConsumptionState.SKIPPED] == ("skipped", ("yellow",))
