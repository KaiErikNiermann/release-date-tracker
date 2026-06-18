"""Tests for the self-growing distributor -> streaming-home map (no network)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from release_tracker.platforms import (
    UNDETERMINED,
    PlatformStore,
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
