"""Local JSON seed provider.

Reads a gitignored ``local/seeds.json``: a list of objects like::

    [
      {"title": "Hollow Knight: Silksong", "kind": "game"},
      {"title": "Dune: Part Two", "kind": "movie", "aliases": ["Dune 2"]},
      {"title": "Steam Frame", "kind": "tech", "watch": true, "notes": "Valve headset"}
    ]

``kind`` accepts our MediaKind values ("movie","tv","game","tech",...) or Notion
option labels ("Movie","Video Game",...).
"""

from __future__ import annotations

import json
from typing import Any, cast

from release_tracker.config import Settings
from release_tracker.logging import get_logger
from release_tracker.models import Entity
from release_tracker.seed.base import SeedBundle, parse_kind

log = get_logger("seed.local")


class LocalSeed:
    name = "local"

    def load(self, settings: Settings) -> SeedBundle:
        path = settings.seeds_path
        if not path.exists():
            log.warning("seed.local.missing", path=str(path))
            return SeedBundle()
        raw = cast("list[dict[str, Any]]", json.loads(path.read_text(encoding="utf-8")))
        entities: list[Entity] = []
        for item in raw:
            title = str(item["title"]).strip()
            kind = parse_kind(str(item.get("kind", "other")))
            entities.append(
                Entity.create(
                    title=title,
                    kind=kind,
                    aliases=tuple(str(a) for a in item.get("aliases", [])),
                    external_ids={k: str(v) for k, v in item.get("external_ids", {}).items()},
                    notes=item.get("notes"),
                    watch=bool(item.get("watch", True)),
                )
            )
        log.info("seed.local.loaded", count=len(entities), path=str(path))
        return SeedBundle(entities=entities)
