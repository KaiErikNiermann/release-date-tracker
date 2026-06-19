"""Seed provider protocol and shared mapping helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from release_tracker.config import Settings
from release_tracker.models import ConsumptionState, Entity, MediaKind, ReleaseObservation

# Notion `Type` select option -> our MediaKind.
NOTION_TYPE_TO_KIND: dict[str, MediaKind] = {
    "Movie": MediaKind.MOVIE,
    "TV Show": MediaKind.TV,
    "Video Game": MediaKind.GAME,
    "Book": MediaKind.BOOK,
    "Music Album": MediaKind.MUSIC,
    "Podcast": MediaKind.PODCAST,
    "Comic/Manga": MediaKind.COMIC,
    # "Anime" is a medium, not a format — default to a series (the common case);
    # enrich tags the anime origin, and an anime film is re-kinded to movie by hand
    # (`rdt edit kind`). Notion has no movie/series split for it, so TV is the guess.
    "Anime": MediaKind.TV,
    "Technology": MediaKind.TECH,
}

# Notion `Status` (watch progress) -> our ConsumptionState.
NOTION_STATUS_TO_STATE: dict[str, ConsumptionState] = {
    "to Watch": ConsumptionState.WANT,
    "Want to Watch/Play": ConsumptionState.WANT,
    "Currently Watching/Playing": ConsumptionState.WATCHING,
    "Watched/Played": ConsumptionState.WATCHED,
}


def parse_consumption_state(value: str | None) -> ConsumptionState:
    """Map a Notion Status option to a ConsumptionState (UNSET when unknown/empty)."""
    return NOTION_STATUS_TO_STATE.get(value or "", ConsumptionState.UNSET)


@dataclass(slots=True)
class SeedBundle:
    """Entities (and any seed observations) produced by a provider."""

    entities: list[Entity] = field(default_factory=list[Entity])
    observations: list[ReleaseObservation] = field(default_factory=list[ReleaseObservation])


@runtime_checkable
class SeedProvider(Protocol):
    name: str

    def load(self, settings: Settings) -> SeedBundle: ...


def parse_kind(value: str) -> MediaKind:
    """Map a free-form type string (Notion option or local seed) to a MediaKind."""
    if value in NOTION_TYPE_TO_KIND:
        return NOTION_TYPE_TO_KIND[value]
    try:
        return MediaKind(value.lower())
    except ValueError:
        return MediaKind.OTHER
