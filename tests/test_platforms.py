"""Tests for the self-growing distributor -> streaming-home map (no network)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from release_tracker.platforms import (
    UNDETERMINED,
    PlatformStore,
    canonical_platform,
    resolve_predicted_platform,
)


def _store(tmp_path: Path) -> PlatformStore:
    return PlatformStore(tmp_path / "platforms.db")


class _Resolver:
    """Fake LLM resolver that records calls and returns a scripted answer."""

    def __init__(self, answer: str | None) -> None:
        self.answer = answer
        self.calls: list[str] = []

    async def __call__(self, studio: str) -> str | None:
        self.calls.append(studio)
        return self.answer


def test_store_roundtrip_and_ttl(tmp_path: Path) -> None:
    store = PlatformStore(tmp_path / "p.db", ttl_days=0)  # everything instantly stale
    store.put("acme", "Acme", "Netflix")
    assert store.get("acme") is None  # ttl_days=0 -> already expired
    fresh = PlatformStore(tmp_path / "q.db", ttl_days=90)
    fresh.put("acme", "Acme", "Netflix")
    assert fresh.get("acme") == "Netflix"
    assert fresh.get("missing") is None


def test_hand_table_short_circuits_without_learning(tmp_path: Path) -> None:
    resolver = _Resolver("WRONG")
    # "Marvel Studios" is in the hand table -> Disney+, resolver must not be called.
    out = asyncio.run(resolve_predicted_platform(("Marvel Studios",), _store(tmp_path), resolver))
    assert out == "Disney+"
    assert resolver.calls == []


def test_unknown_studio_is_learned_then_cached(tmp_path: Path) -> None:
    store = _store(tmp_path)
    resolver = _Resolver("Mubi")
    studios = ("Tiny Indie Distributor",)

    first = asyncio.run(resolve_predicted_platform(studios, store, resolver))
    assert first == "Mubi"
    assert resolver.calls == ["Tiny Indie Distributor"]

    # second lookup hits the store, resolver is not called again
    second = asyncio.run(resolve_predicted_platform(studios, store, resolver))
    assert second == "Mubi"
    assert resolver.calls == ["Tiny Indie Distributor"]


def test_unresolvable_studio_persists_undetermined_and_stops_reasking(tmp_path: Path) -> None:
    store = _store(tmp_path)
    resolver = _Resolver(None)  # model can't say
    studios = ("Obscure Films Ltd",)

    out = asyncio.run(resolve_predicted_platform(studios, store, resolver))
    assert out is None
    assert store.get("obscure films ltd") == UNDETERMINED

    # cached Undetermined -> returns None without a second resolver call
    again = asyncio.run(resolve_predicted_platform(studios, store, resolver))
    assert again is None
    assert resolver.calls == ["Obscure Films Ltd"]


def test_cannot_learn_skips_resolver(tmp_path: Path) -> None:
    store = _store(tmp_path)
    resolver = _Resolver("Netflix")
    out = asyncio.run(resolve_predicted_platform(("Unknown Co",), store, resolver, can_learn=False))
    assert out is None
    assert resolver.calls == []
    assert store.get("unknown co") is None  # nothing poisoned


# --- cross-provider naming ----------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("Paramount Plus Premium", "Paramount+"),
        ("Paramount+ with Showtime", "Paramount+"),
        ("Paramount Plus Basic with Ads", "Paramount+"),
        ("Netflix Standard with Ads", "Netflix"),
        ("Paramount+ Amazon Channel", "Paramount+"),
        ("Max", "HBO Max"),
        ("Amazon Prime Video", "Prime Video"),
    ],
)
def test_canonical_platform_collapses_provider_dressing(raw: str, want: str) -> None:
    """TMDB and JustWatch spell one service several ways; each became its own node."""
    assert canonical_platform(raw) == want


@pytest.mark.parametrize(
    "name", ["Canal+ Séries", "Movistar Plus+ Ficción Total", "U-NEXT", "fuboTV"]
)
def test_canonical_platform_leaves_an_unknown_name_alone(name: str) -> None:
    """No fuzzy matching: a wrong merge silently claims a service carries something it does
    not and is invisible in the UI, so an unrecognised name passes through verbatim."""
    assert canonical_platform(name) == name


def test_canonical_platform_does_not_strip_a_tier_word_that_is_the_name() -> None:
    """ "Showtime" ends in no tier; a greedy suffix strip would have to leave it whole."""
    assert canonical_platform("Showtime") == "Showtime"
