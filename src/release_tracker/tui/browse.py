"""The browse screen: a query bar over the tracked works, filtered as you type.

Filtering is a pure pass over the in-memory snapshot, so it needs no debounce — the
expensive part (building the rows) already happened once at startup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, cast

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.widgets import DataTable, Input, Static

from release_tracker import query, render
from release_tracker.models import Bucket
from release_tracker.tui.state import bucket_of_query, with_bucket
from release_tracker.views import TrackRow

if TYPE_CHECKING:  # pragma: no cover - typing only
    from release_tracker.tui.app import RdtApp

_COLUMNS = ("Date", "⟳", "Title", "Kind", "State", "Who", "Where", "What")
_CYCLE_LIMIT = 40  # how many completions tab will walk through
_HINT_WIDTH = 6  # how many of them the hint line shows at once
_BUCKET_KEYS = {"1": Bucket.AVAILABLE, "2": Bucket.UPCOMING, "3": Bucket.WATCHED}


@dataclass(slots=True)
class _Cycle:
    """An in-progress tab walk through the completions for one token.

    The list has to be remembered rather than recomputed: once tab has filled `is:` in
    as `is:available`, re-running suggest() on the new text matches only "available", so
    a fresh lookup would collapse the list to a single entry and cycling would stall.
    ``value``/``caret`` are what the bar looked like after the last insert — if either
    has changed, the user typed something and the walk is over.
    """

    picks: tuple[query.Suggestion, ...]
    index: int
    start: int
    value: str
    caret: int


class BrowseScreen(Screen[None]):
    """Query bar + results table. Everything else in the app is a modal over this."""

    BINDINGS: ClassVar[list[BindingType]] = [
        # While the Input has focus it swallows printable keys, so these single-key
        # actions only fire once focus is on the table — no chords, no collisions.
        Binding("1", "bucket('available')", "Available", show=False),
        Binding("2", "bucket('upcoming')", "Upcoming", show=False),
        Binding("3", "bucket('watched')", "Watched", show=False),
        Binding("j", "cursor(1)", "Down", show=False),
        Binding("k", "cursor(-1)", "Up", show=False),
        Binding("slash", "focus_query", "Search", show=False),
        Binding("a", "add", "Add"),
        Binding("r", "reload", "Reload"),
        Binding("q", "quit", "Quit"),
        Binding("escape", "back", "Back", show=False),
        Binding("tab", "complete(1)", "Complete", show=False),
        Binding("shift+tab", "complete(-1)", "Complete back", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._visible: list[TrackRow] = []
        self._cycle: _Cycle | None = None

    @property
    def table(self) -> DataTable[Text]:
        """The results table. `query_one` cannot take DataTable[Text] — it isinstance-checks."""
        return cast("DataTable[Text]", self.query_one("#rows", DataTable))

    @property
    def rdt(self) -> RdtApp:
        from release_tracker.tui.app import RdtApp

        assert isinstance(self.app, RdtApp)
        return self.app

    def compose(self) -> ComposeResult:
        yield Input(placeholder="filter — e.g. kind:movie genre:horror year:2026", id="query")
        yield Static("", id="hint")
        yield DataTable[Text](id="rows")
        yield Static("", id="status")

    def on_mount(self) -> None:
        table = self.table
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns(*_COLUMNS)
        self.set_query(f"{with_bucket('', Bucket.AVAILABLE)} ")
        self.query_one("#query", Input).focus()
        self.refresh_rows()

    # --- filtering ---------------------------------------------------------------
    @on(Input.Changed, "#query")
    def _on_query_changed(self) -> None:
        self.refresh_rows()

    def refresh_rows(self) -> None:
        """Re-filter the snapshot and repaint. Pure and in-memory — safe per keystroke."""
        source = self.query_one("#query", Input).value
        parsed = query.parse(source)
        self._visible = query.filter_rows(parsed, self.rdt.snapshot.rows)
        self._visible.sort(key=lambda r: (r.pivot_when is None, r.pivot_when or self.rdt.today))

        table = self.table
        table.clear()
        for row in self._visible:
            who, where, what = render.wcw_cells(row)
            table.add_row(
                Text.from_markup(render.fmt_cell(row.digital or row.theatrical)),
                Text.from_markup(render.fresh_dot(row.freshness)),
                Text.from_markup(render.title_cell(row)),
                Text(row.kind.value),
                Text(row.state.value),
                Text.from_markup(who),
                Text.from_markup(where),
                Text.from_markup(what),
                key=row.entity_id,
            )
        self._update_hint(source)
        self._update_status(parsed)

    def _update_hint(self, source: str) -> None:
        bar = self.query_one("#query", Input)
        hint = self.query_one("#hint", Static)
        if not self.screen.focused or self.screen.focused.id != "query":
            hint.update("")
            return
        if (cycle := self._active_cycle()) is not None:
            picks, active = cycle.picks, cycle.index
        else:
            picks = query.suggest(
                source, bar.cursor_position, self.rdt.snapshot.vocab, limit=_CYCLE_LIMIT
            )
            active = 0
        if not picks:
            hint.update("")
            return
        # scroll the window with the selection so a long list stays walkable
        first = max(0, min(active - _HINT_WIDTH // 2, len(picks) - _HINT_WIDTH))
        shown = [
            f"[reverse]{p.label}[/]" if i + first == active else f"[dim]{p.label}[/]"
            for i, p in enumerate(picks[first : first + _HINT_WIDTH])
        ]
        count = f"[dim]{active + 1}/{len(picks)}[/]" if len(picks) > 1 else ""
        hint.update(Text.from_markup(f"[dim]↹[/] {'  '.join(shown)}  {count}"))

    def _update_status(self, parsed: query.Query) -> None:
        bucket = bucket_of_query(self.query_one("#query", Input).value)
        parts = [
            f"{len(self._visible)}/{len(self.rdt.snapshot.rows)}",
            f"[bold]{bucket.value}[/]" if bucket else "all buckets",
            "[dim]1/2/3 bucket · a add · enter card · / search · r reload · q quit[/]",
        ]
        if parsed.unknown_fields:
            parts.insert(1, f"[yellow]?{','.join(parsed.unknown_fields)}[/]")
        self.query_one("#status", Static).update(Text.from_markup("  ·  ".join(parts)))

    # --- actions -----------------------------------------------------------------
    def set_query(self, text: str) -> None:
        """Set the query and park the caret at the end, so the next keystroke continues it."""
        bar = self.query_one("#query", Input)
        bar.value = text
        bar.cursor_position = len(text)

    def action_bucket(self, name: str) -> None:
        self.set_query(with_bucket(self.query_one("#query", Input).value, Bucket(name)))

    def action_cursor(self, delta: int) -> None:
        table = self.table
        table.move_cursor(row=max(0, min(table.cursor_row + delta, table.row_count - 1)))

    def action_focus_query(self) -> None:
        self.query_one("#query", Input).focus()

    def action_back(self) -> None:
        """Escape walks out: table -> query bar, then clears a non-empty query."""
        bar = self.query_one("#query", Input)
        if self.screen.focused is not bar:
            bar.focus()
        elif bar.value:
            self.set_query("")

    def _active_cycle(self) -> _Cycle | None:
        """The current tab walk, if the user has not typed since the last insert."""
        bar = self.query_one("#query", Input)
        cycle = self._cycle
        if cycle is None or bar.value != cycle.value or bar.cursor_position != cycle.caret:
            return None
        return cycle

    def action_complete(self, delta: int = 1) -> None:
        """Walk the completions for the token under the caret; tab again for the next.

        Repeated tab steps through the list and wraps; shift+tab steps back. Typing
        anything ends the walk and the next tab starts a fresh one.
        """
        bar = self.query_one("#query", Input)
        if self.screen.focused is not bar:
            return
        if (cycle := self._active_cycle()) is not None:
            picks, start = cycle.picks, cycle.start
            end = start + len(picks[cycle.index].insert)
            index = (cycle.index + delta) % len(picks)
        else:
            picks = query.suggest(
                bar.value, bar.cursor_position, self.rdt.snapshot.vocab, limit=_CYCLE_LIMIT
            )
            if not picks:
                self._cycle = None
                return
            index, start, end = 0, picks[0].start, picks[0].end
        insert = picks[index].insert
        value = bar.value[:start] + insert + bar.value[end:]
        caret = start + len(insert)
        bar.value = value
        bar.cursor_position = caret
        # A sole completion has nowhere to cycle, so end the walk: the next tab then
        # re-derives and moves on to the next stage — `gen` -> `genre:` -> its values.
        self._cycle = (
            None
            if len(picks) == 1
            else _Cycle(picks=picks, index=index, start=start, value=value, caret=caret)
        )
        self._update_hint(value)

    @on(Input.Submitted, "#query")
    def _on_submit(self) -> None:
        self.table.focus()

    def selected_row(self) -> TrackRow | None:
        table = self.table
        if not self._visible or not 0 <= table.cursor_row < len(self._visible):
            return None
        return self._visible[table.cursor_row]

    @on(DataTable.RowSelected, "#rows")
    def _on_row_selected(self) -> None:
        """DataTable owns `enter` and stops it, so the open action hangs off its message."""
        if (row := self.selected_row()) is not None:
            self.rdt.open_card(row)

    def action_add(self) -> None:
        self.rdt.open_add(self.query_one("#query", Input).value)

    def action_reload(self) -> None:
        self.rdt.reload_snapshot()
        self.refresh_rows()
