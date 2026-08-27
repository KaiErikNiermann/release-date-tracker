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


@pytest.mark.parametrize(
    "title",
    [
        "Poco X7",
        "Poco X70",
        "Realme 14 Pro",
        "Honor Magic7",
        "Honor Magic 7",
        "Motorola Edge 60",
        "Moto G Power",
        "Oppo Find X8",
        "Asus Zenfone 12 Ultra",
        "Vivo X200",
    ],
)
def test_mid_range_phone_brands_are_recognised(title: str) -> None:
    """These dominate the mid-range and were missing entirely, so `looks_like_tech` said no
    and the add screen's tech retry never fired for them."""
    assert classify_tech(title) is TechCategory.PHONE


@pytest.mark.parametrize(
    "title",
    ["Vivo", "Honor", "Poco", "Honor Society", "Dune", "Blade", "Severance", "Weapons"],
)
def test_brand_words_that_are_also_titles_stay_out_of_tech(title: str) -> None:
    """Vivo is a 2021 film and Honor/Poco are ordinary words, so those brands only match with
    a model number attached. A false positive here would route a film into the tech branch.
    """
    assert looks_like_tech(title) is False
