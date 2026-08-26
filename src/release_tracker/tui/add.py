"""The add palette: the same query syntax, pointed at the outside world.

Typing here searches the external sources rather than the tracker, but it is the *same*
grammar — `dune kind:movie year:2026` narrows a local list and hints an external search
identically, because one parsed Query feeds both. Explicit terms become the search's
kind/year/season hints; the bare words are the search text.

Network work is debounced and runs in an exclusive worker, so a later keystroke cancels
the request in flight instead of racing it.
"""

from __future__ import annotations

import time
from typing import ClassVar

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from release_tracker import query
from release_tracker.capture import capture_work
from release_tracker.lookup import capture_candidates, report_for_candidate
from release_tracker.models import Entity, MediaKind
from release_tracker.sources.base import Candidate

KeyT = tuple[str, "MediaKind | None"]

_DEBOUNCE = 0.6
_MIN_CHARS = 3
_MEMO_TTL = 120.0


class AddScreen(ModalScreen[Entity | None]):
    """Search the external sources and capture a pick. Dismisses with the new entity."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "back", "Back", show=False),
        # The bar is one line, so `down` is dead in it; the OptionList binds its own, and
        # a widget's bindings beat the screen's — so this only ever fires from the bar.
        Binding("down", "focus_candidates", "Into the candidates", show=False),
        # Printable keys are swallowed while the Input has focus, so these only reach the
        # list — the same arrangement the browse table uses.
        Binding("j", "highlight(1)", "Down", show=False),
        Binding("k", "highlight(-1)", "Up", show=False),
        Binding("slash", "focus_query", "Search", show=False),
        Binding("tab", "move_focus(1)", "Focus next", show=False),
        Binding("shift+tab", "move_focus(-1)", "Focus previous", show=False),
    ]

    def __init__(self, initial: str = "") -> None:
        super().__init__()
        self._initial = initial
        self._timer: Timer | None = None
        self._hits: list[tuple[MediaKind, Candidate]] = []
        # The key the shown hits came from, so `enter` can tell "act on these" from
        # "you have typed past them" without guessing at the debounce.
        self._shown_key: tuple[str, MediaKind | None] | None = None
        # Session-scoped, so backspacing through a query costs nothing. Deliberately not
        # in the library: a process-global search cache would surprise CLI callers.
        self._memo: dict[
            tuple[str, MediaKind | None], tuple[float, list[tuple[MediaKind, Candidate]]]
        ] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="add"):
            yield Static(
                Text.from_markup("[bold]Add a title[/]  [dim]— searches TMDB / IGDB / Steam[/]")
            )
            yield Input(placeholder="e.g. dune kind:movie year:2026", id="add-query")
            yield Static("", id="add-status")
            yield OptionList(id="candidates")

    def on_mount(self) -> None:
        bar = self.query_one("#add-query", Input)
        # Carry over the terms an external search can act on (text + kind/year/season);
        # the browse query's local-only filters (is:, tag:, state:) drop out.
        bar.value = query.parse(self._initial).external
        bar.focus()
        if bar.value:
            self._schedule()

    # --- searching -----------------------------------------------------------------
    @on(Input.Changed, "#add-query")
    def _on_change(self) -> None:
        self._schedule()

    @on(Input.Submitted, "#add-query")
    def _on_submit(self) -> None:
        """Enter means "act on what I typed", which is two things depending on the screen.

        If the listed candidates are the ones this query produced, enter steps into them —
        the browse bar's `enter -> table` move. If the query has moved on since (the
        debounce has not fired, or nothing has been searched yet), it runs the search now
        rather than focusing a stale list.
        """
        if self._hits and self._shown_key == self._current_key():
            self.action_focus_candidates()
        else:
            self._schedule(now=True)

    def _current_key(self) -> tuple[str, MediaKind | None]:
        parsed = query.parse(self.query_one("#add-query", Input).value)
        return (parsed.text.strip().lower(), parsed.kind_hint)

    def _schedule(self, *, now: bool = False) -> None:
        if self._timer is not None:
            self._timer.stop()
        parsed = query.parse(self.query_one("#add-query", Input).value)
        if len(parsed.text.strip()) < _MIN_CHARS:
            self._status("[dim]keep typing…[/]")
            return
        if now:
            self.search(parsed.text, parsed.kind_hint)
        else:
            self._timer = self.set_timer(
                _DEBOUNCE, lambda: self.search(parsed.text, parsed.kind_hint)
            )

    def _status(self, markup: str) -> None:
        self.query_one("#add-status", Static).update(Text.from_markup(markup))

    @work(exclusive=True, group="add-search")
    async def search(self, text: str, kind_hint: MediaKind | None) -> None:
        """Exclusive within its own group: a newer keystroke cancels this request rather
        than racing it — but never cancels a capture, which is a write."""
        from release_tracker.tui.app import RdtApp

        assert isinstance(self.app, RdtApp)
        key = (text.lower(), kind_hint)
        cached = self._memo.get(key)
        if cached is not None and time.monotonic() - cached[0] < _MEMO_TTL:
            self._show(cached[1], key)
            return
        self._status(f"[dim]searching “{text}”…[/]")
        try:
            hits = await capture_candidates(
                await self.app.http(), text, self.app.settings, kind_hint=kind_hint
            )
        except Exception as exc:  # a dead provider must not kill the palette
            self._status(f"[red]search failed:[/] {exc}")
            return
        self._memo[key] = (time.monotonic(), hits)
        self._show(hits, key)

    def _show(self, hits: list[tuple[MediaKind, Candidate]], key: KeyT) -> None:
        self._hits = hits
        self._shown_key = key
        options = self.query_one("#candidates", OptionList)
        options.clear_options()
        for kind, cand in hits:
            year = f" [dim]({cand.year})[/]" if cand.year else ""
            extra = f"  [dim]{cand.extra[:60]}[/]" if cand.extra else ""
            options.add_option(
                Option(
                    Text.from_markup(
                        f"[bold]{cand.title}[/]{year}  [dim]{kind.value} · {cand.id_key}"
                        f":{cand.canonical_id} · {cand.score:.2f}[/]{extra}"
                    )
                )
            )
        self._status(
            f"[dim]{len(hits)} candidate(s) · ↓ into the list, enter adds · esc back[/]"
            if hits
            else "[yellow]no matches[/]"
        )

    # --- capturing -----------------------------------------------------------------
    @on(OptionList.OptionSelected, "#candidates")
    def _on_pick(self, event: OptionList.OptionSelected) -> None:
        if 0 <= event.option_index < len(self._hits):
            kind, cand = self._hits[event.option_index]
            self.capture(kind, cand)

    @work(exclusive=True, group="add-capture")
    async def capture(self, kind: MediaKind, cand: Candidate) -> None:
        """Report on the *chosen* candidate then persist — no second search.

        Its own worker group. Sharing the default one with `search` meant a keystroke
        landing mid-capture cancelled it — after the report had been fetched and
        somewhere around the writes, leaving a half-enriched work behind.
        """
        from release_tracker.tui.app import RdtApp

        assert isinstance(self.app, RdtApp)
        parsed = query.parse(self.query_one("#add-query", Input).value)
        self._status(f"[dim]adding “{cand.title}” — pulling dates and credits…[/]")
        client = await self.app.http()
        try:
            report = await report_for_candidate(
                client, parsed.text, kind, cand, self.app.settings, season=parsed.season_hint
            )
            entity = await capture_work(
                self.app.db,
                self.app.settings,
                parsed.text,
                report,
                season=parsed.season_hint,
                client=client,
            )
        except Exception as exc:
            self._status(f"[red]add failed:[/] {exc}")
            return
        if entity is None:
            self._status("[yellow]not tracked[/] — unknown kind, or no canonical id to pin")
            return
        self.dismiss(entity)

    # --- movement -------------------------------------------------------------------
    @property
    def _candidates(self) -> OptionList:
        return self.query_one("#candidates", OptionList)

    def action_focus_candidates(self) -> None:
        """Down out of the query bar lands on the list, as it does out of any search box.

        An empty list has nowhere to land, so the bar keeps focus rather than going dead.
        """
        options = self._candidates
        if options.option_count:
            if options.highlighted is None:
                options.highlighted = 0
            options.focus()

    def action_focus_query(self) -> None:
        self.query_one("#add-query", Input).focus()

    def action_highlight(self, delta: int) -> None:
        options = self._candidates
        if options.option_count:
            options.highlighted = max(
                0, min((options.highlighted or 0) + delta, options.option_count - 1)
            )

    def action_move_focus(self, delta: int) -> None:
        """With only a bar and a list here, either direction is the way to the other one."""
        if delta > 0:
            self.focus_next()
        else:
            self.focus_previous()

    def action_back(self) -> None:
        """Escape walks out: candidates -> query bar, then closes the palette."""
        bar = self.query_one("#add-query", Input)
        if self.focused is not bar:
            bar.focus()
        else:
            self.dismiss(None)
