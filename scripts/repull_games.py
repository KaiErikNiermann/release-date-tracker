"""One-off: re-pull GAME entities after the IGDB precision/certainty fix.

The old puller stamped every IGDB date ``CONFIRMED`` regardless of precision, so a
coarse "2026"/"TBD" became a fake-confirmed Dec-31 that shadowed curated dates at the
pivot (the ONTOS bug). Re-pull so games pick up precision-derived certainty (only an
exact day is confirmed) and the studio-trend refinement for coarse dates.

Run: poetry run python scripts/repull_games.py
"""

from __future__ import annotations

import asyncio

from release_tracker.config import get_settings
from release_tracker.db import Database
from release_tracker.logging import configure_logging, get_logger
from release_tracker.models import MediaKind
from release_tracker.pipeline import pull_entity
from release_tracker.sources.base import make_client

log = get_logger("repull_games")


async def main() -> None:
    configure_logging()
    settings = get_settings()
    db = Database(settings.db_path)
    games = [e for e in db.iter_entities() if e.kind is MediaKind.GAME]
    log.info("repull.start", entities=len(games))
    async with make_client() as client:
        for ent in games:
            stats = await pull_entity(db, settings, ent, client=client)
            log.info("repull.entity", title=ent.title, observations=stats.observations)
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
