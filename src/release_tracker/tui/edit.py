"""The card in edit mode: every field on it, live.

Laid out like the card it covers, so `e` reads as "the same thing, but writable" rather
than as a different screen. Escape goes back to the card, not the table, so a change can
be looked at where it will be read.

**Nothing is staged.** A field commits when you leave it, the way the state toggle already
commits when you change it — no save key, no dirty state, no confirmation. What that buys
is that the rules for what a field means live in exactly one place: the field holds text,
``edits.*`` decides what that text does to the graph, and the screen only decides *when*
to ask. Every write goes through the same functions ``rdt edit …`` calls.

**Adding and removing are the same gesture as typing.** Each section ends in an empty row;
filling it in adds. Blanking a filled row removes. There is no add key and no delete key
to learn — except on the note log, whose rows are not text fields, where `d` deletes.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from release_tracker import edits, query, render
from release_tracker.db import Database
from release_tracker.models import (
    CreditRole,
    DescriptorKind,
    Entity,
    ReleaseChannel,
)
from release_tracker.tui.completing import (
    CompletingInput,
    Suggester,
    completion_hint,
    field_suggester,
)
from release_tracker.tui.cycle import Cycle
from release_tracker.views import WorkCard

if TYPE_CHECKING:  # pragma: no cover - typing only
    from release_tracker.tui.app import RdtApp

# Store and retail channels (steam, psn, amazon…) are a puller's business — they come from
# the store that owns them, and nobody hand-authors "the xbox date". These are the ones a
# person actually knows something about.
_CHANNELS: tuple[ReleaseChannel, ...] = (
    ReleaseChannel.PRIMARY,
    ReleaseChannel.THEATRICAL,
    ReleaseChannel.DIGITAL,
    ReleaseChannel.STREAMING,
    ReleaseChannel.PHYSICAL,
    ReleaseChannel.PREMIERE,
    ReleaseChannel.THEATRICAL_LIMITED,
    ReleaseChannel.TV_BROADCAST,
)
_ROLES: tuple[CreditRole, ...] = tuple(CreditRole)
_KINDS: tuple[DescriptorKind, ...] = tuple(DescriptorKind)
_DATE_HELP = "2026 · 2026-09 · 2026-Q3 · 2026-09-18 · 2027..2029 · ~ approx · ? unsure"


# --- rows ---------------------------------------------------------------------------------
class _Row(Horizontal):
    """One editable line: a label (or a picker) and a field. Rows never write anything.

    ↓/↑ are bound here rather than on the screen because the scroll container that holds
    the rows binds them too, and a widget's bindings beat its ancestors'.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("down", "screen.move(1)", "Next field", show=False),
        Binding("up", "screen.move(-1)", "Previous field", show=False),
    ]

    def __init__(self, label: str, field: Input, *, picker: Cycle | None = None) -> None:
        super().__init__(classes="edit-row")
        self.label = label
        self.field = field
        self.picker = picker
        self.original = field.value

    def compose(self) -> ComposeResult:
        if self.picker is not None:
            yield self.picker
        else:
            yield Static(Text.from_markup(f"[dim]{self.label}[/]"), classes="row-label")
        yield self.field

    def settle(self, value: str) -> None:
        """Accept ``value`` as what the row now says, so a re-commit is a no-op."""
        self.field.value = value
        self.original = value


class _CompletingRow(_Row):
    """A row whose field completes against the graph.

    ``completing`` is the same widget as ``field``, kept under a second name so the
    completion API is reachable without a cast — ``field`` stays typed as the plain Input
    every row has.
    """

    def __init__(self, label: str, field: CompletingInput, *, picker: Cycle | None = None) -> None:
        super().__init__(label, field, picker=picker)
        self.completing = field


class TitleRow(_Row):
    def __init__(self, title: str) -> None:
        super().__init__("title", Input(value=title))


class DateRow(_Row):
    """A hand-authored date for one channel, as an EDTF literal.

    ``channel`` is None on the add row, where it comes from the picker instead. The field
    holds only what a *person* authored: a pulled date shows as the placeholder, so it is
    visible without being silently frozen into a manual one by an accidental commit.
    """

    def __init__(self, channel: ReleaseChannel | None, edtf: str = "", pulled: str = "") -> None:
        picker = None if channel is not None else Cycle([c.value for c in _CHANNELS])
        super().__init__(
            channel.value if channel is not None else "",
            Input(value=edtf, placeholder=pulled or _DATE_HELP),
            picker=picker,
        )
        self.channel = channel

    @property
    def target(self) -> ReleaseChannel:
        return self.channel or ReleaseChannel(self.picker.value if self.picker else "primary")


