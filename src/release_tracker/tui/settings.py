"""Somewhere to put a key, and an answer to "why didn't it take effect".

Before this, configuring `rdt` meant hand-authoring a file nothing creates, in a directory
nothing makes, described only in a `.env.example` that an installed copy of the tool does
not ship. This is the same settings the environment carries, editable in place.

Two things it refuses to do. It never blocks on verification — a provider outage or an
aeroplane would otherwise stop you saving a key you know is good, which is worse than
storing a typo you can correct. And it never hides that the environment outranks the file:
a variable exported in your shell wins over anything typed here, and a screen that let you
type into a value that could not take effect would be lying about what it does.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import SecretStr, ValidationError
from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from release_tracker import config_file
from release_tracker.config import Settings, field_name_for, get_settings, secret
from release_tracker.config_file import FIELD_DOCS, FieldDoc
from release_tracker.credentials import CheckResult, check_tmdb, check_twitch
from release_tracker.tui.edit import Row

__all__ = ["SettingsScreen"]

_GROUP_HEADINGS: dict[str, str] = {
    "keys": "API keys",
    "paths": "Paths",
    "sources": "Sources",
    "preferences": "Preferences",
    "display": "Colours",
}

# Where to send someone who has none of these yet. The one detail worth carrying in the UI
# is which of TMDB's two credentials to copy — it is the likeliest first mistake.
SIGNUP: dict[str, str] = {
    "TMDB_API_KEY": "themoviedb.org → Settings → API → API Key (v3 auth), not the v4 token",
    "TWITCH_CLIENT_ID": "dev.twitch.tv/console/apps → register, client type Confidential",
    "TWITCH_CLIENT_SECRET": "the same Twitch app → New Secret",
    "OPENAI_API_KEY": "platform.openai.com/api-keys (paid)",
    "NOTION_TOKEN": "notion.so/my-integrations, then share the database with it",
}

# Changing these reopens a database that async workers hold across awaits, so they are
# applied at next launch rather than under a running session.
_NEEDS_RESTART: frozenset[str] = frozenset({"RDT_DB_PATH"})


class SettingRow(Row):
    """One setting: its name, a field, and where its current value is coming from."""

    def __init__(self, doc: FieldDoc, current: str, origin: str) -> None:
        placeholder = current or doc.blurb
        super().__init__(doc.alias, Input(placeholder=placeholder))
        self.doc = doc
        self.origin = origin

    def note(self) -> str:
        """The right-hand column: where the value is from, and anything to warn about."""
        if self.origin == "environment":
            # The one case where typing here achieves nothing until something else changes.
            return "[yellow]set in your environment — that wins[/]"
        if self.doc.alias in _NEEDS_RESTART:
            return f"[dim]{self.origin or 'default'} · applies next launch[/]"
        return f"[dim]{self.origin}[/]" if self.origin else ""

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield Static(Text.from_markup(self.note()), classes="row-note")


class SettingsScreen(ModalScreen[None]):
    """Edit configuration. Dismisses when closed; writes only on request."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Back", show=False),
        Binding("ctrl+s", "save", "Save", show=False),
        Binding("ctrl+t", "verify", "Check keys", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._origins: dict[str, str] = {}

    # --- layout ---------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        settings = get_settings()
        self._origins = config_file.origins()
        with Vertical(id="settings"):
            yield Static(
                Text.from_markup("[bold]Settings[/]  [dim]— environment first, then this file[/]"),
                id="settings-title",
            )
            with VerticalScroll(id="settings-body", can_focus=False):
                for group, heading in _GROUP_HEADINGS.items():
                    docs = [doc for doc in FIELD_DOCS if doc.group == group]
                    if not docs:
                        continue
                    yield Static(Text.from_markup(f"[bold]{heading}[/]"), classes="edit-head")
                    for doc in docs:
                        yield SettingRow(
                            doc, _current(settings, doc.alias), self._origins.get(doc.alias, "")
                        )
            with Vertical(id="settings-chrome"):
                yield Static("", id="settings-hint")
                yield Static(
                    Text.from_markup(
                        "[dim]↓↑ field · ctrl+s saves · ctrl+t checks the keys · esc back[/]"
                    ),
                    id="settings-foot",
                )
                yield Static("", id="settings-status")

    def on_mount(self) -> None:
        rows = self._rows
        if rows:
            rows[0].field.focus()
        self._show_signup(rows[0].doc.alias if rows else "")

    # --- state ----------------------------------------------------------------------
    @property
    def _rows(self) -> list[SettingRow]:
        body = self.query_one("#settings-body", VerticalScroll)
        return [widget for widget in body.children if isinstance(widget, SettingRow)]

    def row(self, alias: str) -> SettingRow:
        """The row for one setting — the form's state is its rows, so this is how a caller
        (or a test) reads what the screen would write."""
        return next(row for row in self._rows if row.doc.alias == alias)

    def _typed(self) -> dict[str, str]:
        """Only what was actually typed. An untouched field shows its current value as a
        placeholder, so leaving it alone must not rewrite it."""
        return {row.doc.alias: row.field.value for row in self._rows if row.field.value.strip()}

    def _say(self, markup: str) -> None:
        """Set the status line. Anything interpolated from an exception or a provider must
        be escaped first — square brackets in a validation message parse as a style tag and
        raise `MissingStyle` out of the render, taking the screen down with it."""
        self.query_one("#settings-status", Static).update(Text.from_markup(markup))

    def _show_signup(self, alias: str) -> None:
        hint = SIGNUP.get(alias, "")
        self.query_one("#settings-hint", Static).update(Text.from_markup(f"[dim]{hint}[/]"))

    @on(Input.Changed)
    def _on_change(self) -> None:
        focused = next((row for row in self._rows if row.field.has_focus), None)
        if focused is not None:
            self._show_signup(focused.doc.alias)

    # --- saving ---------------------------------------------------------------------
    def action_save(self) -> None:
        typed = self._typed()
        if not typed:
            self._say("[dim]Nothing to save.[/]")
            return
        try:
            path = config_file.set_values(dict(typed))
        except ValidationError as exc:
            # Only the first message: pydantic's full text is several lines and a URL, which
            # in a one-line status is noise around the sentence that matters.
            detail = str(exc.errors()[0].get("msg", exc))
            self._say(f"[red]Rejected:[/] {escape(detail)}")
            return
        except Exception as exc:  # a bad value must not close the screen or lose the typing
            self._say(f"[red]Rejected:[/] {escape(str(exc))}")
            return
        restart = sorted(alias for alias in typed if alias in _NEEDS_RESTART)
        shadowed = sorted(alias for alias in typed if self._origins.get(alias) == "environment")
        applied = self.rdt.apply_settings()
        for row in self._rows:
            if row.doc.alias in typed:
                row.field.value = ""
                row.field.placeholder = _current(applied, row.doc.alias)
        self._origins = config_file.origins()
        notes = [f"[green]Saved[/] [dim]{path}[/]"]
        if shadowed:
            notes.append(
                f"[yellow]{', '.join(shadowed)} is also in your environment, which wins[/]"
            )
        if restart:
            notes.append(f"[dim]{', '.join(restart)} applies next launch[/]")
        self._say("  ·  ".join(notes))

    @property
    def rdt(self):
        from release_tracker.tui.app import RdtApp

        assert isinstance(self.app, RdtApp)
        return self.app

    # --- verifying ------------------------------------------------------------------
    def action_verify(self) -> None:
        self._check()

    @work(exclusive=True, group="settings-verify")
    async def _check(self) -> None:
        """Ask the providers whether their keys work. Never gates saving — see the module
        docstring; this only tells you sooner what a search would tell you later."""
        settings = get_settings()
        typed = self._typed()
        tmdb = typed.get("TMDB_API_KEY") or secret(settings.tmdb_api_key) or ""
        client_id = typed.get("TWITCH_CLIENT_ID") or secret(settings.twitch_client_id) or ""
        client_secret = (
            typed.get("TWITCH_CLIENT_SECRET") or secret(settings.twitch_client_secret) or ""
        )
        self._say("[dim]checking…[/]")
        client = await self.rdt.http()
        results: list[str] = []
        if tmdb:
            results.append(_verdict("TMDB", await check_tmdb(client, tmdb)))
        if client_id or client_secret:
            results.append(_verdict("IGDB", await check_twitch(client, client_id, client_secret)))
        self._say("  ·  ".join(results) if results else "[dim]No keys to check.[/]")

    def action_close(self) -> None:
        self.dismiss(None)


def _verdict(label: str, result: CheckResult) -> str:
    colour = "green" if result.ok else "red"
    return f"[{colour}]{label}: {escape(result.detail)}[/]"


def _current(settings: Settings, alias: str) -> str:
    """The effective value as it is safe to show — masked when it is a credential."""
    name = field_name_for(alias)
    if name is None:
        return ""
    raw = getattr(settings, name)
    value = secret(raw) if isinstance(raw, SecretStr) else raw
    return config_file.mask(alias, str(value)) if value else ""
