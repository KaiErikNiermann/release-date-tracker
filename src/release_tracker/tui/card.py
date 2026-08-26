"""The work card: everything known about one title, with an auto-applying state toggle.

The toggle is what the modal exists for, so it takes focus on mount — open a row, change
your mind about it, escape. Changes debounce rather than requiring a confirm keypress,
and escaping before the debounce fires flushes it, so a fast decision is never lost.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Static

from release_tracker import render, views
from release_tracker.models import ConsumptionState, Entity
from release_tracker.tui.cycle import Cycle
from release_tracker.views import WorkCard

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
                Text.from_markup("[dim]←/→ change state (auto-saves) · e edit · esc back[/]"),
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
            if (entity := self.rdt.db.get_entity(self.entity.id)) is None:
                self.dismiss(self.entity.consumption_state)  # deleted out from under us
                return
            self.entity = entity
            self.card = views.work_card(self.rdt.db, entity)
            self.query_one("#card-title", Static).update(Text.from_markup(self._title()))
            self.query_one("#card-detail", Static).update(Text.from_markup(self._body()))

        self.app.push_screen(EditScreen(self.entity, self.card), reread)

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
