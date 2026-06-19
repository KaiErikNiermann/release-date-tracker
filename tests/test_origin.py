"""Tests for anime-origin detection — the heuristic that replaced MediaKind.ANIME.

Anime is a medium/origin (orthogonal to movie-vs-series format), surfaced as a
DescriptorKind.ORIGIN tag. Detection keys off "Animation genre + a Japanese-origin
signal", with origin *country* (not language) load-bearing so JP/US co-pros that
air in English still register.
"""

from __future__ import annotations

from release_tracker.models import MediaKind
from release_tracker.sources.tmdb import is_anime


def test_anime_kind_is_gone() -> None:
    # the whole point: anime is no longer a format-kind
    assert not hasattr(MediaKind, "ANIME")
    assert "anime" not in {k.value for k in MediaKind}


def test_japanese_language_animation_is_anime() -> None:
    detail = {"original_language": "ja", "origin_country": ["JP"]}
    assert is_anime(detail, ("Animation", "Drama")) is True


def test_jp_us_coproduction_in_english_is_anime() -> None:
    # Lazarus: original_language 'en', but JP origin + Animation -> still anime
    detail = {"original_language": "en", "origin_country": ["JP", "US"]}
    assert is_anime(detail, ("Animation", "Sci-Fi & Fantasy")) is True


def test_jp_via_production_countries_is_anime() -> None:
    # movies carry the country in production_countries rather than origin_country
    detail = {"original_language": "ja", "production_countries": [{"iso_3166_1": "JP"}]}
    assert is_anime(detail, ("Animation",)) is True


def test_western_animation_is_not_anime() -> None:
    detail = {"original_language": "en", "origin_country": ["US"]}
    assert is_anime(detail, ("Animation", "Comedy")) is False


def test_japanese_live_action_is_not_anime() -> None:
    # JP origin but no Animation genre -> a live-action J-drama, not anime
    detail = {"original_language": "ja", "origin_country": ["JP"]}
    assert is_anime(detail, ("Drama", "Crime")) is False
