"""The text field every screen types into.

Textual's ``Input`` binds ``ctrl+backspace`` and ``alt+backspace`` to
``delete_right_word`` — backwards from what those chords mean in a shell, a browser or an
editor, and from Textual's own ``ctrl+w``. At the end of a line, which is where a query
is usually being edited, deleting rightward has nothing to delete, so the chord reads as
dead rather than as wrong.

So every field in the TUI is this rather than a bare ``Input``. It only rebinds; the
completion behaviour is :class:`~release_tracker.tui.completing.CompletingInput`, which
builds on this so the two never drift apart.
"""

from __future__ import annotations

from typing import ClassVar

from textual.binding import Binding, BindingType
from textual.widgets import Input

__all__ = ["TextInput"]


class TextInput(Input):
    """An ``Input`` whose word-delete chords delete the word behind the caret.

    Three chords for one gesture because the terminal decides which of them arrives:
    ``ctrl+backspace`` only when the kitty keyboard protocol is disambiguating it, and
    ``ctrl+h`` for the terminals that send ``^H`` and cannot tell it from a plain
    backspace. ``alt+backspace`` is the one that works everywhere.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding(
            "ctrl+backspace,ctrl+h,alt+backspace",
            "delete_left_word",
            "Delete word left",
            show=False,
        ),
    ]
