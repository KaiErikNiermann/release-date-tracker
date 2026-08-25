"""The browse screen: a query bar over the tracked works, filtered as you type.

Filtering is a pure pass over the in-memory snapshot, so it needs no debounce — the
expensive part (building the rows) already happened once at startup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Input, Static

from release_tracker import query, render
from release_tracker.models import Bucket
from release_tracker.tui.state import bucket_of_query, with_bucket
from release_tracker.views import TrackRow

if TYPE_CHECKING:  # pragma: no cover - typing only
    from release_tracker.tui.app import RdtApp

_COLUMNS = ("Date", "⟳", "Title", "Kind", "State", "Who", "Where", "What")
_BUCKET_KEYS = {"1": Bucket.AVAILABLE, "2": Bucket.UPCOMING, "3": Bucket.WATCHED}


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
        Binding("tab", "complete", "Complete", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._visible: list[TrackRow] = []

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
        with Vertical():
            yield DataTable[Text](id="rows")
        yield Static("", id="status")

    def on_mount(self) -> None:
        table = self.table
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns(*_COLUMNS)
        bar = self.query_one("#query", Input)
        bar.value = with_bucket("", Bucket.AVAILABLE)
        bar.focus()
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
        picks = query.suggest(source, bar.cursor_position, self.rdt.snapshot.vocab, limit=5)
        if not picks:
            hint.update("")
            return
        rest = "  ".join(f"[dim]{p.label}[/]" for p in picks[1:])
        hint.update(Text.from_markup(f"[dim]↹[/] [bold]{picks[0].label}[/]  {rest}"))

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
    def action_bucket(self, name: str) -> None:
        bar = self.query_one("#query", Input)
        bar.value = with_bucket(bar.value, Bucket(name))

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
            bar.value = ""

    def action_complete(self) -> None:
        """Accept the top completion, splicing it into the token under the caret."""
        bar = self.query_one("#query", Input)
        if self.screen.focused is not bar:
            return
        picks = query.suggest(bar.value, bar.cursor_position, self.rdt.snapshot.vocab, limit=1)
        if not picks:
            return
        top = picks[0]
        bar.value = bar.value[: top.start] + top.insert + bar.value[top.end :]
        bar.cursor_position = top.start + len(top.insert)

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
