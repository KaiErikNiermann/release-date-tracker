"""Tests for tech classification + per-category policy."""

from __future__ import annotations

import pytest

from release_tracker.tech import (
    CATEGORY_INFO,
    TechCategory,
    classify_tech,
    looks_like_tech,
    tech_info,
)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("iPhone 18 Pro", TechCategory.PHONE),
        ("Samsung Galaxy S26 Ultra", TechCategory.PHONE),
        ("Galaxy Watch 8", TechCategory.WEARABLE),  # watch beats the "galaxy" phone rule
        ("Galaxy Tab S11", TechCategory.TABLET),  # tab beats phone too
        ("NVIDIA RTX 5090", TechCategory.GPU),
        ("AMD Ryzen 9 9950X3D", TechCategory.CPU),
        ("PlayStation 6", TechCategory.CONSOLE),
        ("Nintendo Switch 2", TechCategory.CONSOLE),
        ("MacBook Pro M5", TechCategory.LAPTOP),
        ("iPad Pro 2026", TechCategory.TABLET),
        ("AirPods Pro 3", TechCategory.AUDIO),
        ("LG OLED TV C6", TechCategory.TV),
        ("Sony Alpha A7 VI", TechCategory.CAMERA),
        ("DJI Mavic 5", TechCategory.DRONE),
        ("Mac Mini M5", TechCategory.DESKTOP),
    ],
)
def test_classify_tech(title: str, expected: TechCategory) -> None:
    assert classify_tech(title) is expected


def test_unknown_title_is_other_and_not_tech() -> None:
    assert classify_tech("Dune Part Three") is TechCategory.OTHER
    assert looks_like_tech("Dune Part Three") is False
    assert looks_like_tech("RTX 5090") is True


def test_every_category_has_policy() -> None:
    for category in TechCategory:
        info = tech_info(category)
        assert info.preferred_sources, f"{category} needs at least one source"
        assert isinstance(info.price_volatile, bool)
    assert set(CATEGORY_INFO) == set(TechCategory)


def test_component_heavy_categories_flag_price_volatility() -> None:
    assert tech_info(TechCategory.GPU).price_volatile is True
    assert tech_info(TechCategory.CPU).price_volatile is True
    assert tech_info(TechCategory.AUDIO).price_volatile is False
