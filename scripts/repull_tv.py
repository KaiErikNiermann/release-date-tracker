"""One-off: re-pull TV entities after the season-air-date fallback fix.

The old puller stamped an unaired season with the show's first_air_date (e.g.
"Severance: Season 3" -> S1's 2022 date). Delete stale tmdb observations for TV
entities and re-pull so the wrong dates are dropped (a season with no air date
now yields no observation rather than a wrong one).
"""

from __future__ import annotations

import asyncio

from release_tracker.config import get_settings
from release_tracker.db import Database
from release_tracker.logging import configure_logging, get_logger
from release_tracker.models import MediaKind
from release_tracker.pipeline import pull_entity
from release_tracker.sources.base import make_client

log = get_logger("repull_tv")


async def main() -> None:
    configure_logging()
    settings = get_settings()
    db = Database(settings.db_path)
    tv = [e for e in db.iter_entities() if e.kind is MediaKind.TV]
    log.info("repull.start", entities=len(tv))
    async with make_client() as client:
        for ent in tv:
            removed = db.delete_observations(ent.id, ("tmdb",))
            stats = await pull_entity(db, settings, ent, client=client)
            log.info(
                "repull.entity", title=ent.title, cleared=removed, observations=stats.observations
            )
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
