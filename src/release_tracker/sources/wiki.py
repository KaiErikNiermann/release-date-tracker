"""Wikipedia hints — a keyless source that cuts manual contingency churn for the LLM/skills.

Two stages, both best-effort (never raise — a miss/outage yields ``None``, like ``ddg.py``):

1. **Exists-check (always):** does a Wikipedia page exist for this title? A cheap MediaWiki
   ``search/title`` GET — deterministic "is there a page", returning the canonical url.
2. **Section fetch (on demand):** only when the structured sources (TMDB/IGDB) didn't yield
   the facet we need — pull the page wikitext and grep the infobox for ``platforms`` /
   ``released`` / ``language`` (+ a couple more) as **raw, unparsed** text. The skill/LLM
   reads it to pre-fill ``rdt edit contingency``; the tool never parses dates out of it.

The CLI dumps the same ``wiki_hints`` JSON; it's the skill that mines it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, cast

import httpx

from release_tracker.logging import get_logger
from release_tracker.sources.base import get_json

log = get_logger("wiki")

_SEARCH = "https://en.wikipedia.org/w/rest.php/v1/search/title"
_PAGE = "https://en.wikipedia.org/w/rest.php/v1/page/"
_ARTICLE = "https://en.wikipedia.org/wiki/"
# infobox params worth surfacing as contingency hints (raw value text, capped).
_FACET_PARAMS = ("platforms", "platform", "released", "release date", "language", "country")
_PARAM_RE = re.compile(
    r"^\s*\|\s*(" + "|".join(p.replace(" ", r"\s*") for p in _FACET_PARAMS) + r")\s*=\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)
_MAX_VALUE = 200


@dataclass(frozen=True, slots=True)
class WikiHints:
    """A Wikipedia page pointer (+ optional raw infobox facets) for a tracked title."""

    page_title: str
    url: str
    exists: bool
    sections: dict[str, str] = field(default_factory=dict[str, str])  # infobox param -> raw value

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "page_title": self.page_title,
            "url": self.url,
            "exists": self.exists,
        }
        if self.sections:  # lean: omit when we didn't fetch facets
            out["sections"] = dict(self.sections)
        return out


def _clean(value: str) -> str:
    """Strip the noisiest wiki markup from an infobox value, keeping it readable + capped."""
    value = re.sub(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>", "", value, flags=re.DOTALL)  # footnotes
    # a template's *args* often hold the real value (e.g. {{vgrelease|June 2, 2020}}); drop the
    # template name + braces but keep the args as text, rather than nuking the whole value.
    value = re.sub(
        r"\{\{([^{}]*)\}\}", lambda m: re.sub(r"^[^|]*\|?", "", m.group(1)).replace("|", " "), value
    )
    value = re.sub(r"\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", value)  # [[link|text]] -> text
    value = re.sub(r"</?[a-zA-Z][^>]*>", " ", value)  # stray html (<br/>, <small>)
    value = re.sub(r"\s+", " ", value).strip(" |")
    return value[:_MAX_VALUE]


def parse_facets(wikitext: str) -> dict[str, str]:
    """Pull a few infobox facet params from raw wikitext (raw/unparsed by design)."""
    out: dict[str, str] = {}
    for match in _PARAM_RE.finditer(wikitext):
        key = re.sub(r"\s+", " ", match.group(1)).strip().lower()
        value = _clean(match.group(2))
        if value and key not in out:
            out[key] = value
    return out


async def wiki_hints(
    client: httpx.AsyncClient, title: str, *, want_facets: bool = False
) -> WikiHints | None:
    """Stage 1 (page exists?) always; stage 2 (infobox facets) only when ``want_facets``.

    Best-effort: any failure (no page, network, bad payload) yields ``None``.
    """
    try:
        found = cast(
            "dict[str, Any]",
            await get_json(client, _SEARCH, params={"q": title, "limit": "1"}),
        )
    except Exception as exc:  # keyless best-effort — never break a lookup
        log.warning("wiki.search_error", title=title, error=str(exc))
        return None
    pages = cast("list[dict[str, Any]]", found.get("pages", []))
    if not pages:
        return None
    page = pages[0]
    key = str(page.get("key", "")).strip()
    page_title = str(page.get("title", title)).strip()
    if not key:
        return None
    hints = WikiHints(page_title=page_title, url=_ARTICLE + key, exists=True)
    if not want_facets:
        return hints
    try:
        detail = cast("dict[str, Any]", await get_json(client, _PAGE + key))
    except Exception as exc:
        log.warning("wiki.page_error", title=title, error=str(exc))
        return hints  # keep the exists-hint even if the section fetch failed
    sections = parse_facets(str(detail.get("source", "")))
    return WikiHints(page_title=page_title, url=hints.url, exists=True, sections=sections)
