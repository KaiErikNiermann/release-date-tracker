"""Where a work's facts come from, and which of those we can go back and re-read.

Every card gets a Sources section that answers one question honestly: *what can this tool
refetch, and what do you have to go look at yourself?* Two things make the split real rather
than cosmetic:

* Some sources have a puller. Their line is ``AUTO`` and the card's update key re-pulls them.
* Some are addressable but not fetchable — GSMArena blocks ``ClaudeBot`` and its RSL licence
  prohibits ``ai-inference``; TechPowerUp prohibits text-and-data-mining outright. Their line
  is ``LINK`` with the reason attached, and the user is the update mechanism: open it, read
  the date, hand-edit. That is *more* useful than hiding them, because Wikidata records their
  ids (``P4723``, ``P13418``, ``P13844``) so we can land on the exact device page.

Derived, never stored. The inputs — pinned ids, the urls sources already recorded on their
observations, and the category policy — are all live, so a link can't go stale against them
and there is no table to migrate. Nothing here does I/O.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import quote_plus

from release_tracker.models import Entity, MediaKind
from release_tracker.sources import sources_for
from release_tracker.tech import CATEGORY_INFO, classify_tech

__all__ = [
    "SourceAccess",
    "SourceLink",
    "work_sources",
]


class SourceAccess(enum.StrEnum):
    """Whether we can re-read this source ourselves."""

    AUTO = "auto"  # a registered Source pulls it — the card's update key acts on this
    LINK = "link"  # addressable only; the user opens it and hand-edits what they find


@dataclass(frozen=True, slots=True)
class SourceLink:
    """One canonical place this work's dates can be read."""

    provider: str  # stable key: "tmdb", "gsmarena", "search:gsmarena.com"
    label: str  # what the card shows
    access: SourceAccess
    url: str
    reason: str | None = None  # why it is link-only, when that isn't obvious


@dataclass(frozen=True, slots=True)
class _Spec:
    """How to turn a pinned id into a url, and who (if anyone) can refetch it."""

    label: str
    template: str  # carries {id}, and {kind} when the path differs per media kind
    puller: str | None = None  # the Source.name able to re-read it, if there is one
    reason: str | None = None
    kind_segment: Mapping[MediaKind, str] | None = None


_LICENCE_DECLINED: Final = "site declines automated extraction — open it to read the date"
_NO_STRUCTURE: Final = "no structured data to parse"

# Keyed by the `external_ids` key, which is what actually gets pinned on an entity.
_SPECS: Final[dict[str, _Spec]] = {
    "tmdb": _Spec(
        "TMDB",
        "https://www.themoviedb.org/{kind}/{id}",
        puller="tmdb",
        kind_segment={MediaKind.MOVIE: "movie", MediaKind.TV: "tv"},
    ),
    "igdb": _Spec("IGDB", "https://www.igdb.com/games/{id}", puller="igdb"),
    "steam_appid": _Spec("Steam", "https://store.steampowered.com/app/{id}", puller="steam"),
    "wikidata": _Spec("Wikidata", "https://www.wikidata.org/wiki/{id}", puller="wikidata"),
    "gsmarena": _Spec(
        "GSMArena", "https://www.gsmarena.com/model-{id}.php", reason=_LICENCE_DECLINED
    ),
    "techpowerup_gpu": _Spec(
        "TechPowerUp GPU",
        "https://www.techpowerup.com/gpu-specs/wd.{id}",
        reason=_LICENCE_DECLINED,
    ),
    "techpowerup_cpu": _Spec(
        "TechPowerUp CPU",
        "https://www.techpowerup.com/cpu-specs/_.{id}",
        reason=_LICENCE_DECLINED,
    ),
    # The numeric SKU id alone resolves — Intel redirects it to the slugged url. There is no
    # puller because there is no way to *find* the id: ARK has no public API, its search page
    # is client-rendered, and Wikidata carries no ARK property. A human finds the SKU, we
    # deep-link it.
    "intel_ark": _Spec(
        "Intel ARK",
        "https://www.intel.com/content/www/us/en/products/sku/{id}/specifications.html",
        reason="no way to search ARK programmatically — open it for the launch date",
    ),
    "imdb": _Spec("IMDb", "https://www.imdb.com/title/{id}/", reason=_NO_STRUCTURE),
    "metacritic": _Spec("Metacritic", "https://www.metacritic.com/{id}", reason=_NO_STRUCTURE),
    "rottentomatoes": _Spec(
        "Rotten Tomatoes", "https://www.rottentomatoes.com/{id}", reason=_NO_STRUCTURE
    ),
    "official_website": _Spec("Official site", "{id}", reason=_NO_STRUCTURE),
}

