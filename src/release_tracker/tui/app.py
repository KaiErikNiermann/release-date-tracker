"""The Textual application shell.

Deliberately thin: it owns the session's resources (one Database, one HTTP client, one
snapshot) and routes between screens. Every tracker decision — what a query means, which
bucket a work is in, what a capture does — belongs to the library, so the TUI cannot
answer differently from the CLI.

**Threading rule:** `Database` opens its sqlite connection without `check_same_thread`,
so a thread worker touching it would raise. All database access therefore stays on the
event loop (the one blocking call, the ~85 ms snapshot build, happens at startup and on
an explicit reload). Network work runs in async workers, which is free here because the
whole source layer is already async httpx.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, date, datetime
from typing import ClassVar

import httpx
from textual.app import App
from textual.binding import Binding, BindingType

from release_tracker import views
from release_tracker.config import Settings, get_settings
from release_tracker.db import Database
from release_tracker.logging import configure_logging
from release_tracker.models import ConsumptionState, Entity
from release_tracker.sources.base import make_client
from release_tracker.tui.add import AddScreen
from release_tracker.tui.browse import BrowseScreen
from release_tracker.tui.card import CardScreen
from release_tracker.tui.state import Snapshot, build_snapshot
from release_tracker.views import TrackRow

__all__ = ["RdtApp", "run"]


class RdtApp(App[None]):
    """Query-first browser over the tracker."""

    CSS_PATH = "rdt.tcss"
    TITLE = "rdt"
    BINDINGS: ClassVar[list[BindingType]] = [Binding("ctrl+c", "quit", "Quit", show=False)]

    def __init__(
        self,
        settings: Settings | None = None,
        db: Database | None = None,
        today: date | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings or get_settings()
        self.db = db or Database(self.settings.db_path)
        self.today = today or datetime.now(UTC).date()
        self.snapshot: Snapshot = build_snapshot(self.db, self.settings, self.today)
        self._client: httpx.AsyncClient | None = None

    def on_mount(self) -> None:
        self.push_screen(BrowseScreen())

    async def http(self) -> httpx.AsyncClient:
        """One client for the session — candidate searches and captures share it."""
        if self._client is None:
            self._client = make_client()
        return self._client

    async def on_unmount(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
        self.db.close()

    # --- snapshot ------------------------------------------------------------------
    def reload_snapshot(self) -> None:
        self.snapshot = build_snapshot(self.db, self.settings, self.today)

    def _refresh_browse(self) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, BrowseScreen):
                screen.refresh_rows()

    def set_state(self, entity: Entity, state: ConsumptionState) -> None:
        """Persist a state change and patch the one affected row.

        Re-reading a single row costs ~0.3 ms against ~85 ms for the whole snapshot, so
        the list behind the modal stays live without a rebuild.
        """
        if not self.db.set_consumption_state(entity.id, state):
            self.notify(f"could not update {entity.title}", severity="error")
            return
        updated = self.db.get_entity(entity.id)
        if updated is not None:
            notes = self.db.note_counts().get(entity.id, 0) > 0
            row = views.track_row(self.db, updated, self.today, self.settings, notes)
            self.snapshot = self.snapshot.replace_row(row)
        self._refresh_browse()

    # --- navigation ----------------------------------------------------------------
    def open_card(self, row: TrackRow) -> None:
        entity = self.db.get_entity(row.entity_id)
        if entity is None:
            self.notify("that work is no longer in the tracker", severity="warning")
            return
        self.push_screen(CardScreen(entity, views.work_card(self.db, entity)))

    def open_add(self, initial: str = "") -> None:
        def added(entity: Entity | None) -> None:
            if entity is None:
                return
            self.reload_snapshot()
            self._refresh_browse()
            self.notify(f"added {entity.title}")

        self.push_screen(AddScreen(initial), added)


def run() -> None:
    configure_logging()
    RdtApp().run()
