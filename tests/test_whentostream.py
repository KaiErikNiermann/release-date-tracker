"""Tests for the When To Stream source (pure slug/date/article parsing; fetch is best-effort)."""

from __future__ import annotations

from datetime import date

from release_tracker.sources.whentostream import parse_article, parse_date, slugify

# a trimmed article, in the site's real shape (og:title + a PVOD/SVOD block in the body).
_ARTICLE = """
<html><head>
<meta property="og:title" content="Hoppers (2026) - When To Stream" />
</head><body>
<p>A normal world beyond anything she imagined.</p>
<p>PVOD Release Date : April 28, 2026</p>
<p>SVOD Release Date : June 3, 2026 (Disney+)</p>
</body></html>
"""

_THEATRICAL_ONLY = """
<meta property="og:title" content="Some Film (2027) - When To Stream" />
<p>Theatrical Release Date : December 25, 2027</p>
"""


def test_slugify_matches_site_convention() -> None:
    assert slugify("Hoppers") == "hoppers"
    assert slugify("Avatar: Fire and Ash") == "avatar-fire-and-ash"
    assert slugify("Hopper's Tale") == "hoppers-tale"  # apostrophe dropped, not hyphenated
    assert slugify("Tom & Jerry") == "tom-and-jerry"  # & -> and


def test_parse_date_handles_comma_optional() -> None:
    assert parse_date("April 28, 2026") == date(2026, 4, 28)
    assert parse_date("June 3 2026") == date(2026, 6, 3)
    assert parse_date("not a date") is None


def test_parse_article_extracts_pvod_svod_and_service() -> None:
    h = parse_article(_ARTICLE, "https://whentostream.com/hoppers-2026/")
    assert h is not None
    assert h.title == "Hoppers (2026)"  # the " - When To Stream" suffix is stripped
    assert h.pvod == date(2026, 4, 28)
    assert h.svod_date == date(2026, 6, 3)
    assert h.svod_service == "Disney+"
    assert h.theatrical is None


def test_parse_article_theatrical_only() -> None:
    h = parse_article(_THEATRICAL_ONLY, "https://whentostream.com/some-film-2027/")
    assert h is not None
    assert h.theatrical == date(2027, 12, 25)
    assert h.pvod is None and h.svod_date is None


def test_parse_article_none_when_no_dates() -> None:
    html = '<meta property="og:title" content="Nothing (2026) - When To Stream" /><p>No dates.</p>'
    assert parse_article(html, "https://whentostream.com/nothing-2026/") is None


def test_parse_article_to_dict_omits_empty() -> None:
    h = parse_article(_ARTICLE, "https://whentostream.com/hoppers-2026/")
    assert h is not None
    d = h.to_dict()
    assert d == {
        "title": "Hoppers (2026)",
        "url": "https://whentostream.com/hoppers-2026/",
        "pvod": "2026-04-28",
        "svod_date": "2026-06-03",
        "svod_service": "Disney+",
    }  # no "theatrical" key
