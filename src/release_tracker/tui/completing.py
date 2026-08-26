"""An Input that completes against the tracker's own vocabulary.

The query bar and the card editor want the same thing from a text field: while you type,
show what the leading candidate would make of it; on tab, take it; on tab again, walk to
the next one. They differ only in *where* the candidates come from and how an accepted one
is spliced back — the query bar replaces the token under the caret, a card field replaces
the whole value — so that difference is the one thing injected, as a ``suggester``.

The offer is never written into the value until it is taken. Typing is therefore never
fought: the only thing that changes underneath is the dim tail and whatever the owning
screen does with :class:`CompletingInput.Offered`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import ClassVar

from rich.text import Text
from textual import events
from textual.binding import Binding, BindingType
from textual.message import Message
from textual.widgets import Input

from release_tracker import query

__all__ = ["CompletingInput", "Suggester", "completion_hint", "field_suggester"]

Suggester = Callable[[str, int], Sequence[query.Suggestion]]
"""Candidates for ``(text, caret)``, best first."""

WALK_LIMIT = 40  # how many candidates tab will walk through
HINT_WIDTH = 6  # how many of them the hint line shows at once


def field_suggester(field: str, vocab: query.Vocabulary, *, limit: int = WALK_LIMIT) -> Suggester:
    """Complete a whole field value against one query field's vocabulary.

    For a field holding a single value — a credit's name, a tag, a platform — so the
    candidate replaces everything typed rather than a token within it. Scoped to the field,
    because a ``director`` box offering an actor suggests a name that provably cannot be
    right; unscoped freeform text is still accepted, and becomes a candidate itself once
    the graph has it.
    """

    def suggest(text: str, _caret: int) -> Sequence[query.Suggestion]:
        return tuple(
            query.Suggestion(
                insert=entry.value,
                label=entry.value,
                detail=f"{detail} · {entry.uses}" if entry.uses else detail,
                start=0,
                end=len(text),
            )
            for entry, detail in query.rank_values(field, vocab, text.strip().lower(), limit=limit)
        )

    return suggest


def completion_hint(picks: Sequence[query.Suggestion], active: int) -> Text:
    """The candidate strip: a window onto the list, with the active one picked out."""
    # scroll the window with the selection so a long list stays walkable
    first = max(0, min(active - HINT_WIDTH // 2, len(picks) - HINT_WIDTH))
    shown = [
        f"[reverse]{p.label}[/]" if i + first == active else f"[dim]{p.label}[/]"
        for i, p in enumerate(picks[first : first + HINT_WIDTH])
    ]
    count = f"[dim]{active + 1}/{len(picks)}[/]" if len(picks) > 1 else ""
    return Text.from_markup(f"[dim]↹[/] {'  '.join(shown)}  {count}")


@dataclass(slots=True)
class _Walk:
    """An in-progress tab walk through the completions for one token.

    Anchored to the text as *typed* (``origin``): applying a pick never moves the anchor,
    so a second tab keeps stepping through the same list rather than re-deriving from its
    own output — a fresh lookup after tab filled ``is:`` in as ``is:available`` matches
    only "available", which would collapse the list and stall the walk.

    ``value``/``caret`` are what the field looked like after the last apply; if either has
    drifted, the user typed something and the walk is over. ``chosen`` separates a pick
    the user has taken from one merely being offered as a dim tail, which is what decides
    whether the next tab steps on or takes what is on screen.
    """

    picks: tuple[query.Suggestion, ...]
    index: int
    origin: str
    origin_caret: int
    value: str
    caret: int
    chosen: bool = False


class CompletingInput(Input):
    """An Input that offers a completion as a dim tail; tab takes it, tab again walks.

    Beyond the completion itself it fixes one Textual default: ctrl+backspace deletes the
    word to the *left*, where Textual binds it to ``delete_right_word``, which is not what
    the chord means anywhere else. (It only arrives at all when the terminal disambiguates
    it — kitty keyboard protocol; alt+backspace and ctrl+w are bound alongside for the
    terminals that send ^H and cannot tell it from a plain backspace.)
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("tab", "complete(1)", "Complete", show=False),
        Binding("shift+tab", "complete(-1)", "Complete back", show=False),
        Binding(
            "ctrl+backspace,ctrl+h,alt+backspace",
            "delete_left_word",
            "Delete word left",
            show=False,
        ),
    ]

    class Offered(Message):
        """The live offer changed — what the field now stands for is ``input.effective``."""

        def __init__(self, input: CompletingInput) -> None:
            super().__init__()
            self.input = input

        @property
        def control(self) -> CompletingInput:
            return self.input

    def __init__(
        self,
        suggester: Suggester,
        *,
        value: str = "",
        placeholder: str = "",
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(value=value, placeholder=placeholder, id=id, classes=classes)
        self._suggester = suggester
        self._walk: _Walk | None = None
        self._announced: str | None = None

    # --- what the field currently stands for -------------------------------------
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

    @property
    def effective(self) -> str:
        """The value plus the completion it is offering — what the field means right now."""
        return self.ghost if self.ghosting else self.value

    @property
    def picks(self) -> tuple[query.Suggestion, ...]:
        """The candidates tab is walking, empty when there are none."""
        return self._walk.picks if self._walk is not None and self.has_focus else ()

    @property
    def index(self) -> int:
        """Which of :attr:`picks` is active."""
        return self._walk.index if self._walk is not None else 0

    def accept_ghost(self) -> bool:
        """Commit the dim tail into the value. ``True`` if there was one to commit.

        Deliberately not conditioned on focus: the last thing a blurring field does is
        accept, and by then Textual has already taken the focus away.
        """
        if not self._extends_value:
            return False
        self.value = self.ghost
        self.cursor_position = len(self.value)
        return True

    # --- the walk ----------------------------------------------------------------
    def _live_walk(self) -> _Walk | None:
        """The walk in progress, if the field still looks the way it did when we set it."""
        walk = self._walk
        if walk is None or self.value != walk.value or self.cursor_position != walk.caret:
            return None
        return walk

    def _fresh_walk(self) -> _Walk | None:
        picks = tuple(self._suggester(self.value, self.cursor_position))
        if not picks:
            return None
        return _Walk(
            picks=picks,
            index=0,
            origin=self.value,
            origin_caret=self.cursor_position,
            value=self.value,
            caret=self.cursor_position,
        )

    def _ghost_for(self, walk: _Walk) -> str | None:
        """The text the active pick would produce, when it only *extends* what was typed.

        Only an extension can be painted as the dim tail, and only what is painted is
        allowed to count — so the offer and the display are the same string or there is no
        offer at all. A pick that has to rewrite what was typed (a quoted name, a canonical
        field, a caret parked mid-text) is spliced in by tab instead.
        """
        pick = walk.picks[walk.index]
        if pick.kind != "value" or walk.origin_caret != len(walk.origin):
            return None
        candidate = query.apply(walk.origin, pick)
        extends = candidate.startswith(walk.origin) and len(candidate) > len(walk.origin)
        return candidate if extends else None

    def refresh_offer(self) -> None:
        """Derive (or keep) the walk for the text under the caret and paint its tail.

        Typing never writes into the field — a completion nobody asked for stays an offer.
        Once tab has taken a pick there is nothing left to offer: the value is there in
        full, and a tail would only repeat it in grey.
        """
        if not self.has_focus:
            # The tail stops being painted the moment focus goes, so keeping it would mean
            # standing for something invisible. Committing keeps what was on screen.
            if not self.accept_ghost():
                self.ghost = ""
                self._announce()
            return
        if (walk := self._live_walk()) is None:
            self._walk = walk = self._fresh_walk()
            if walk is None:
                self.ghost = ""
                self._announce()
                return
        self.ghost = "" if walk.chosen else (self._ghost_for(walk) or "")
        self._announce()

    def _announce(self) -> None:
        if self._announced != self.effective:
            self._announced = self.effective
            self.post_message(self.Offered(self))

    def action_complete(self, delta: int = 1) -> None:
        """Take the offered completion, then walk: tab again for the next candidate.

        Tab writes the pick into the field rather than only re-pointing the dim tail — the
        tail is a hint, and a hint you cannot pick up leaves the value to be finished by
        hand. So the first tab takes what is on screen (shift+tab takes the one before it)
        and each tab after that steps to the next candidate, rewriting in place. The caret
        lands at the end, ready to carry on typing.
        """
        if (walk := self._live_walk() or self._fresh_walk()) is None:
            self._walk = None
            return
        # An already-taken pick is stepped off; one merely offered is taken as it stands,
        # unless the walk is going backwards, which has nothing to step back onto yet.
        if walk.chosen or delta < 0:
            walk.index = (walk.index + delta) % len(walk.picks)
        walk.chosen = True
        self._walk = walk

        pick = walk.picks[walk.index]
        self.ghost = ""
        self.value = query.apply(walk.origin, pick)
        self.cursor_position = pick.start + len(pick.insert)
        walk.value, walk.caret = self.value, self.cursor_position
        # A sole completion has nowhere to cycle, so end the walk: the next tab then
        # re-derives and moves on to the next stage — `gen` -> `genre:` -> its values.
        self._walk = None if len(walk.picks) == 1 else walk
        self.refresh_offer()

    # --- events ------------------------------------------------------------------
    def on_input_changed(self) -> None:
        """Re-offer on every edit.

        Hung off the message rather than the value watcher because a watcher runs *inside*
        the assignment, before Textual has moved the caret — and an offer derived from a
        caret one character behind is no offer at all. A widget receives its own posted
        messages before they bubble, so this still lands before the owning screen sees the
        change, and whatever it does with the new value already sees the matching offer.
        """
        self.refresh_offer()

    def on_focus(self) -> None:
        """Textual clears the tail when the field takes focus — put it back afterwards.

        Deferred because this handler runs *before* `Input._on_focus` does the clearing:
        both are dispatched from the same event, subclass first.
        """
        self.call_later(self.refresh_offer)

    def on_blur(self) -> None:
        self.accept_ghost()

    async def _on_key(self, event: events.Key) -> None:
        # Space ends a token, so it reads as "take what I can see": whatever the offer was
        # driving is already on screen, and dropping it on the way to the next word would
        # contradict what was there a keystroke ago.
        if event.character == " " and self.cursor_at_end:
            self.accept_ghost()
        await super()._on_key(event)
