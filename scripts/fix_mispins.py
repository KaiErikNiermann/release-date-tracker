"""One-off: correct a batch of mis-resolved canonical pins (ground-truthed by hand).

Several titles had matched an older same-named work (e.g. "Resident Evil" -> the 2002
film). Re-pin each to the right canonical id and re-pull; for titles with no canonical
record yet (a sequel/remake not in TMDB), pin a SKIP sentinel and record the announced
date as a manual observation so the tracker still shows the real release.

Run: poetry run python scripts/fix_mispins.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

import httpx

from release_tracker.config import Settings, get_settings
from release_tracker.db import Database
from release_tracker.logging import configure_logging, get_logger
from release_tracker.models import (
    Certainty,
    DatePrecision,
    Entity,
    MediaKind,
    ReleaseChannel,
    ReleaseObservation,
    SourceTier,
)
from release_tracker.pipeline import pull_entity
from release_tracker.sources.base import make_client

log = get_logger("fix_mispins")

# Tier-0 providers whose stale rows we clear before a re-pull / manual override.
_CANONICAL_PROVIDERS = ("tmdb", "igdb", "steam", "model")


def _find(db: Database, title: str, kind: MediaKind) -> Entity:
    matches = [e for e in db.iter_entities() if e.title == title and e.kind is kind]
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one {kind.value} {title!r}, found {len(matches)}")
    return matches[0]


def _manual_obs(
    entity_id: str, when: date, channel: ReleaseChannel, basis: str
) -> ReleaseObservation:
    """An officially-announced date we hand-enter because no Tier-0 record carries it yet."""
    return ReleaseObservation(
        entity_id=entity_id,
        channel=channel,
        region="WW",
        release_date=when,
        precision=DatePrecision.EXACT,
        certainty=Certainty.CONFIRMED,
        source_tier=SourceTier.OFFICIAL,
        provider="manual",
        source_name="Manual (announced)",
        source_quote=basis,
        fetched_at=datetime.now(UTC),
    )


async def _repin_and_pull(
    db: Database,
    settings: Settings,
    client: httpx.AsyncClient,
    ent: Entity,
    ids: dict[str, str],
    *,
    kind: MediaKind | None = None,
) -> None:
    updated = ent.model_copy(update={"external_ids": ids, **({"kind": kind} if kind else {})})
    db.delete_observations(ent.id, _CANONICAL_PROVIDERS)
    db.upsert_entity(updated)
    stats = await pull_entity(db, settings, updated, client=client)
    log.info(
        "repin",
        title=updated.title,
        kind=updated.kind.value,
        ids=ids,
        observations=stats.observations,
    )


async def main() -> None:
    configure_logging()
    settings = get_settings()
    db = Database(settings.db_path)

    async with make_client() as client:
        # --- correct canonical id found: re-pin + re-pull ---------------------
        # Resident Evil -> Zach Cregger reboot (Sept 18, 2026).
        await _repin_and_pull(
            db, settings, client, _find(db, "Resident Evil", MediaKind.MOVIE), {"tmdb": "1423191"}
        )
        # In the Gray -> Guy Ritchie's "In the Grey" (Gyllenhaal/Cavill, 2026).
        await _repin_and_pull(
            db, settings, client, _find(db, "In the Gray", MediaKind.MOVIE), {"tmdb": "1122573"}
        )
        # Lucky -> Apple TV+ limited series (Anya Taylor-Joy, July 15, 2026).
        await _repin_and_pull(
            db, settings, client, _find(db, "Lucky", MediaKind.TV), {"tmdb": "278624"}
        )
        # Human Vapor -> Netflix/Toho series remake (July 2, 2026); was mis-kinded as a movie.
        await _repin_and_pull(
            db,
            settings,
            client,
            _find(db, "Human Vapor", MediaKind.MOVIE),
            {"tmdb": "253960"},
            kind=MediaKind.TV,
        )
        # Defect -> emptyvessel's shooter (Mick Gordon); fix the wrong Steam appid.
        await _repin_and_pull(
            db,
            settings,
            client,
            _find(db, "Defect", MediaKind.GAME),
            {"igdb": "313764", "steam_appid": "2470010"},
        )

        # --- no canonical record yet: skip-pin + manual announced date --------
        # Superman 2 -> "Superman: Man of Tomorrow" (July 9, 2027) — not in TMDB yet.
        superman = _find(db, "Superman 2", MediaKind.MOVIE)
        db.delete_observations(superman.id, (*_CANONICAL_PROVIDERS, "notion"))
        db.upsert_entity(
            superman.model_copy(
                update={"title": "Superman: Man of Tomorrow", "external_ids": {"tmdb": "-"}}
            )
        )
        db.upsert_observation(
            _manual_obs(
                superman.id,
                date(2027, 7, 9),
                ReleaseChannel.THEATRICAL,
                "Gunn-era sequel, announced",
            )
        )
        log.info("manual", title="Superman: Man of Tomorrow", when="2027-07-09")

    # Dance Macabre -> Hisko Hulsing animated short, Annecy premiere June 22, 2026.
    # Two entities share one Notion page: keep the tracked `other` row, drop the
    # mis-resolved `movie` (1992) duplicate.
    dupe = _find(db, "Dance Macabre", MediaKind.MOVIE)
    db.delete_entity(dupe.id)
    real = _find(db, "Dance Macabre", MediaKind.OTHER)
    db.delete_observations(real.id, (*_CANONICAL_PROVIDERS, "notion"))
    db.upsert_entity(real.model_copy(update={"title": "Danse Macabre", "kind": MediaKind.MOVIE}))
    db.upsert_observation(
        _manual_obs(
            real.id,
            date(2026, 6, 22),
            ReleaseChannel.PREMIERE,
            "Hisko Hulsing short, Annecy premiere",
        )
    )
    log.info("manual", title="Danse Macabre", when="2026-06-22", dropped_dupe=dupe.id)
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
