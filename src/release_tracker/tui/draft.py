"""Read the entry before it exists — the step between picking something and tracking it.

The add screen's enter still writes immediately, because for a search hit with a canonical
id there is nothing to second-guess. This is the other door: it shows what *would* be
written, lets it be corrected, and only then commits.

It earns its place on synthetic entries. Everything about "Steam Deck 2" is inferred — that
it is tech, which sort of tech, which product it follows — and inference at that confidence
belongs in front of a person rather than behind a spinner. The category is the field that
proves it: a line can change category between generations, so a handheld's successor may not
be a handheld, and no amount of pattern-tuning over the name sees that coming.

Rows are :mod:`release_tracker.tui.edit`'s, so the form reads the same as editing an entry
that already exists. The screen is not, though: ``EditScreen`` writes through to the database
on every field, which is exactly what must not happen to something that hasn't been agreed
to yet. This one holds a :class:`~release_tracker.drafts.Draft` in memory and writes once.
"""

from __future__ import annotations

from dataclasses import replace
from typing import ClassVar

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from release_tracker import drafts
from release_tracker.drafts import Draft
from release_tracker.models import Entity, MediaKind
from release_tracker.tech import TechCategory
from release_tracker.titles import DEFAULT_PART_LABEL
from release_tracker.tui.cycle import Cycle
from release_tracker.tui.edit import DATE_HELP, Row, TitleRow


def _num(value: int | None) -> str:
    """A coordinate as the form shows it — blank rather than "None" when it is unset."""
    return "" if value is None else str(value)


_KINDS: tuple[MediaKind, ...] = tuple(MediaKind)
_CATEGORIES: tuple[TechCategory, ...] = tuple(TechCategory)


