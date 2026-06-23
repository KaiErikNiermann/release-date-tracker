"""Tests for the DuckDuckGo Instant Answer fallback parser (pure, fixture-driven)."""

from __future__ import annotations

from typing import Any

from release_tracker.sources.ddg import parse_instant_answer


def _payload(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "Heading": "Apple pie",
        "AbstractText": "An apple pie is a pie in which the principal filling is apples.",
        "AbstractSource": "Wikipedia",
        "AbstractURL": "https://en.wikipedia.org/wiki/Apple_pie",
        "RelatedTopics": [],
    }
    return base | kw


def test_parses_abstract_and_source() -> None:
    info = parse_instant_answer(_payload())
    assert info is not None
    assert info.heading == "Apple pie"
    assert info.source == "Wikipedia"
    assert info.url == "https://en.wikipedia.org/wiki/Apple_pie"
    assert info.abstract.startswith("An apple pie")


def test_empty_payload_is_a_miss() -> None:
    # no abstract and no related links -> None, so the caller omits the field entirely
    assert parse_instant_answer(_payload(AbstractText="", RelatedTopics=[])) is None


def test_related_links_flatten_and_cap() -> None:
    related = [
        {"Text": "A24 (company)", "FirstURL": "https://example.com/a24"},
        {
            "Topics": [  # nested grouping — flattened one level
                {"Text": "A24 films", "FirstURL": "https://example.com/films"},
                {"Text": "no url here"},  # dropped: missing FirstURL
            ]
        },
        *[{"Text": f"t{i}", "FirstURL": f"https://example.com/{i}"} for i in range(10)],
    ]
    info = parse_instant_answer(_payload(AbstractText="", RelatedTopics=related), max_related=3)
    assert info is not None
    assert len(info.related) == 3  # capped
    assert info.related[0] == ("A24 (company)", "https://example.com/a24")
    assert info.related[1] == ("A24 films", "https://example.com/films")


def test_related_only_still_yields_info() -> None:
    # an abstract-less answer that still has links is useful context, not a miss
    info = parse_instant_answer(
        _payload(AbstractText="", RelatedTopics=[{"Text": "x", "FirstURL": "https://e.com/x"}])
    )
    assert info is not None
    assert info.abstract == ""
    assert info.related == (("x", "https://e.com/x"),)


def test_to_dict_shape() -> None:
    d = parse_instant_answer(_payload()).to_dict()  # type: ignore[union-attr]
    assert set(d) == {"heading", "abstract", "source", "url", "related"}
    assert isinstance(d["related"], list)