class CreditRow(_CompletingRow):
    def __init__(self, role: CreditRole, name: str, node_id: str, suggester: Suggester) -> None:
        super().__init__(role.value, CompletingInput(suggester, value=name))
        self.role = role
        self.node_id = node_id


class CreditAddRow(_CompletingRow):
    def __init__(self, suggester: Suggester) -> None:
        picker = Cycle([r.value for r in _ROLES])
        super().__init__(
            "", CompletingInput(suggester, placeholder="credit a name…"), picker=picker
        )

    @property
    def role(self) -> CreditRole:
        return CreditRole(self.picker.value if self.picker else "other")


class TagRow(_CompletingRow):
    def __init__(self, kind: DescriptorKind, name: str, node_id: str, suggester: Suggester) -> None:
        super().__init__(kind.value, CompletingInput(suggester, value=name))
        self.kind = kind
        self.node_id = node_id


class TagAddRow(_CompletingRow):
    def __init__(self, suggester: Suggester) -> None:
        picker = Cycle([k.value for k in _KINDS])
        super().__init__("", CompletingInput(suggester, placeholder="tag it…"), picker=picker)

    @property
    def kind(self) -> DescriptorKind:
        return DescriptorKind(self.picker.value if self.picker else "genre")


class PlatformRow(_CompletingRow):
    def __init__(self, name: str, node_id: str, suggester: Suggester) -> None:
        super().__init__("on", CompletingInput(suggester, value=name, placeholder="a platform…"))
        self.node_id = node_id


class NoteAddRow(_Row):
    def __init__(self) -> None:
        super().__init__("note", Input(placeholder="add a note…"))


class NoteRow(Static):
    """One entry in the log. Not a text field — the log records what was written, so `d`
    drops an entry rather than rewriting history."""

    can_focus = True

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("down", "screen.move(1)", "Next field", show=False),
        Binding("up", "screen.move(-1)", "Previous field", show=False),
        Binding("d", "screen.remove_note", "Delete note", show=False),
    ]

    def __init__(self, note_id: int, when: str, body: str) -> None:
        super().__init__(Text.from_markup(f"[dim]{when}[/]  {body}"), classes="note-row")
        self.note_id = note_id