# Providers that are us, not a source — a hand-edit and our own prediction have no page to
# send anyone to.
_OURS: Final[frozenset[str]] = frozenset({"manual", "model", "notion"})


def _spec_url(spec: _Spec, value: str, kind: MediaKind) -> str | None:
    """The url for one pinned id, or None when the template can't be filled for this kind."""
    if spec.kind_segment is not None:
        segment = spec.kind_segment.get(kind)
        if segment is None:  # e.g. a tmdb id on something that is neither film nor TV
            return None
        return spec.template.format(id=value, kind=segment)
    return spec.template.format(id=value)


def _search_links(entity: Entity) -> list[SourceLink]:
    """Pre-built searches for a device we hold no id for — the only lead there is.

    Wikidata knows a small fraction of consumer tech, so this is the common case, and a
    query the user can click beats a shrug. The specialist sites are the right target even
    though we can't parse them: a person reading GSMArena is exactly what its licence
    contemplates, and it is where the answer actually is.
    """
    info = CATEGORY_INFO[classify_tech(entity.title)]
    query = quote_plus(entity.title)
    links = [
        SourceLink(
            provider=f"search:{domain}",
            label=f"Search {domain}",
            access=SourceAccess.LINK,
            url=f"https://duckduckgo.com/?q=site%3A{domain}+{query}",
        )
        for domain in info.preferred_sources
    ]
    links.append(
        SourceLink(
            provider="search:web",
            label="Search the web",
            access=SourceAccess.LINK,
            url=f"https://duckduckgo.com/?q={query}+release+date",
        )
    )
    return links


def work_sources(
    entity: Entity, observation_urls: Mapping[str, str] | None = None
) -> tuple[SourceLink, ...]:
    """Every canonical place this work can be read, ``AUTO`` first.

    ``observation_urls`` maps a provider name to the url it recorded on an observation. It is
    preferred over a constructed one because the source itself wrote it — and for IGDB it is
    the *only* option, since we pin a numeric id but its pages are addressed by slug.
    """
    pullers = {source.name for source in sources_for(entity.kind)}
    seen: set[str] = set()
    out: list[SourceLink] = []

    for provider, url in sorted((observation_urls or {}).items()):
        if provider in _OURS or not url:
            continue
        spec = next((s for s in _SPECS.values() if s.puller == provider), None)
        out.append(
            SourceLink(
                provider=provider,
                label=spec.label if spec is not None else provider,
                access=SourceAccess.AUTO if provider in pullers else SourceAccess.LINK,
                url=url,
                reason=None if provider in pullers else _NO_STRUCTURE,
            )
        )
        seen.add(provider)

    for key, value in sorted(entity.external_ids.items()):
        spec = _SPECS.get(key)
        if spec is None or not value or spec.puller in seen:
            continue
        url = _spec_url(spec, value, entity.kind)
        if url is None:
            continue
        auto = spec.puller is not None and spec.puller in pullers
        out.append(
            SourceLink(
                provider=key,
                label=spec.label,
                access=SourceAccess.AUTO if auto else SourceAccess.LINK,
                url=url,
                reason=None if auto else spec.reason,
            )
        )
        seen.add(spec.puller or key)

    if not out and entity.kind is MediaKind.TECH:
        out.extend(_search_links(entity))

    out.sort(key=lambda link: (link.access is not SourceAccess.AUTO, link.label))
    return tuple(out)
