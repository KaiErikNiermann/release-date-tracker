"""The browse screen: a query bar over the tracked works, filtered as you type.

Filtering is a pure pass over the in-memory snapshot, so it needs no debounce — the
expensive part (building the rows) already happened once at startup.

Completion is *previewed* rather than committed: a half-typed ``is:a`` shows the table
for ``is:aging`` with ``ging`` dimmed after the caret, and tab walks that preview through
the rest of the candidates. A partial term therefore never shows an empty table, and what
the table is filtered by is exactly what the bar displays.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, cast

from rich.text import Text
from textual import events, on
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

COLUMNS = ("Date", "⟳", "Title", "Kind", "State", "Who", "Where", "What")
_WALK_LIMIT = 40  # how many completions tab will walk through
_HINT_WIDTH = 6  # how many of them the hint line shows at once
_BUCKET_KEYS = {"1": Bucket.AVAILABLE, "2": Bucket.UPCOMING, "3": Bucket.WATCHED}


class QueryInput(Input):
    """The query bar.

    Two things the browse screen needs that a plain ``Input`` will not give it:

    * a settable completion tail. Textual fills one in only from a ``Suggester``, which is
      handed the whole value and nothing else; ours depends on the caret and on how far
      the tab walk has got, so the screen drives it directly.
    * ctrl+backspace deleting the word to the *left*. Textual binds it to
      ``delete_right_word``, which no terminal user expects. (In a terminal without the
      kitty keyboard protocol, ctrl+backspace is indistinguishable from backspace and
      only alt+backspace / ctrl+w get through — nothing can be done about that here.)
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding(
            "ctrl+backspace,ctrl+h,alt+backspace",
            "delete_left_word",
            "Delete word left",
            show=False,
        ),
    ]

    @property
    def ghost(self) -> str:
        """The completion rendered dim after the caret; empty when there is none."""
        return self._suggestion

    @ghost.setter
    def ghost(self, text: str) -> None:
        self._suggestion = text

    @property
    def _extends_value(self) -> bool:
        return len(self.ghost) > len(self.value) and self.ghost.startswith(self.value)

    @property
    def ghosting(self) -> bool:
        """Is a completion tail actually on screen? The same test Textual renders by."""
        return self.has_focus and self._extends_value

    def accept_ghost(self) -> bool:
        """Commit the dim tail into the value. ``True`` if there was one to commit.

        Deliberately not conditioned on focus: the last thing a blurring bar does is
        accept, and by then Textual has already taken the focus away.
        """
        if not self._extends_value:
            return False
        self.value = self.ghost
        self.cursor_position = len(self.value)
        return True

    async def _on_key(self, event: events.Key) -> None:
        # Space ends the token, so it reads as "take what I can see": the table is already
        # showing the completion's rows, and dropping it on the way to the next term would
        # contradict what was on screen a keystroke ago.
        if event.character == " " and self.cursor_at_end:
            self.accept_ghost()
        await super()._on_key(event)


