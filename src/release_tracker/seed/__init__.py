"""Seed providers — where the watchlist of entities comes from.

Two providers, both decoupled from the core so personal data never has to live in
the repo:

- ``local``: a gitignored ``local/seeds.json`` (works with no credentials).
- ``notion``: a live pull from a Notion database via your own integration token.

A seed yields :class:`Entity` objects (and, for Notion, the user's hand-authored
dates become low-tier observations so manual curation is just another source).
"""

from __future__ import annotations

from release_tracker.seed.base import SeedBundle, SeedProvider
from release_tracker.seed.local import LocalSeed
from release_tracker.seed.notion import NotionSeed

__all__ = ["LocalSeed", "NotionSeed", "SeedBundle", "SeedProvider"]
