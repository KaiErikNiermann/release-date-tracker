"""The browse screen: a query bar over the tracked works, filtered as you type.

Filtering is a pure pass over the in-memory snapshot, so it needs no debounce — the
expensive part (building the rows) already happened once at startup.

Completion is *previewed* rather than committed: a half-typed ``is:a`` shows the table
for ``is:aging`` with ``ging`` dimmed after the caret, and tab walks that preview through
the rest of the candidates. A partial term therefore never shows an empty table, and what
the table is filtered by is exactly what the bar displays. The bar itself is the shared
:class:`CompletingInput` — the card editor completes its fields against the same graph
with the same keys.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.widgets import DataTable, Input, Static

from release_tracker import query, render
from release_tracker.models import Bucket
from release_tracker.tui.completing import WALK_LIMIT, CompletingInput, completion_hint
from release_tracker.tui.state import bucket_of_query, with_bucket
from release_tracker.views import TrackRow

if TYPE_CHECKING:  # pragma: no cover - typing only
    from release_tracker.tui.app import RdtApp

COLUMNS = ("Date", "⟳", "Title", "Kind", "State", "Who", "Where", "What")
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
        # The bar is one line, so `down` is dead in it; the table binds its own, and a
        # widget's bindings beat the screen's, so this only ever fires from the bar.
        Binding("down", "focus_rows", "Into the list", show=False),
        Binding("a", "add", "Add"),
        Binding("r", "reload", "Reload"),
        Binding("q", "quit", "Quit"),
        Binding("escape", "back", "Back", show=False),
        # The bar binds tab for its own completion walk, and a widget's bindings beat the
        # screen's — so these only ever fire from the table, where nothing can be completed.
        Binding("tab", "move_focus(1)", "Focus next", show=False),
        Binding("shift+tab", "move_focus(-1)", "Focus previous", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._visible: list[TrackRow] = []
        self._shown_query: str | None = None  # what the table currently holds

    @property
    def table(self) -> DataTable[Text]:
        """The results table. `query_one` cannot take DataTable[Text] — it isinstance-checks."""
        return cast("DataTable[Text]", self.query_one("#rows", DataTable))

    @property
    def bar(self) -> CompletingInput:
        return self.query_one("#query", CompletingInput)

    @property
    def effective_query(self) -> str:
        """What the table is filtered by: the bar, plus the completion it is previewing."""
        return self.bar.effective

    @property
    def rdt(self) -> RdtApp:
        from release_tracker.tui.app import RdtApp

        assert isinstance(self.app, RdtApp)
        return self.app

    def compose(self) -> ComposeResult:
        yield CompletingInput(
            self._suggest,
            placeholder="filter — e.g. kind:movie genre:horror year:2026",
            id="query",
            # the table is filtered by the offer, so a space or a step away takes it
            implicit_accept=True,
        )
        yield Static("", id="hint")
        yield DataTable[Text](id="rows")
        yield Static("", id="status")

    def on_mount(self) -> None:
        table = self.table
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns(*COLUMNS)
        self.set_query(f"{with_bucket('', Bucket.AVAILABLE)} ")
        self.bar.focus()
        self.refresh_rows()

    # --- filtering ---------------------------------------------------------------
    def _suggest(self, source: str, caret: int) -> tuple[query.Suggestion, ...]:
        """The bar completes the token under the caret against the whole query language."""
        return query.suggest(source, caret, self.rdt.snapshot.vocab, limit=WALK_LIMIT)

    @on(Input.Changed, "#query")
    @on(CompletingInput.Offered, "#query")
    def _on_query_changed(self) -> None:
        self._sync_view()

    def on_descendant_focus(self) -> None:
        self._sync_view()

    def refresh_rows(self) -> None:
        """Re-filter the snapshot and repaint. Pure and in-memory — safe per keystroke."""
        self._shown_query = None  # the rows themselves may have changed
        self._sync_view()

    def _sync_view(self) -> None:
        """Bring the table and the footers in line with what the bar currently means."""
        source = self.effective_query
        parsed = query.parse(source)
        if source != self._shown_query:
            self._shown_query = source
            self._fill_table(parsed)
        self._update_hint()
        self._update_status(parsed)

    def _fill_table(self, parsed: query.Query) -> None:
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
                Text.from_markup(render.state_label(row.state)),
                Text.from_markup(who),
                Text.from_markup(where),
                Text.from_markup(what),
                key=row.entity_id,
            )

    def _update_hint(self) -> None:
        bar = self.bar
        hint = self.query_one("#hint", Static)
        hint.update(completion_hint(bar.picks, bar.index) if bar.picks else "")

    def _update_status(self, parsed: query.Query) -> None:
        bucket = bucket_of_query(self.effective_query)
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
        bar = self.bar
        bar.ghost = ""
        bar.value = text
        bar.cursor_position = len(text)

    def action_bucket(self, name: str) -> None:
        self.set_query(with_bucket(self.effective_query, Bucket(name)))

    def action_cursor(self, delta: int) -> None:
        table = self.table
        table.move_cursor(row=max(0, min(table.cursor_row + delta, table.row_count - 1)))

    def action_focus_query(self) -> None:
        self.bar.focus()

    def action_focus_rows(self) -> None:
        """Down out of the query bar lands on the list, as it does out of any search box.

        Focus alone is the whole move: the cursor is already on the first row (filtering
        rebuilds the table), so this reads as bar -> row 1, and the next down -> row 2.
        An empty result set has nowhere to land, so the bar keeps focus.
        """
        if self.table.row_count:
            self.table.focus()

    def action_back(self) -> None:
        """Escape walks out: table -> query bar, then clears a non-empty query."""
        bar = self.bar
        if self.screen.focused is not bar:
            bar.focus()
        elif bar.value:
            self.set_query("")

    def action_move_focus(self, delta: int) -> None:
        """Tab where there is nothing to complete means what it means everywhere else.

        The bar binds tab for its own completion walk, so this only fires from the table —
        and with just the bar and the table to move between, either direction is the way
        back to the query, which is where a shift+tab out of a list is aiming anyway.
        """
        if delta > 0:
            self.focus_next()
        else:
            self.focus_previous()

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
        self.rdt.open_add(self.effective_query)

    def action_reload(self) -> None:
        self.rdt.reload_snapshot()
        self.refresh_rows()
