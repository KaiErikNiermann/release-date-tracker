"""The work card: everything known about one title, with an auto-applying state toggle.

The toggle is what the modal exists for, so it takes focus on mount — open a row, change
your mind about it, escape. Changes debounce rather than requiring a confirm keypress,
and escaping before the debounce fires flushes it, so a fast decision is never lost.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Static

from release_tracker import render, views
from release_tracker.links import SourceAccess
from release_tracker.models import ConsumptionState, Entity
from release_tracker.pipeline import refresh_entity
from release_tracker.tui.cycle import Cycle
from release_tracker.views import DateChange, WorkCard

if TYPE_CHECKING:  # pragma: no cover - typing only
    from release_tracker.tui.app import RdtApp

_STATES = tuple(ConsumptionState)
_DEBOUNCE = 0.4


class StateToggle(Cycle):
    """The consumption states, all six on one line. Emits Changed; never writes anything.

    A :class:`Cycle` that draws itself as a strip rather than one value at a time: the
    whole scale is short enough to show at once, and seeing where `watching` sits between
    `want` and `watched` is most of what the toggle is for.
    """

    class Changed(Cycle.Changed):
        """The state moved. A Cycle.Changed that also carries the enum member itself."""

        def __init__(self, toggle: StateToggle, state: ConsumptionState) -> None:
            super().__init__(toggle, state.value)
            self.state = state

    def __init__(self, state: ConsumptionState) -> None:
        super().__init__([s.value for s in _STATES], index=_STATES.index(state))

    @property
    def state(self) -> ConsumptionState:
        return _STATES[self.index]

    def render(self) -> Text:
        cells = [
            f"[reverse bold] {s.value} [/]" if i == self.index else f"[dim] {s.value} [/]"
            for i, s in enumerate(_STATES)
        ]
        return Text.from_markup("".join(cells))

    def watch_index(self) -> None:
        self.refresh()
        self.post_message(self.Changed(self, self.state))


class CardScreen(ModalScreen[ConsumptionState | None]):
    """One work, full detail. Dismisses with the state it settled on (or None)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("e", "edit", "Edit", show=False),
        Binding("u", "update", "Re-pull the automatic sources", show=False),
        Binding("escape", "close", "Back", show=False),
        Binding("q", "close", "Back", show=False),
    ]

    def __init__(self, entity: Entity, card: WorkCard) -> None:
        super().__init__()
        self.entity = entity
        self.card = card
        self._pending: ConsumptionState | None = None
        self._timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="card"):
            yield Static(Text.from_markup(self._title()), id="card-title")
            yield StateToggle(self.entity.consumption_state)
            with VerticalScroll(id="card-body"):
                yield Static(Text.from_markup(self._body()), id="card-detail")
            yield Static(
                Text.from_markup(
                    "[dim]←/→ change state (auto-saves) · u update · e edit · esc back[/]"
                ),
                id="card-foot",
            )

    def on_mount(self) -> None:
        self.query_one(StateToggle).focus()  # the toggle is why this modal exists

    def _title(self) -> str:
        parts = [self.entity.title, f"[dim]{self.entity.kind.value}[/]"]
        if self.card.series:
            parts.append(f"[dim]· {', '.join(self.card.series)}[/]")
        return "  ".join(parts)

    def _body(self) -> str:
        lines: list[str] = []
        for est in self.card.estimates:
            cell = views.DateCell(
                when=est.release_date,
                precision=est.precision,
                confirmed=est.certainty.value == "confirmed",
                end=est.date_end,
            )
            lines.append(
                f"  [bold]{est.channel.value:<20}[/] {render.fmt_cell(cell)} "
                f"[dim]{est.region}  {render.provenance(est)}[/]"
            )
        if not lines:
            lines.append("  [dim]no dates yet[/]")
        sections: list[tuple[str, list[str]]] = [
            ("Dates", lines),
            ("Who", [f"  [dim]{c.role.value:<18}[/] {c.name}" for c in self.card.credits]),
            (
                "Where",
                [
                    f"  {p.name}{' [dim](predicted)[/]' if p.predicted else ''}"
                    for p in self.card.platforms
                ],
            ),
            ("What", [f"  {render.fmt_tag(t)} [dim]{t.kind.value}[/]" for t in self.card.tags]),
        ]
        if self.card.blockers:
            sections.append(
                (
                    "Blocked by",
                    [f"  [red]{b.name}[/] [dim]{b.status}[/]" for b in self.card.blockers],
                )
            )
        if self.card.sources:
            sections.append(
                (
                    "Sources",
                    [
                        *[render.fmt_source(link) for link in self.card.sources],
                        f"  {render.source_legend()}",
                    ],
                )
            )
        if self.card.derived_from or self.card.derivatives:
            related = [
                f"  [dim]{r.relation.value}[/] {r.node.name}" for r in self.card.derived_from
            ]
            related += [
                f"  [dim]-> {r.relation.value}[/] {r.node.name}" for r in self.card.derivatives
            ]
            sections.append(("Related", related))
        out: list[str] = []
        for heading, body in sections:
            if body:
                out.append(f"[bold]{heading}[/]")
                out.extend(body)
                out.append("")
        return "\n".join(out)

    # --- debounced auto-apply ------------------------------------------------------
    def on_state_toggle_changed(self, event: StateToggle.Changed) -> None:
        self._pending = event.state
        if self._timer is not None:
            self._timer.stop()  # each keypress restarts the clock
        self._timer = self.set_timer(_DEBOUNCE, self._commit)

    def _commit(self) -> None:
        self._timer = None
        if self._pending is None or self._pending is self.entity.consumption_state:
            return
        self.app.call_later(self._write, self._pending)

    def _write(self, state: ConsumptionState) -> None:
        self.rdt.set_state(self.entity, state)
        self.entity = self.entity.model_copy(update={"consumption_state": state})
        self._pending = None

    def action_edit(self) -> None:
        """Open the same card, writable. Coming back re-reads it, so the change is there.

        The card is a snapshot taken when it opened; an edit screen over it can change
        anything on it, and a stale card is worse than no card — hence the full re-read
        rather than patching whichever field was touched.
        """
        from release_tracker.tui.edit import EditScreen

        def reread(_: None) -> None:
            self._reread()

        self.app.push_screen(EditScreen(self.entity, self.card), reread)

    def _open_edit(self, changes: Sequence[DateChange]) -> None:
        """The edit form, told what a refresh just moved. Same screen `e` opens."""
        from release_tracker.tui.edit import EditScreen

        def reread(_: None) -> None:
            self._reread()

        self.app.push_screen(EditScreen(self.entity, self.card, changes), reread)

    def _reread(self) -> bool:
        """Re-read the work from the db and repaint. False if it is gone.

        The card is a snapshot taken when it opened, so anything that can change the work
        underneath it — an edit screen, a re-pull — ends here rather than patching whichever
        field it happened to touch.
        """
        if (entity := self.rdt.db.get_entity(self.entity.id)) is None:
            self.dismiss(self.entity.consumption_state)  # deleted out from under us
            return False
        self.entity = entity
        self.card = views.work_card(self.rdt.db, entity)
        self.query_one("#card-title", Static).update(Text.from_markup(self._title()))
        self.query_one("#card-detail", Static).update(Text.from_markup(self._body()))
        return True

    def action_update(self) -> None:
        """Re-pull every source the Sources section marks as automatic, then open the form."""
        if not any(link.access is SourceAccess.AUTO for link in self.card.sources):
            self.app.notify("Nothing here updates automatically — open a link instead.")
            return
        self._refetch()

    # Its own group, never the default one. Nothing else on this card spawns a worker yet, so
    # today it changes nothing — but an exclusive worker cancels its whole group, and that is
    # precisely how the add screen once had a keystroke abort a half-finished write.
    @work(exclusive=True, group="card-update")
    async def _refetch(self) -> None:
        """Run the same refresh ``rdt refresh`` runs, then hand the result to the edit form.

        Deliberately the full thing — Tier-0 *and* the offer scan — rather than the bare pull
        this used to do. An "update" that quietly skipped a source is the kind of difference
        nobody notices until a date is wrong, and one card can afford the extra seconds.

        The spinner sits on the card body because that is where the new dates land. Every
        failure is caught and shown: a dead provider must leave the card usable, not tear
        the screen down.
        """
        body = self.query_one("#card-body", VerticalScroll)
        body.loading = True
        try:
            result = await refresh_entity(
                self.rdt.db, self.rdt.settings, self.entity, client=await self.rdt.http()
            )
        except Exception as exc:
            body.loading = False
            self.app.notify(f"Update failed: {exc}", severity="error")
            return
        body.loading = False
        if result.error is not None:
            self.app.notify(f"Update failed: {result.error}", severity="error")
            return
        if not self._reread():
            return
        # the row behind the modal is showing the old date until this lands
        self.rdt.after_edit(self.entity, graph=False)
        # Straight into the form rather than a notification. The write has already happened
        # — a pull can't overwrite a hand-authored date, only outrank it — so this is where
        # you see what moved, and what is now being shown instead of what you typed.
        self._open_edit(views.diff_estimates(result.before, result.after))

    @property
    def rdt(self) -> RdtApp:
        from release_tracker.tui.app import RdtApp

        assert isinstance(self.app, RdtApp)
        return self.app

    def action_close(self) -> None:
        # Flush before leaving: escaping right after a toggle must still save.
        if self._timer is not None:
            self._timer.stop()
            self._commit()
        self.dismiss(self.entity.consumption_state)
