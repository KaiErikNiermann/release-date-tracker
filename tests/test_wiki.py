"""Tests for the Wikipedia hint parser (pure; the fetch is best-effort HTTP)."""

from __future__ import annotations

from release_tracker.sources.wiki import WikiHints, parse_facets

_INFOBOX = """
{{Infobox video game
| title       = Valorant
| developer   = [[Riot Games]]
| publisher   = Riot Games
| platforms   = [[Microsoft Windows|Windows]]<ref>c</ref>, PlayStation 5
| released    = {{vgrelease|June 2, 2020}}
| genre       = Hero shooter
}}
'''Valorant''' is a 2020 first-person shooter.
"""


def test_parse_facets_pulls_platforms_and_release() -> None:
    facets = parse_facets(_INFOBOX)
    # link markup stripped, footnotes removed, value kept readable
    assert facets["platforms"] == "Windows, PlayStation 5"
    assert "June 2, 2020" in facets["released"]
    assert "genre" not in facets  # not a tracked facet param


def test_parse_facets_empty_on_no_infobox() -> None:
    assert parse_facets("Just prose, no infobox params here.") == {}


def test_to_dict_omits_empty_sections() -> None:
    bare = WikiHints("Foo", "https://en.wikipedia.org/wiki/Foo", exists=True)
    assert "sections" not in bare.to_dict()
    rich = WikiHints("Foo", "https://e/Foo", exists=True, sections={"platforms": "PC"})
    assert rich.to_dict()["sections"] == {"platforms": "PC"}
