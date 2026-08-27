"""The current moment, in the one timezone this project stores.

Everything persisted here is UTC-aware — `ReleaseObservation.fetched_at`, the cache's
`computed_at`, the platform map's `resolved_at` — so a naive local `datetime.now()`
compares as unequal to all of it and subtracts wrongly by the local offset. Keeping both
spellings in one place means the answer to "what is today" cannot drift between the CLI,
the TUI and the sources, which had grown two byte-identical private copies.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

__all__ = ["utc_now", "utc_today"]


def utc_now() -> datetime:
    """Now, timezone-aware. The only correct argument to a stored timestamp."""
    return datetime.now(UTC)


def utc_today() -> date:
    """Today in UTC — not in the local zone, so a run at 23:00 UTC+2 agrees with the data."""
    return datetime.now(UTC).date()
