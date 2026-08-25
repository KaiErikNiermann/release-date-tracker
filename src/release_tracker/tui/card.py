"""The work card: everything known about one title, with an auto-applying state toggle.

The toggle is what the modal exists for, so it takes focus on mount — open a row, change
your mind about it, escape. Changes debounce rather than requiring a confirm keypress,
and escaping before the debounce fires flushes it, so a fast decision is never lost.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static

from release_tracker import render, views
from release_tracker.models import ConsumptionState, Entity
from release_tracker.views import WorkCard

_STATES = tuple(ConsumptionState)
_DEBOUNCE = 0.4


class StateToggle(Widget):
    """A horizontal strip of consumption states. Emits Changed; never writes anything."""

    can_focus = True
    index: reactive[int] = reactive(0)

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("left,h", "step(-1)", "Prev", show=False),
        Binding("right,l", "step(1)", "Next", show=False),
    ]

    class Changed(Message):
        def __init__(self, state: ConsumptionState) -> None:
            super().__init__()
            self.state = state

    def __init__(self, state: ConsumptionState) -> None:
        super().__init__()
        self.index = _STATES.index(state)

    def render(self) -> Text:
        cells = [
            f"[reverse bold] {s.value} [/]" if i == self.index else f"[dim] {s.value} [/]"
            for i, s in enumerate(_STATES)
        ]
        return Text.from_markup("".join(cells))

    def action_step(self, delta: int) -> None:
        self.index = (self.index + delta) % len(_STATES)

    def watch_index(self, index: int) -> None:
        self.refresh()
        self.post_message(self.Changed(_STATES[index]))


class CardScreen(ModalScreen[ConsumptionState | None]):
    """One work, full detail. Dismisses with the state it settled on (or None)."""

    BINDINGS: ClassVar[list[BindingType]] = [
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
                Text.from_markup("[dim]←/→ change state (auto-saves) · esc back[/]"),
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
                f"[dim]{est.region}  conf {est.confidence:.2f}[/]"
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
        from release_tracker.tui.app import RdtApp

        assert isinstance(self.app, RdtApp)
        self.app.set_state(self.entity, state)
        self.entity = self.entity.model_copy(update={"consumption_state": state})
        self._pending = None

    def action_close(self) -> None:
        # Flush before leaving: escaping right after a toggle must still save.
        if self._timer is not None:
            self._timer.stop()
            self._commit()
        self.dismiss(self.entity.consumption_state)