class _PickerRow(Horizontal):
    """A label and a ←/→ picker. The edit screen's picker rows carry a field too; these
    are the whole choice, so there is nothing to type into."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("down", "screen.move(1)", "Next field", show=False),
        Binding("up", "screen.move(-1)", "Previous field", show=False),
    ]

    def __init__(self, label: str, values: tuple[str, ...], current: str) -> None:
        super().__init__(classes="edit-row")
        self.label = label
        self.cycle = Cycle(values, index=values.index(current) if current in values else 0)

    def compose(self) -> ComposeResult:
        yield Static(Text.from_markup(f"[dim]{self.label}[/]"), classes="row-label")
        yield self.cycle


class DraftScreen(ModalScreen[Entity | None]):
    """Review a proposed entry, then add it. Dismisses with the entity, or None if dropped."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "back", "Back", show=False),
        Binding("ctrl+s", "commit", "Add it", show=False),
    ]

    def __init__(self, draft: Draft) -> None:
        super().__init__()
        self.draft = draft

    # --- layout ---------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Vertical(id="draft"):
            kind = "new entry" if self.draft.synthetic else "from search"
            yield Static(
                Text.from_markup(f"[bold]{self.draft.title}[/]  [dim]review — {kind}[/]"),
                id="draft-title",
            )
            with VerticalScroll(id="draft-body", can_focus=False):
                yield TitleRow(self.draft.title)
                yield _PickerRow("kind", tuple(k.value for k in _KINDS), self.draft.kind.value)
                yield _PickerRow(
                    "category",
                    tuple(c.value for c in _CATEGORIES),
                    (self.draft.category or TechCategory.OTHER).value,
                )
                yield Row("date", Input(value=self.draft.edtf, placeholder=DATE_HELP))
                yield Row("season", Input(value=_num(self.draft.season), placeholder="e.g. 2"))
                yield Row("part", Input(value=_num(self.draft.part), placeholder="e.g. 1"))
                yield Row(
                    "part label",
                    Input(value=self.draft.part_label or "", placeholder=DEFAULT_PART_LABEL),
                )
            # Docked as one container — see the note in `edit.py`; docked siblings overlap.
            with Vertical(id="draft-chrome"):
                yield Static(self._provenance(), id="draft-lineage")
                yield Static(
                    Text.from_markup(
                        "[dim]↓↑ field · ←→ change · ctrl+s adds it · esc drops it[/]"
                    ),
                    id="draft-foot",
                )
                yield Static("", id="draft-status")

    def _provenance(self) -> Text:
        """Where every prefilled value came from, stated plainly so a wrong guess is visible.

        The whole licence for guessing eagerly. A prefill costs one ←→ to correct *if* you can
        see it is a guess and what it was read off; unattributed, it is something you have to
        re-derive to trust, and then it has saved nothing.
        """
        lines = [f"[dim]· {reason}[/]" for reason in self.draft.reasons]
        if (line := self._lineage()) is not None:
            lines.append(line)
        return Text.from_markup("\n".join(lines))

    def _lineage(self) -> str | None:
        """The device line: what family this was positioned against, or that there was none."""
        predecessor = self.draft.predecessor
        if predecessor is None:
            if self.draft.synthetic and self.draft.version is not None:
                return (
                    "[dim]No lineage found — nothing to infer from, so check the fields above.[/]"
                )
            return None
        when = f" [dim]({predecessor.released.isoformat()})[/]" if predecessor.released else ""
        what = f"  [dim]{predecessor.instance_of}[/]" if predecessor.instance_of else ""
        maker = f"  [dim]· {predecessor.brand}[/]" if predecessor.brand else ""
        return f"[dim]follows[/] [bold]{predecessor.label}[/]{when}{what}{maker}"

    def on_mount(self) -> None:
        self._sync_rows()
        self.query_one(TitleRow).field.focus()

    # --- state ----------------------------------------------------------------------
    @property
    def _rows(self) -> list[Row | _PickerRow]:
        body = self.query_one("#draft-body", VerticalScroll)
        return [w for w in body.children if isinstance(w, Row | _PickerRow)]

    def picker(self, label: str) -> _PickerRow:
        """The picker row under ``label``. Public because the form's state *is* its rows —
        there is no separate model to inspect, so a caller checking what the screen would
        commit has nowhere else to look."""
        return next(r for r in self._rows if isinstance(r, _PickerRow) and r.label == label)

    def _row(self, label: str) -> Row | _PickerRow:
        """Any row by label, picker or field alike — what `_KIND_ROWS` hides and shows."""
        return next(r for r in self._rows if r.label == label)

    # Rows that only make sense for one kind, and the kind that owns them. Category is a tech
    # idea; season and part are TV coordinates. Driven off one table rather than a branch per
    # row, so the next kind-specific field is a line here and nothing else.
    _KIND_ROWS: ClassVar[dict[str, MediaKind]] = {
        "category": MediaKind.TECH,
        "season": MediaKind.TV,
        "part": MediaKind.TV,
        "part label": MediaKind.TV,
    }

    def _sync_rows(self) -> None:
        """Show only the rows the chosen kind actually has."""
        kind = self.picker("kind").cycle.value
        for label, owner in self._KIND_ROWS.items():
            self._row(label).display = kind == owner.value

    @on(Cycle.Changed)
    def _on_picker(self, event: Cycle.Changed) -> None:
        del event
        self._sync_rows()

    def _gather(self) -> Draft:
        """The draft as the form now reads it.

        A field the current kind does not own is read as unset, never as whatever it held
        before. Otherwise a `season:2` prefill would survive being re-kinded to `movie` — off
        screen, so nobody could see it — and land as a coordinate on a film.
        """
        title = self.query_one(TitleRow).field.value.strip() or self.draft.title
        kind = MediaKind(self.picker("kind").cycle.value)
        category = (
            TechCategory(self.picker("category").cycle.value) if kind is MediaKind.TECH else None
        )
        tv = kind is MediaKind.TV
        return replace(
            self.draft,
            title=title,
            kind=kind,
            category=category,
            edtf=self._value("date"),
            season=self._coord("season") if tv else None,
            part=self._coord("part") if tv else None,
            # Blank means "Part" rather than "unnamed" — the default is what a cut is called
            # when nobody said otherwise, not the absence of a name.
            part_label=(self._value("part label").strip() or None) if tv else None,
        )

    def _value(self, label: str) -> str:
        row = self._row(label)
        return row.field.value if isinstance(row, Row) else ""

    def _coord(self, label: str) -> int | None:
        """A season/part box as a number. Anything that is not one reads as unset — the form
        has no error channel, and a stray character must not cost the whole entry."""
        raw = self._value(label).strip()
        return int(raw) if raw.isdigit() else None

    # --- committing -----------------------------------------------------------------
    @on(Input.Submitted)
    def _on_submit(self) -> None:
        self.action_commit()

    def action_commit(self) -> None:
        self.draft = self._gather()
        self.commit()

    @work(exclusive=True, group="draft-commit")
    async def commit(self) -> None:
        """Its own worker group, for the same reason the add screen's capture has one: this
        is a write, and a stray keystroke must not cancel it half-done."""
        from release_tracker.tui.app import RdtApp

        assert isinstance(self.app, RdtApp)
        self._busy(True, f"[dim]adding “{self.draft.title}”…[/]")
        try:
            entity = await drafts.commit(
                self.app.db, self.app.settings, self.draft, await self.app.http()
            )
        except Exception as exc:
            self._busy(False, f"[red]add failed:[/] {exc}")
            return
        if entity is None:
            self._busy(False, "[yellow]not tracked[/] — no canonical id to pin")
            return
        self.dismiss(entity)

    def _busy(self, flag: bool, markup: str) -> None:
        self.query_one("#draft-body", VerticalScroll).disabled = flag
        self.query_one("#draft-status", Static).update(Text.from_markup(markup))

    # --- movement -------------------------------------------------------------------
    def action_move(self, delta: int) -> None:
        """↓/↑ walk the form, skipping whatever the current kind has hidden."""
        rows = [r for r in self._rows if r.display]
        focused = next(
            (i for i, row in enumerate(rows) if row.has_focus_within),
            0,
        )
        target = rows[max(0, min(focused + delta, len(rows) - 1))]
        match target:
            case _PickerRow():
                target.cycle.focus()
            case _:
                target.field.focus()

    def action_back(self) -> None:
        self.dismiss(None)
