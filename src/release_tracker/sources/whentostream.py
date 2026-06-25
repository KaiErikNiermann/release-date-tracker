"""When To Stream — a corroboration + SVOD-drop source for movies.

JustWatch tells us the *global-earliest* VOD date by scanning storefronts; When To Stream
(whentostream.com) complements it with two things JustWatch/TMDB don't give cleanly:

* a US **PVOD** (premium buy/rent) date to **corroborate** the digital window — an independent
  read that flags an anomaly when JustWatch surfaces a suspiciously-early regional offer;
* the **SVOD** (subscription) drop date *with the named service* (Disney+, Netflix…), which
  neither TMDB nor JustWatch predicts ahead of the offer actually going live.

The site is a WordPress blog with one article per film at a stable ``<slug>-<year>`` URL whose
body carries a consistent ``PVOD Release Date : <date>`` / ``SVOD Release Date : <date> (Service)``
block. We construct the slug, fetch the article, and regex those lines — dates are US windows.

Contract mirrors :mod:`release_tracker.sources.ddg` / :mod:`release_tracker.sources.wiki`:
best-effort, **never raises** (a miss/outage/unparseable page yields ``None``), and ``to_dict``
omits empty fields. The tool only parses the *dates* — the article's prose reasoning is left for
the skill/LLM to read, not the parser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

import httpx

from release_tracker.logging import get_logger
from release_tracker.models import MediaKind
from release_tracker.sources.base import get_text

log = get_logger("whentostream")

_BASE = "https://whentostream.com"
# the slug year can lag/lead our canonical year by one (theatrical vs listed) — try neighbours.
_YEAR_OFFSETS = (0, -1, 1)
_OG_TITLE = re.compile(r'<meta property="og:title" content="([^"]+)"')
# each date line: "<Label> Release Date : April 28, 2026" (SVOD also has a "(Service)" suffix).
_THEATRICAL = re.compile(r"Theatrical Release Date\s*:?\s*([A-Z][a-z]+ \d{1,2},?\s*\d{4})")
_PVOD = re.compile(r"PVOD Release Date\s*:?\s*([A-Z][a-z]+ \d{1,2},?\s*\d{4})")
_SVOD = re.compile(r"SVOD Release Date\s*:?\s*([A-Z][a-z]+ \d{1,2},?\s*\d{4})\s*\(([^)]+)\)")
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class WhenToStreamHints:
    """US theatrical / PVOD / SVOD dates for a film, mined from its When To Stream article."""

    title: str
    url: str
    theatrical: date | None
    pvod: date | None  # US premium buy/rent date — corroborates the digital window
    svod_date: date | None  # US subscription-streaming drop date
    svod_service: str | None  # the named SVOD home (Disney+, Netflix, Max…)

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"title": self.title, "url": self.url}
        if self.theatrical is not None:
            out["theatrical"] = self.theatrical.isoformat()
        if self.pvod is not None:
            out["pvod"] = self.pvod.isoformat()
        if self.svod_date is not None:
            out["svod_date"] = self.svod_date.isoformat()
        if self.svod_service is not None:
            out["svod_service"] = self.svod_service
        return out


def slugify(title: str) -> str:
    """Match When To Stream's slug convention: lowercase, drop apostrophes, non-alnum to hyphen."""
    s = title.casefold().replace("&", " and ").replace("'", "").replace("’", "")  # noqa: RUF001
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def parse_date(value: str) -> date | None:
    """Parse a 'Month D, YYYY' (comma optional) US date string."""
    cleaned = re.sub(r"\s+", " ", value.replace(",", " ")).strip()
    try:
        return datetime.strptime(cleaned, "%B %d %Y").date()  # noqa: DTZ007 — date-only, no tz
    except ValueError:
        return None


def parse_article(html: str, url: str) -> WhenToStreamHints | None:
    """Pure: pull the title + theatrical/PVOD/SVOD dates from an article. None if none parse."""
    title_m = _OG_TITLE.search(html)
    title = title_m.group(1).split(" - When To Stream")[0].strip() if title_m else ""
    text = _WS.sub(" ", _TAGS.sub(" ", html))
    theatrical = _first_date(_THEATRICAL, text)
    pvod = _first_date(_PVOD, text)
    svod_m = _SVOD.search(text)
    svod_date = parse_date(svod_m.group(1)) if svod_m else None
    svod_service = svod_m.group(2).strip() if svod_m else None
    if not title or not (theatrical or pvod or svod_date):
        return None  # wrong/empty page — nothing worth attaching
    return WhenToStreamHints(
        title=title,
        url=url,
        theatrical=theatrical,
        pvod=pvod,
        svod_date=svod_date,
        svod_service=svod_service,
    )


def _first_date(pattern: re.Pattern[str], text: str) -> date | None:
    m = pattern.search(text)
    return parse_date(m.group(1)) if m else None


async def hints(
    client: httpx.AsyncClient, title: str, *, kind: MediaKind, year: int | None
) -> WhenToStreamHints | None:
    """Best-effort When To Stream lookup for a movie. Never raises — a miss yields None.

    Tries ``<slug>-<year>`` for the canonical year and its neighbours (the article slug's year
    can differ from TMDB's by one). Returns the first article that parses, else None.
    """
    if kind is not MediaKind.MOVIE or year is None:
        return None  # the site only covers theatrical films heading to streaming
    slug = slugify(title)
    if not slug:
        return None
    for offset in _YEAR_OFFSETS:
        url = f"{_BASE}/{slug}-{year + offset}/"
        html = await _safe_get(client, url)
        parsed = parse_article(html, url) if html is not None else None
        if parsed is not None:
            return parsed
    log.info("whentostream.miss", title=title, year=year)
    return None


async def _safe_get(client: httpx.AsyncClient, url: str) -> str | None:
    """GET the article text, or None on any error — a 404 for the wrong slug year is expected."""
    try:
        return await get_text(client, url)
    except Exception as exc:
        log.debug("whentostream.fetch_miss", url=url, error=str(exc))
        return None
