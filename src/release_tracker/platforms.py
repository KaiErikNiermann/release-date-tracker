"""Self-growing distributor -> streaming-home map.

:data:`release_tracker.deltas.STUDIO_STREAMING` is a hand-curated table covering the
majors. When a movie lookup surfaces a distributor we *don't* have, we ask the LLM
gap-filler where that studio's films typically stream and persist the answer — or
the sentinel ``Undetermined`` when even the model can't say — so the map grows
itself and we never re-ask the same studio while the entry is fresh.

Resolution order (cheapest first): hand table -> learned store -> one LLM call for
the best distributor candidate. The store is a derived cache (its own SQLite file,
separate from the entity DB), TTL'd so a studio that gains a known home later, or a
deal that churns, gets picked up on the next refresh.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from release_tracker.config import Settings, secret
from release_tracker.deltas import match_studio, predicted_platform
from release_tracker.logging import get_logger

log = get_logger("platforms")

# Stored when the model has no confident streaming home for a studio — keeps us
# from re-asking, but is never returned as an answer.
UNDETERMINED: Final[str] = "Undetermined"
# Output deals churn and unknowns can become known; refresh entries this often.
PLATFORM_TTL_DAYS: Final[int] = 120

Resolver = Callable[[str], Awaitable[str | None]]

_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS learned_platforms (
    studio_key   TEXT PRIMARY KEY,
    studio_name  TEXT NOT NULL,
    platform     TEXT NOT NULL,
    resolved_at  TEXT NOT NULL
);
"""

_SYSTEM_PROMPT: Final[str] = (
    "You map film distributors/studios to the subscription streaming service their "
    "theatrical films typically land on first (the studio's in-house service or its "
    "current pay-1 output-deal partner), e.g. Walt Disney -> Disney+, Universal -> "
    "Peacock, Sony/Columbia -> Netflix, Warner Bros -> HBO Max, Lionsgate -> Starz. "
    "Reason about the parent company for small labels. Return the canonical service "
    "name only. If the studio has no consistent or known streaming home, return null."
)


def _norm(name: str) -> str:
    return " ".join(name.lower().split())


# Models sometimes emit these as a *string* instead of JSON null — treat as "unknown".
_NULL_TOKENS: Final[frozenset[str]] = frozenset(
    {"", "null", "none", "n/a", "na", "unknown", "undetermined", "undecided", "tbd"}
)


class PlatformGuess(BaseModel):
    """Structured LLM answer: the typical streaming home, or null if none/unknown."""

    platform: str | None = Field(description="canonical streaming service name, or null")


class PlatformStore:
    """TTL'd SQLite KV of learned distributor -> streaming home (incl. ``Undetermined``)."""

    def __init__(self, path: Path, *, ttl_days: int = PLATFORM_TTL_DAYS) -> None:
        self.path = path
        self.ttl = timedelta(days=ttl_days)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> PlatformStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get(self, studio_key: str) -> str | None:
        """Stored value (a platform name or ``Undetermined``) if fresh, else ``None``."""
        row = self._conn.execute(
            "SELECT platform, resolved_at FROM learned_platforms WHERE studio_key = ?",
            (studio_key,),
        ).fetchone()
        if row is None:
            return None
        if datetime.now(UTC) - datetime.fromisoformat(row["resolved_at"]) > self.ttl:
            return None
        return str(row["platform"])

    def put(self, studio_key: str, studio_name: str, platform: str) -> None:
        self._conn.execute(
            """
            INSERT INTO learned_platforms (studio_key, studio_name, platform, resolved_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(studio_key) DO UPDATE SET
                studio_name=excluded.studio_name,
                platform=excluded.platform,
                resolved_at=excluded.resolved_at
            """,
            (studio_key, studio_name, platform, datetime.now(UTC).isoformat()),
        )
        self._conn.commit()


async def resolve_platform_llm(studio: str, settings: Settings) -> str | None:
    """Ask the LLM gap-filler for a studio's typical streaming home (``None`` if unknown)."""
    if not settings.openai_api_key:
        return None
    client = AsyncOpenAI(api_key=secret(settings.openai_api_key))
    try:
        completion = await client.beta.chat.completions.parse(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"Distributor/studio: {studio}"},
            ],
            response_format=PlatformGuess,
        )
    except Exception as exc:  # network / API / quota — degrade to "unknown", don't crash
        log.warning("platforms.llm_error", studio=studio, error=str(exc))
        return None
    guess = completion.choices[0].message.parsed
    if guess is None or not guess.platform:
        return None
    answer = guess.platform.strip()
    return None if answer.lower() in _NULL_TOKENS else answer


async def resolve_predicted_platform(
    studios: tuple[str, ...],
    store: PlatformStore,
    resolve: Resolver,
    *,
    can_learn: bool = True,
) -> str | None:
    """Hand table -> learned store -> learn the best candidate (pure orchestration).

    ``resolve`` is injected so this is testable without the network. Learns at most
    one studio per call (the distributor candidate), persisting ``Undetermined`` on a
    miss so it isn't re-asked until the entry expires. ``can_learn`` is False when we
    can't actually ask (no API key) — then we never write a guessed ``Undetermined``.
    """
    if hard := predicted_platform(studios):
        return hard
    for studio in studios:
        if (learned := store.get(_norm(studio))) and learned != UNDETERMINED:
            return learned
    if not can_learn:
        return None

    candidate = match_studio(studios)
    if candidate is None:
        return None
    key = _norm(candidate)
    if store.get(key) is not None:  # already attempted and still fresh — don't re-ask
        return None
    platform = await resolve(candidate)
    store.put(key, candidate, platform or UNDETERMINED)
    return platform


async def learn_predicted_platform(studios: tuple[str, ...], settings: Settings) -> str | None:
    """Production wiring: open the learned store and resolve via the real LLM."""

    async def resolve(studio: str) -> str | None:
        return await resolve_platform_llm(studio, settings)

    with PlatformStore(settings.platform_db_path) as store:
        return await resolve_predicted_platform(
            studios, store, resolve, can_learn=bool(settings.openai_api_key)
        )
