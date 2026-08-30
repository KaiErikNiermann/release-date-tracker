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

from release_tracker import drafts, query
from release_tracker.capture import capture_work
from release_tracker.drafts import Draft
from release_tracker.lookup import (
    DETECT_KINDS,
    MATCH_FLOOR,
    capture_candidates,
    report_for_candidate,
)
from release_tracker.models import Entity, MediaKind
from release_tracker.sources import unavailable_for
from release_tracker.sources.base import Candidate
from release_tracker.tech import looks_like_tech
from release_tracker.tui.draft import DraftScreen

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
        Binding("e", "review", "Review before adding", show=False),
        Binding("slash", "focus_query", "Search", show=False),
        Binding("tab", "move_focus(1)", "Focus next", show=False),
        Binding("shift+tab", "move_focus(-1)", "Focus previous", show=False),
    ]

    def __init__(self, initial: str = "") -> None:
        super().__init__()
        self._initial = initial
        self._timer: Timer | None = None
        self._hits: list[tuple[MediaKind, Candidate]] = []
        # The "add it yourself" row, always offered below the hits once a search has run.
        # Never auto-added: everything on it is inferred, so it only ever opens review.
        self._freeform: Draft | None = None
        # The key the shown hits came from, so `enter` can tell "act on these" from
        # "you have typed past them" without guessing at the debounce.
        self._shown_key: tuple[str, MediaKind | None] | None = None
        # Session-scoped, so backspacing through a query costs nothing. Deliberately not
        # in the library: a process-global search cache would surprise CLI callers.
        self._memo: dict[KeyT, tuple[float, list[tuple[MediaKind, Candidate]], Draft]] = {}

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
            self.search(
                parsed.text,
                parsed.kind_hint,
                year_hint=parsed.year_hint,
                season_hint=parsed.season_hint,
            )
        else:
            self._timer = self.set_timer(
                _DEBOUNCE,
                lambda: self.search(
                    parsed.text,
                    parsed.kind_hint,
                    year_hint=parsed.year_hint,
                    season_hint=parsed.season_hint,
                ),
            )

    def _status(self, markup: str) -> None:
        self.query_one("#add-status", Static).update(Text.from_markup(markup))

    @work(exclusive=True, group="add-search")
    async def search(
        self,
        text: str,
        kind_hint: MediaKind | None,
        *,
        year_hint: int | None = None,
        season_hint: int | None = None,
    ) -> None:
        """Exclusive within its own group: a newer keystroke cancels this request rather
        than racing it — but never cancels a capture, which is a write."""
        from release_tracker.tui.app import RdtApp

        assert isinstance(self.app, RdtApp)
        key = (text.lower(), kind_hint)
        cached = self._memo.get(key)
        if cached is not None and time.monotonic() - cached[0] < _MEMO_TTL:
            self._show(cached[1], key, cached[2])
            return
        self._status(f"[dim]searching “{text}”…[/]")
        # Cleared by whoever finishes: `_show` on success, the handler below on failure.
        # Deliberately not a `finally` — a cancelled search must leave the spinner alone,
        # because the newer keystroke that cancelled it has already raised its own.
        self._candidates.loading = True
        try:
            hits = await capture_candidates(
                await self.app.http(), text, self.app.settings, kind_hint=kind_hint
            )
            # Tech is deliberately absent from the unhinted sweep: Wikidata label-matches
            # everything at score 1.0, so "Dune" would return a sand dune and a Klaus Schulze
            # album alongside the film. But once the media DBs have come back empty there is
            # nothing left to drown — so when the text plainly names a device, retry as tech.
            # Same routing `lookup` already does via `looks_like_tech`.
            if not hits and kind_hint is None and looks_like_tech(text):
                hits = await capture_candidates(
                    await self.app.http(), text, self.app.settings, kind_hint=MediaKind.TECH
                )
            # What we would write if none of the above is it. Always built, because a search
            # that found things can still have found the wrong things, and the unannounced
            # case — a device, a film nobody has listed yet, an album — is the one a release
            # tracker exists for. The hits feed the inference rather than suppressing it.
            freeform = await drafts.infer_freeform(
                await self.app.http(),
                text,
                kind_hint=kind_hint,
                year_hint=year_hint,
                season_hint=season_hint,
                hits=hits,
            )
        except Exception as exc:  # a dead provider must not kill the palette
            # The escape hatch matters most when the search itself is down, so the row still
            # appears — from the offline half of the inference. `_show` resets the hit list
            # with it, which a bare `return` here would leave stale and mis-indexed.
            self._show(
                [],
                key,
                drafts.prefill(
                    text, kind_hint=kind_hint, year_hint=year_hint, season_hint=season_hint
                ),
                status=f"[red]search failed:[/] {exc}",
            )
            return
        self._memo[key] = (time.monotonic(), hits, freeform)
        missing = (
            unavailable_for(self._searched_kinds(kind_hint), self.app.settings) if not hits else {}
        )
        self._show(hits, key, freeform, missing)

    def _searched_kinds(self, kind_hint: MediaKind | None) -> tuple[MediaKind, ...]:
        """The kinds this query actually reached, so only their sources are reported on."""
        return (kind_hint,) if kind_hint is not None else DETECT_KINDS

    def _show(
        self,
        hits: list[tuple[MediaKind, Candidate]],
        key: KeyT,
        freeform: Draft,
        missing: dict[str, str] | None = None,
        *,
        status: str | None = None,
    ) -> None:
        self._hits = hits
        self._freeform = freeform
        self._shown_key = key
        options = self._candidates
        options.loading = False
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
        options.add_option(Option(Text.from_markup(self._freeform_label(freeform, hits))))
        self._status(status if status is not None else self._search_status(hits, missing, key))

    def _freeform_label(self, draft: Draft, hits: list[tuple[MediaKind, Candidate]]) -> str:
        """The one row, worded for the situation it is standing in.

        A device with a lineage keeps the fullest form — it is the case with the most inferred
        and so the most worth stating up front. Otherwise the wording turns on whether anything
        credible came back: with real matches this is the quiet "none of these", and without
        them it is the main way forward and says so.
        """
        if draft.version is not None:
            version = f" {draft.version.token}"
            follows = (
                f"  [dim]follows {draft.predecessor.label}[/]"
                if draft.predecessor is not None
                else "  [dim]no lineage found[/]"
            )
            return (
                f"[bold]{draft.title}[/]  [dim]not announced · track it as new{version}[/]{follows}"
            )
        kind = f"  [dim]as {draft.kind.value}[/]"
        if any(cand.score >= MATCH_FLOOR for _, cand in hits):
            return f"[bold]+ Add “{draft.title}” myself[/]  [dim]— none of these[/]{kind}"
        return f"[bold]+ Add “{draft.title}” as a new entry[/]  [dim]— nothing matched[/]{kind}"

    def _search_status(
        self,
        hits: list[tuple[MediaKind, Candidate]],
        missing: dict[str, str] | None,
        key: KeyT,
    ) -> str:
        """What the search itself found. The freeform row is *extra* to this line, never a
        replacement for it — an empty result still has to say why it was empty, or a typo and
        an unconfigured source both read as "this thing does not exist"."""
        if hits:
            return (
                f"[dim]{len(hits)} candidate(s) · ↓ into the list, enter adds, e reviews first"
                " · esc back[/]"
            )
        tail = " [dim]— or add it yourself, last row[/]"
        if missing:
            # An unconfigured source returns an empty list exactly like a real miss, so
            # without this the answer to "why is Dune not here" is indistinguishable from
            # "Dune does not exist". Name what was never asked.
            named = "; ".join(sorted(missing.values()))
            return f"[yellow]nothing matched[/] [dim]— {named}[/]{tail}"
        # Only worth saying when it would actually change the outcome: a hinted search
        # already scoped itself, and a name we recognised as a device has already been
        # retried as tech, so telling either to add `kind:tech` sends them nowhere.
        already_tried = key[1] is not None or looks_like_tech(key[0])
        hint = "" if already_tried else " [dim]— for a device, add[/] [bold]kind:tech[/]"
        return f"[yellow]no matches[/]{hint}{tail}"

    # --- capturing -----------------------------------------------------------------
    @on(OptionList.OptionSelected, "#candidates")
    def _on_pick(self, event: OptionList.OptionSelected) -> None:
        if 0 <= event.option_index < len(self._hits):
            kind, cand = self._hits[event.option_index]
            self.capture(kind, cand)
        elif self._freeform is not None and event.option_index == len(self._hits):
            self._review(self._freeform)

    def action_review(self) -> None:
        """Open the highlighted row as a draft instead of adding it outright.

        The same door for both kinds of row: a search hit arrives with its title and kind
        already filled in, a synthetic one with whatever the lineage gave up. Printable
        keys are swallowed while the bar has focus, so this only fires from the list.
        """
        index = self._candidates.highlighted
        if index is None:
            return
        if 0 <= index < len(self._hits):
            kind, cand = self._hits[index]
            parsed = query.parse(self.query_one("#add-query", Input).value)
            self._review(drafts.for_candidate(parsed.text.strip() or cand.title, kind, cand))
        elif self._freeform is not None and index == len(self._hits):
            self._review(self._freeform)

    def _review(self, draft: Draft) -> None:
        """Hand off to the review screen and adopt whatever it decides."""

        def done(entity: Entity | None) -> None:
            if entity is not None:
                self.dismiss(entity)

        self.app.push_screen(DraftScreen(draft), done)

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
        self._busy(True, f"[dim]adding “{cand.title}” — pulling dates and credits…[/]")
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
            self._busy(False, f"[red]add failed:[/] {exc}")
            return
        if entity is None:
            self._busy(False, "[yellow]not tracked[/] — unknown kind, or no canonical id to pin")
            return
        self.dismiss(entity)

    def _busy(self, flag: bool, markup: str) -> None:
        """Show a capture as running: a spinner where the result will land, and a dead bar.

        A capture is several seconds of network — a pull, then enrichment — and the only
        sign of it was a line of grey text that read much like the search line above it.
        The spinner is the widget's own `loading`, and disabling the Input says the
        keystroke would not be read anyway (it no longer cancels the write, but it would
        queue a search behind it and land on a screen that is about to close).
        """
        self._candidates.loading = flag
        self.query_one("#add-query", Input).disabled = flag
        self._status(markup)

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
