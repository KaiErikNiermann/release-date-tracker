"""A ←/→ picker over a fixed list of values.

The tracker's enums are short and closed — six consumption states, sixteen credit roles,
five descriptor kinds — so picking one is a step along a list rather than a search through
one. Extracted from the card's state toggle because the card editor needs the same
behaviour for roles, kinds and channels, and only the drawing differs: a handful of states
fit on one line as a strip, sixteen roles do not.

Like the toggle it came from, it emits :class:`Cycle.Changed` and never writes anything.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from rich.text import Text
from textual.binding import Binding, BindingType
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget

__all__ = ["Cycle"]


class Cycle(Widget):
    """Step through ``values`` with ←/→. Renders as ``◂ value ▸`` unless told otherwise."""

    can_focus = True
    index: reactive[int] = reactive(0)

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("left,h", "step(-1)", "Prev", show=False),
        Binding("right,l", "step(1)", "Next", show=False),
    ]

    class Changed(Message):
        """The selection moved. ``value`` is the label now showing."""

        def __init__(self, cycle: Cycle, value: str) -> None:
            super().__init__()
            self.cycle = cycle
            self.value = value

        @property
        def control(self) -> Cycle:
            return self.cycle

    def __init__(self, values: Sequence[str], *, index: int = 0, id: str | None = None) -> None:
        super().__init__(id=id)
        self.values = tuple(values)
        self.index = index

    @property
    def value(self) -> str:
        """The label currently selected."""
        return self.values[self.index]

    def render(self) -> Text:
        return Text.from_markup(f"[dim]◂[/] {self.value} [dim]▸[/]")

    def action_step(self, delta: int) -> None:
        self.index = (self.index + delta) % len(self.values)

    def watch_index(self) -> None:
        self.refresh()
        self.post_message(self.Changed(self, self.value))
