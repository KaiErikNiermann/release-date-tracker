"""Technology category taxonomy + region/price policy for ``rdt rd``.

There is no single free, structured DB for consumer tech the way TMDB/IGDB cover
film and games — so tech is **search-first**. The engine's job is policy, not data
acquisition: it classifies the item into a category and, per category, names the
authoritative sources to consult and how to reason about price. Two hard rules
fall out of how tech actually ships, and both are encoded here:

* **Region is a hard constraint.** Unlike a streaming date you can dodge with a
  VPN, a phone / GPU / console launches and is priced *per country* (tariffs,
  taxes, FX, carrier & retailer deals). Every tech answer is scoped to one region.
* **Price anchors to the predecessor but adjusts for part-cost swings.** Component
  prices (DRAM/NAND, panels, silicon, tariffs, FX) move enough that last gen's
  MSRP can be a poor prior — the predecessor anchors the guess, it doesn't dictate.

This module is pure (no I/O): classification + a static per-category policy table,
so it is trivially testable and reused by both the CLI and the ``/rd`` skill.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class TechCategory(StrEnum):
    """Consumer-tech buckets. ``OTHER`` is the catch-all fallback."""

    PHONE = "phone"
    TABLET = "tablet"
    LAPTOP = "laptop"
    DESKTOP = "desktop"
    CPU = "cpu"
    GPU = "gpu"
    CONSOLE = "console"
    WEARABLE = "wearable"
    TV = "tv"
    AUDIO = "audio"
    CAMERA = "camera"
    DRONE = "drone"
    OTHER = "other"


@dataclass(slots=True, frozen=True)
class TechInfo:
    """Per-category policy: where to look and how volatile the price is."""

    label: str
    preferred_sources: tuple[str, ...]  # domains to scope a web search to
    price_volatile: bool  # do component costs materially move the price?
    note: str  # short category-specific guidance


# Authoritative-ish sources per category, used as WebSearch ``allowed_domains``.
CATEGORY_INFO: Final[dict[TechCategory, TechInfo]] = {
    TechCategory.PHONE: TechInfo(
        "phone",
        ("gsmarena.com", "theverge.com"),
        price_volatile=True,
        note="Phones have solid specs DBs (GSMArena); launch + price are per-country.",
    ),
    TechCategory.TABLET: TechInfo(
        "tablet",
        ("gsmarena.com", "theverge.com"),
        price_volatile=True,
        note="Tablets track close to phones; GSMArena covers most.",
    ),
    TechCategory.LAPTOP: TechInfo(
        "laptop",
        ("notebookcheck.net", "theverge.com"),
        price_volatile=True,
        note="Pricing swings with RAM/SSD/panel costs and config tiers.",
    ),
    TechCategory.DESKTOP: TechInfo(
        "desktop",
        ("tomshardware.com", "theverge.com"),
        price_volatile=True,
        note="Prebuilts/mini-PCs; price follows component (CPU/GPU/RAM) costs.",
    ),
    TechCategory.CPU: TechInfo(
        "cpu",
        ("techpowerup.com", "tomshardware.com"),
        price_volatile=True,
        note="Intel/AMD MSRP set at launch; street price moves with supply.",
    ),
    TechCategory.GPU: TechInfo(
        "gpu",
        ("techpowerup.com", "videocardz.com"),
        price_volatile=True,
        note="GPU street price diverges hard from MSRP (demand, VRAM, mining/AI).",
    ),
    TechCategory.CONSOLE: TechInfo(
        "console",
        ("theverge.com", "ign.com"),
        price_volatile=True,
        note="Console pricing is region-set; recent gens have *risen* mid-life (FX/tariffs).",
    ),
    TechCategory.WEARABLE: TechInfo(
        "wearable",
        ("gsmarena.com", "theverge.com"),
        price_volatile=False,
        note="Watches/bands; price fairly stable gen-to-gen.",
    ),
    TechCategory.TV: TechInfo(
        "tv",
        ("rtings.com", "theverge.com"),
        price_volatile=True,
        note="Panel costs and size tiers dominate; launch price drops fast.",
    ),
    TechCategory.AUDIO: TechInfo(
        "audio",
        ("rtings.com", "theverge.com"),
        price_volatile=False,
        note="Earbuds/headphones/speakers; price stable, anchor to predecessor.",
    ),
    TechCategory.CAMERA: TechInfo(
        "camera",
        ("dpreview.com", "theverge.com"),
        price_volatile=True,
        note="Sensor/lens costs and FX move body prices between generations.",
    ),
    TechCategory.DRONE: TechInfo(
        "drone",
        ("dronedj.com", "theverge.com"),
        price_volatile=False,
        note="DJI-dominated; tariffs/import rules strongly affect regional price.",
    ),
    TechCategory.OTHER: TechInfo(
        "tech",
        ("theverge.com", "tomshardware.com"),
        price_volatile=True,
        note="Unknown tech category — search broadly, scoped to the region.",
    ),
}

# Ordered most-specific-first: the first category whose keywords hit wins.
# Wearable/tablet precede phone so "Galaxy Watch"/"Galaxy Tab" aren't read as a
# "Galaxy S" phone. Each entry is (category, alternation fragments).
_KEYWORDS: Final[tuple[tuple[TechCategory, tuple[str, ...]], ...]] = (
    (
        TechCategory.GPU,
        ("rtx", "gtx", r"radeon\s+rx", "geforce", r"arc\s+[ab]\d+", r"graphics\s+card", "gpu"),
    ),
    (
        TechCategory.CPU,
        (
            "ryzen",
            "threadripper",
            r"core\s+i[3579]",
            r"core\s+ultra",
            "xeon",
            "epyc",
            "cpu",
            "processor",
        ),
    ),
    (
        TechCategory.CONSOLE,
        (
            "playstation",
            r"ps[56]",
            "xbox",
            r"nintendo\s+switch",
            r"switch\s*2",
            r"steam\s+deck",
            r"rog\s+ally",
            "console",
        ),
    ),
    (
        TechCategory.WEARABLE,
        (
            r"apple\s+watch",
            r"galaxy\s+watch",
            r"pixel\s+watch",
            r"smart\s*watch",
            "fitbit",
            "whoop",
        ),
    ),
    (TechCategory.TABLET, ("ipad", r"galaxy\s+tab", r"surface\s+pro", "tablet")),
    (
        TechCategory.LAPTOP,
        ("macbook", "thinkpad", "xps", "zenbook", r"surface\s+laptop", "laptop", "notebook"),
    ),
    (
        TechCategory.AUDIO,
        ("airpods", "earbuds", "headphones", "sonos", "soundbar", "bose", "sennheiser"),
    ),
    (TechCategory.TV, (r"oled\s+tv", "qled", "bravia", "tv", "television")),
    (
        TechCategory.CAMERA,
        ("camera", "gopro", "eos", r"nikon\s+z", r"sony\s+alpha", "fujifilm", "mirrorless"),
    ),
    (TechCategory.DRONE, ("drone", "dji", "mavic")),
    (
        TechCategory.PHONE,
        (
            "iphone",
            r"galaxy\s+[saz]\d+",
            r"pixel\s*\d+",
            "oneplus",
            "xiaomi",
            "redmi",
            r"nothing\s+phone",
            "xperia",
            "smartphone",
            "phone",
        ),
    ),
    (
        TechCategory.DESKTOP,
        (r"mac\s+mini", r"mac\s+studio", r"mac\s+pro", "nuc", "prebuilt", "desktop"),
    ),
)

_PATTERNS: Final[tuple[tuple[re.Pattern[str], TechCategory], ...]] = tuple(
    (re.compile(r"\b(?:" + "|".join(frags) + r")\b", re.IGNORECASE), cat)
    for cat, frags in _KEYWORDS
)


def classify_tech(title: str) -> TechCategory:
    """Best-effort category from the title; ``OTHER`` when nothing matches."""
    for pattern, category in _PATTERNS:
        if pattern.search(title):
            return category
    return TechCategory.OTHER


def tech_info(category: TechCategory) -> TechInfo:
    return CATEGORY_INFO[category]


def looks_like_tech(title: str) -> bool:
    """True if the title clearly names a tech product (a non-OTHER category)."""
    return classify_tech(title) is not TechCategory.OTHER