@dataclass(slots=True)
class _Walk:
    """An in-progress tab walk through the completions for one token.

    Anchored to the text as *typed* (``origin``): applying a pick never moves the anchor,
    so a second tab keeps stepping through the same list rather than re-deriving from its
    own output — a fresh lookup after tab filled ``is:`` in as ``is:available`` matches
    only "available", which would collapse the list and stall the walk.

    ``value``/``caret`` are what the bar looked like after the last apply; if either has
    drifted, the user typed something and the walk is over. ``shown`` says whether
    ``picks[index]`` is currently on screen, which is what decides whether the next tab
    advances or applies where it stands.
    """

    picks: tuple[query.Suggestion, ...]
    index: int
    origin: str
    origin_caret: int
    value: str
    caret: int
    shown: bool = False


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
        self._walk: _Walk | None = None
        self._shown_query: str | None = None  # what the table currently holds

    @property
    def table(self) -> DataTable[Text]:
        """The results table. `query_one` cannot take DataTable[Text] — it isinstance-checks."""
        return cast("DataTable[Text]", self.query_one("#rows", DataTable))

    @property
    def bar(self) -> QueryInput:
        return self.query_one("#query", QueryInput)

    @property
    def effective_query(self) -> str:
        """What the table is filtered by: the bar, plus the completion it is previewing."""
        bar = self.bar
        return bar.ghost if bar.ghosting else bar.value

    @property
    def rdt(self) -> RdtApp:
        from release_tracker.tui.app import RdtApp

        assert isinstance(self.app, RdtApp)
        return self.app

    def compose(self) -> ComposeResult:
        yield QueryInput(placeholder="filter — e.g. kind:movie genre:horror year:2026", id="query")
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
    @on(Input.Changed, "#query")
    def _on_query_changed(self) -> None:
        self._sync_view()

    def on_descendant_focus(self) -> None:
        """Textual drops the completion tail whenever the bar takes focus — put it back."""
        self._sync_view()

    @on(Input.Blurred, "#query")
    def _on_query_blurred(self) -> None:
        self._sync_view()

    def refresh_rows(self) -> None:
        """Re-filter the snapshot and repaint. Pure and in-memory — safe per keystroke."""
        self._shown_query = None  # the rows themselves may have changed
        self._sync_view()

    def _sync_view(self) -> None:
        """Point the bar at the active completion and bring the table in line with it."""
        self._sync_completion()
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

    # --- completion --------------------------------------------------------------
    def _live_walk(self) -> _Walk | None:
        """The walk in progress, if the bar still looks the way it did when we set it."""
        bar, walk = self.bar, self._walk
        if walk is None or bar.value != walk.value or bar.cursor_position != walk.caret:
            return None
        return walk

    def _fresh_walk(self) -> _Walk | None:
        bar = self.bar
        picks = query.suggest(
            bar.value, bar.cursor_position, self.rdt.snapshot.vocab, limit=_WALK_LIMIT
        )
        if not picks:
            return None
        return _Walk(
            picks=picks,
            index=0,
            origin=bar.value,
            origin_caret=bar.cursor_position,
            value=bar.value,
            caret=bar.cursor_position,
        )

    def _ghost(self, walk: _Walk) -> str | None:
        """The text the active pick would produce, when it only *extends* what was typed.

        Only an extension can be painted as the bar's dim tail, and only what is painted
        is allowed to filter — so preview and display are the same string or there is no
        preview at all. A pick that has to rewrite the token (a quoted name, a canonical
        field, a caret parked mid-query) falls back to being spliced in by tab.
        """
        pick = walk.picks[walk.index]
        if pick.kind != "value" or walk.origin_caret != len(walk.origin):
            return None
        candidate = query.apply(walk.origin, pick)
        extends = candidate.startswith(walk.origin) and len(candidate) > len(walk.origin)
        return candidate if extends else None

    def _sync_completion(self) -> None:
        """Derive (or keep) the walk for the token under the caret and paint its tail.

        Typing never commits — a completion the user has not asked for stays a preview,
        so the only thing that changes under them is the table, which is the point.
        """
        bar = self.bar
        if not bar.has_focus:
            # The tail stops being painted the moment focus goes, so keeping it as a
            # filter would narrow the table by something the user can no longer see.
            # Committing keeps the rows they were looking at, with the reason in the bar.
            if not bar.accept_ghost():  # a commit comes back round through Input.Changed
                bar.ghost = ""
            return
        if (walk := self._live_walk()) is None:
            self._walk = walk = self._fresh_walk()
            if walk is None:
                bar.ghost = ""
                return
            walk.shown = self._ghost(walk) is not None
        bar.ghost = (self._ghost(walk) or "") if walk.shown else ""

    def _update_hint(self) -> None:
        hint = self.query_one("#hint", Static)
        walk = self._walk if self.bar.has_focus else None
        if walk is None:
            hint.update("")
            return
        picks, active = walk.picks, walk.index
        # scroll the window with the selection so a long list stays walkable
        first = max(0, min(active - _HINT_WIDTH // 2, len(picks) - _HINT_WIDTH))
        shown = [
            f"[reverse]{p.label}[/]" if i + first == active else f"[dim]{p.label}[/]"
            for i, p in enumerate(picks[first : first + _HINT_WIDTH])
        ]
        count = f"[dim]{active + 1}/{len(picks)}[/]" if len(picks) > 1 else ""
        hint.update(Text.from_markup(f"[dim]↹[/] {'  '.join(shown)}  {count}"))

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

    def action_back(self) -> None:
        """Escape walks out: table -> query bar, then clears a non-empty query."""
        bar = self.bar
        if self.screen.focused is not bar:
            bar.focus()
        elif bar.value:
            self.set_query("")

    def action_complete(self, delta: int = 1) -> None:
        """Walk the completions for the token under the caret; tab again for the next.

        A candidate that merely extends what was typed rides along as the bar's dim tail
        and the table follows it, so each tab shows what the next candidate *means*
        before anything is committed. One that has to rewrite the token is spliced in
        outright — there is nothing to preview and nothing gained by withholding it.

        Away from the bar there is nothing to complete, so the key goes back to meaning
        what it means in every other Textual app and every browser: move focus. With only
        the bar and the table to move between, that is the way back to the query.
        """
        bar = self.bar
        if not bar.has_focus:
            if delta > 0:
                self.focus_next()
            else:
                self.focus_previous()
            return
        if (walk := self._live_walk() or self._fresh_walk()) is None:
            self._walk = None
            return
        if walk.shown:  # what is on screen is a candidate, so move off it
            walk.index = (walk.index + delta) % len(walk.picks)
        walk.shown = True
        self._walk = walk

        if (ghost := self._ghost(walk)) is not None:
            bar.ghost = ghost
        else:
            pick = walk.picks[walk.index]
            bar.ghost = ""
            bar.value = query.apply(walk.origin, pick)
            bar.cursor_position = pick.start + len(pick.insert)
            walk.value, walk.caret = bar.value, bar.cursor_position
            # A sole completion has nowhere to cycle, so end the walk: the next tab then
            # re-derives and moves on to the next stage — `gen` -> `genre:` -> its values.
            if len(walk.picks) == 1:
                self._walk = None
        self._sync_view()

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
