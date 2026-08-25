"""Session state for the TUI: one snapshot of the tracker, filtered in memory.

Building a row costs ~8-10 queries, so a full snapshot of the tracker is ~85 ms — fine
once, far too slow per keystroke. Everything the browse screen does is therefore a pure
filter over this immutable snapshot, and the database is touched only at startup, on an
explicit refresh, and on a write.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from release_tracker import query, views
from release_tracker.config import Settings
from release_tracker.db import Database
from release_tracker.models import Bucket
from release_tracker.views import TrackRow

__all__ = ["Snapshot", "bucket_of_query", "build_snapshot", "with_bucket"]


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Every tracked row plus the completion vocabulary, as of one moment."""

    rows: tuple[TrackRow, ...]
    vocab: query.Vocabulary
    today: date

    def replace_row(self, row: TrackRow) -> Snapshot:
        """Swap one row in place — a state change re-reads one row, never all 169."""
        return Snapshot(
            rows=tuple(row if r.entity_id == row.entity_id else r for r in self.rows),
            vocab=self.vocab,
            today=self.today,
        )


def build_snapshot(db: Database, settings: Settings, today: date) -> Snapshot:
    return Snapshot(
        rows=tuple(views.track_rows(db, today, settings)),
        vocab=views.build_vocabulary(db),
        today=today,
    )


def _bucket_token(text: str) -> Bucket | None:
    """The bucket a raw `is:<bucket>` token names, if it names one."""
    field, sep, value = text.lstrip("-!").partition(":")
    if not sep or field.lower() != "is":
        return None
    try:
        return Bucket(value.strip().strip('"').lower())
    except ValueError:
        return None


def bucket_of_query(source: str) -> Bucket | None:
    """Which bucket the query currently pins, if any."""
    return next((b for t in query.lex(source) if (b := _bucket_token(t.text))), None)


def with_bucket(source: str, bucket: Bucket) -> str:
    """Rewrite the query so it pins ``bucket``, leaving every other term intact.

    The bucket tabs are sugar over the query language rather than separate view state:
    there is one source of truth for what is on screen, and pressing a tab teaches the
    syntax by showing it in the bar.
    """
    kept = [source[t.start : t.end] for t in query.lex(source) if _bucket_token(t.text) is None]
    return " ".join([f"is:{bucket.value}", *kept])
