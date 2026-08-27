"""Tier-0 structured-data pullers (free APIs, no NLP).

Each source implements :class:`Source` and maps a provider's data into our
``ReleaseObservation`` model. A registry routes an entity to the sources that
support its :class:`MediaKind`.
"""

from __future__ import annotations

from release_tracker.models import MediaKind
from release_tracker.sources.base import Candidate, Source, SourceResult
from release_tracker.sources.igdb import IgdbSource
from release_tracker.sources.steam import SteamSource
from release_tracker.sources.tmdb import TmdbSource
from release_tracker.sources.wikidata import WikidataSource

ALL_SOURCES: tuple[Source, ...] = (TmdbSource(), IgdbSource(), SteamSource(), WikidataSource())

# provider names that come from live API pulls (vs. the 'notion' manual seed),
# so a refresh can clear only stale API rows and keep hand-authored ones.
API_PROVIDERS: tuple[str, ...] = tuple(s.name for s in ALL_SOURCES)


def sources_for(kind: MediaKind) -> tuple[Source, ...]:
    return tuple(s for s in ALL_SOURCES if s.supports(kind))


__all__ = ["ALL_SOURCES", "API_PROVIDERS", "Candidate", "Source", "SourceResult", "sources_for"]