# --- the screen ---------------------------------------------------------------------------
class EditScreen(ModalScreen[None]):
    """The card, writable. Dismisses when escape takes you back to it."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Back to the card", show=False),
    ]

    def __init__(self, entity: Entity, card: WorkCard) -> None:
        super().__init__()
        self.entity = entity
        self.card = card

    @property
    def rdt(self) -> RdtApp:
        from release_tracker.tui.app import RdtApp

        assert isinstance(self.app, RdtApp)
        return self.app

    @property
    def db(self) -> Database:
        return self.rdt.db

    def _credit_suggester(self, role: CreditRole) -> Suggester:
        """Names already credited in this role, widening to everyone if it is empty.

        Role-scoped first because that is where the answer usually is — and a `director`
        box offering an actor suggests a name that provably cannot be right. But a role
        nobody holds yet would then offer nothing at all, and the first composer you add
        is exactly when you want the name you already typed somewhere else.
        """

        def suggest(text: str, caret: int) -> Sequence[query.Suggestion]:
            vocab = self.rdt.snapshot.vocab
            scoped = field_suggester(role.value, vocab)(text, caret)
            return scoped or field_suggester("person", vocab)(text, caret)

        return suggest

    def _suggester(self, field: str) -> Suggester:
        """Candidates for one query field, read from whatever the vocabulary is *now*.

        Late-bound on purpose: adding a credit rebuilds the snapshot, and a field holding
        a closure over the old vocabulary would go on offering the old names.
        """

        def suggest(text: str, caret: int) -> Sequence[query.Suggestion]:
            return field_suggester(field, self.rdt.snapshot.vocab)(text, caret)

        return suggest

    # --- layout ------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Vertical(id="edit"):
            yield Static(
                Text.from_markup(f"[bold]{self.entity.title}[/]  [dim]edit[/]"), id="edit-title"
            )
            with VerticalScroll(id="edit-body", can_focus=False):
                yield from self._rows()
            yield Static("", id="edit-hint")
            yield Static(
                Text.from_markup(
                    "[dim]↹ complete · ↓↑ field · empty a row to remove it · d drops a note "
                    "· esc back[/]"
                ),
                id="edit-foot",
            )
            yield Static("", id="edit-status")

    def _rows(self) -> ComposeResult:
        yield TitleRow(self.entity.title)

        yield Static(Text.from_markup("[bold]Dates[/]"), classes="edit-head")
        authored = edits.manual_dates(self.db, self.entity.id)
        for est in self.card.estimates:
            yield DateRow(
                est.channel,
                authored.get(est.channel, ""),
                pulled=render.fmt_when(est.release_date, est.precision),
            )
        for channel, edtf in authored.items():  # authored on a channel with no estimate yet
            if all(e.channel is not channel for e in self.card.estimates):
                yield DateRow(channel, edtf)
        yield DateRow(None)

        yield Static(Text.from_markup("[bold]Who[/]"), classes="edit-head")
        for credit in self.card.credits:
            yield CreditRow(
                credit.role, credit.name, credit.node_id, self._credit_suggester(credit.role)
            )
        yield CreditAddRow(self._credit_suggester(_ROLES[0]))

        yield Static(Text.from_markup("[bold]Where[/]"), classes="edit-head")
        for platform in self.card.platforms:
            yield PlatformRow(platform.name, platform.node_id, self._suggester("platform"))
        yield PlatformRow("", "", self._suggester("platform"))

        yield Static(Text.from_markup("[bold]What[/]"), classes="edit-head")
        for tag in self.card.tags:
            yield TagRow(tag.kind, tag.name, tag.node_id, self._suggester(tag.kind.value))
        yield TagAddRow(self._suggester("tag"))

        yield Static(Text.from_markup("[bold]Notes[/]"), classes="edit-head")
        for line in self.db.iter_notes(self.entity.id):
            yield NoteRow(line.id, line.created.isoformat(), line.body)
        yield NoteAddRow()

    def on_mount(self) -> None:
        self.query_one(TitleRow).field.focus()

    # --- feedback ----------------------------------------------------------------
    def _say(self, markup: str) -> None:
        self.query_one("#edit-status", Static).update(Text.from_markup(markup))

    def _after(self, *, graph: bool, message: str) -> None:
        """Announce a write and let the rest of the app catch up with it."""
        self.rdt.after_edit(self.entity, graph=graph)
        self._say(message)

    @on(Cycle.Changed)
    def _on_picker(self, event: Cycle.Changed) -> None:
        """A picker beside a field decides what that field completes against."""
        match event.cycle.parent:
            case CreditAddRow() as row:
                row.completing.candidates = self._credit_suggester(row.role)
            case TagAddRow() as row:
                row.completing.candidates = self._suggester(row.kind.value)
            case _:
                pass

    @on(CompletingInput.Offered)
    def _on_offer(self, event: CompletingInput.Offered) -> None:
        field = event.input
        hint = self.query_one("#edit-hint", Static)
        hint.update(completion_hint(field.picks, field.index) if field.picks else "")

    def on_descendant_focus(self) -> None:
        if not isinstance(self.focused, CompletingInput):
            self.query_one("#edit-hint", Static).update("")

    # --- committing --------------------------------------------------------------
    @on(Input.Submitted)
    @on(Input.Blurred)
    def _on_field_done(self, event: Input.Submitted | Input.Blurred) -> None:
        if isinstance(row := event.input.parent, _Row):
            self._commit(row)

    def _commit(self, row: _Row) -> None:
        """Apply what a row now says. A no-op when it says what it said before, so the
        explicit flush on escape cannot double-write."""
        value = row.field.value.strip()
        if value == row.original.strip():
            return
        match row:
            case TitleRow():
                self._rename(row, value)
            case DateRow():
                self._date(row, value)
            case CreditRow():
                self._credit(row, value)
            case CreditAddRow():
                self._add_credit(row, value)
            case TagRow():
                self._tag(row, value)
            case TagAddRow():
                self._add_tag(row, value)
            case PlatformRow():
                self._platform(row, value)
            case NoteAddRow():
                self._add_note(row, value)
            case _:  # pragma: no cover - every row type is handled above
                pass

    def _rename(self, row: TitleRow, value: str) -> None:
        if not value:
            row.settle(self.entity.title)  # a work has to be called something
            return
        self.entity = edits.rename(self.db, self.entity, value)
        row.settle(value)
        self.query_one("#edit-title", Static).update(
            Text.from_markup(f"[bold]{value}[/]  [dim]edit[/]")
        )
        self._after(graph=True, message=f"renamed to [bold]{value}[/]")

    def _date(self, row: DateRow, value: str) -> None:
        if not value:
            edits.clear_date(self.db, self.entity, row.target)
            row.settle("")
            self._after(graph=False, message=f"cleared the {row.target.value} date")
            return
        try:
            written = edits.set_date(self.db, self.entity, row.target, value)
        except ValueError as exc:
            self._say(f"[red]{exc}[/]  [dim]{_DATE_HELP}[/]")
            return
        row.settle(written.edtf)  # echo the canonical literal back
        if row.channel is None:  # the add row: leave a real one behind and reset
            self.mount(DateRow(written.channel, written.edtf), before=row)
            row.settle("")
        self._after(
            graph=False,
            message=f"{written.channel.value} → [bold]{written.edtf}[/] "
            f"[dim]{render.fmt_when(written.when, written.precision)} · "
            f"{written.certainty.value}[/]",
        )

    def _credit(self, row: CreditRow, value: str) -> None:
        edits.remove_credits(self.db, self.entity, [row.node_id])
        if not value:
            row.display = False
            self._after(graph=True, message=f"dropped {row.original}")
            return
        row.node_id = edits.add_credit(self.db, self.entity, value, row.role).id
        row.settle(value)
        self._after(graph=True, message=f"{row.role.value} → [bold]{value}[/]")

    def _add_credit(self, row: CreditAddRow, value: str) -> None:
        if not value:
            return
        role = row.role
        node = edits.add_credit(self.db, self.entity, value, role)
        self.mount(CreditRow(role, value, node.id, self._credit_suggester(role)), before=row)
        row.settle("")
        self._after(graph=True, message=f"{role.value} → [bold]{value}[/]")

    def _tag(self, row: TagRow, value: str) -> None:
        edits.remove_tags(self.db, self.entity, [row.node_id])
        if not value:
            row.display = False
            self._after(graph=True, message=f"untagged {row.original}")
            return
        row.node_id = edits.add_tag(self.db, self.entity, value, row.kind).id
        row.settle(value)
        self._after(graph=True, message=f"{row.kind.value} → [bold]{value}[/]")

    def _add_tag(self, row: TagAddRow, value: str) -> None:
        if not value:
            return
        kind = row.kind
        node = edits.add_tag(self.db, self.entity, value, kind)
        self.mount(TagRow(kind, value, node.id, self._suggester(kind.value)), before=row)
        row.settle("")
        self._after(graph=True, message=f"{kind.value} → [bold]{value}[/]")

    def _platform(self, row: PlatformRow, value: str) -> None:
        if row.node_id:
            edits.remove_platforms(self.db, self.entity, [row.node_id])
        if not value:
            row.display = False
            self._after(graph=True, message=f"dropped {row.original}")
            return
        node = edits.add_platform(self.db, self.entity, value)
        if not row.node_id:  # the add row: leave a real one behind and reset
            self.mount(PlatformRow(value, node.id, self._suggester("platform")), before=row)
            row.settle("")
        else:
            row.node_id = node.id
            row.settle(value)
        self._after(graph=True, message=f"available on [bold]{value}[/]")

    def _add_note(self, row: NoteAddRow, value: str) -> None:
        if not value:
            return
        self.db.add_note(self.entity.id, value)
        line = self.db.iter_notes(self.entity.id)[0]
        self.mount(NoteRow(line.id, line.created.isoformat(), line.body), before=row)
        row.settle("")
        self._after(graph=False, message="noted")

    # --- actions -----------------------------------------------------------------
    def action_move(self, delta: int) -> None:
        if delta > 0:
            self.focus_next()
        else:
            self.focus_previous()

    def action_remove_note(self) -> None:
        if isinstance(row := self.focused, NoteRow):
            self.db.delete_note(row.note_id)
            row.display = False
            self.focus_next()
            self._after(graph=False, message="note dropped")

    def action_close(self) -> None:
        """Commit what is under the cursor before leaving.

        Blur would commit it too, but blur arrives as a message — by the time it landed
        the screen would be gone and the card would already have re-read without it.
        """
        if isinstance(row := getattr(self.focused, "parent", None), _Row):
            self._commit(row)
        self.dismiss()
