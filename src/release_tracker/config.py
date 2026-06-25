"""Runtime configuration loaded from environment / ``.env`` (never committed)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv_lower(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated accept-list to a lowercased tuple (facet values are lowercased)."""
    return tuple(v.strip().lower() for v in raw.split(",") if v.strip())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
    )

    # --- LLM gap-filler ---
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")

    # --- TMDB (movies/TV) ---
    tmdb_api_key: str | None = Field(default=None, alias="TMDB_API_KEY")

    # --- IGDB (games) via Twitch ---
    twitch_client_id: str | None = Field(default=None, alias="TWITCH_CLIENT_ID")
    twitch_client_secret: str | None = Field(default=None, alias="TWITCH_CLIENT_SECRET")

    # --- Notion seed (optional) ---
    notion_token: str | None = Field(default=None, alias="NOTION_TOKEN")
    notion_database_id: str | None = Field(default=None, alias="NOTION_DATABASE_ID")

    # --- defaults ---
    # CSV of accepted ISO-2 regions; the sentinel ANY (or *) means "region never gates me"
    # (e.g. a VPN makes region-locks inapplicable) — see contingency.matcher_from_settings.
    regions_raw: str = Field(default="US,DE,GB", alias="RDT_REGIONS")
    db_path: Path = Field(default=Path("data/releases.db"), alias="RDT_DB_PATH")
    seeds_path: Path = Field(default=Path("local/seeds.json"), alias="RDT_SEEDS_PATH")
    # derived, disposable cache of mined studio release-timing trends (separate
    # from db_path so the stateless `rdt rd` lookup never opens the entity DB)
    trend_cache_path: Path = Field(
        default=Path("data/trends_cache.db"), alias="RDT_TREND_CACHE_PATH"
    )
    # self-growing distributor -> streaming-home map, learned as we meet new studios
    platform_db_path: Path = Field(default=Path("data/platforms.db"), alias="RDT_PLATFORM_DB_PATH")

    # --- JustWatch offer scan (the "earliest digital + where" source for film/TV) ---
    # On by default (keyless). The basket is the set of early-digital-window markets the offer
    # query fans across to find the global-earliest VOD date + its storefront (a VPN target).
    # Deliberately NOT tied to `regions` (which gates "available to me"): here we want the
    # earliest date *anywhere*, region-blind, then report where it is.
    justwatch_enabled: bool = Field(default=True, alias="RDT_JUSTWATCH")
    justwatch_regions_raw: str = Field(
        default="US,CA,GB,IE,AU,DE,FR,IT,ES,NL,JP,BR", alias="RDT_JUSTWATCH_REGIONS"
    )

    # --- consumption / availability ---
    # which release channel decides "available to me": digital (can't do theatrical),
    # theatrical, or any (soonest). Drives the upcoming/available split.
    availability_channel: Literal["digital", "theatrical", "any"] = Field(
        default="digital", alias="RDT_AVAILABILITY_CHANNEL"
    )

    # --- contingency profile: what I own / accept (gates "available to me") ---
    # Each is an empty-by-default accept-set; empty => I don't constrain that dimension
    # (wildcard), so availability is unchanged until I opt in. Region (above) is the one
    # pre-populated dimension. Custom dims: "dim:v1|v2;dim2:v3".
    platforms_raw: str = Field(default="", alias="RDT_PLATFORMS")  # "ps5,pc"
    os_raw: str = Field(default="", alias="RDT_OS")  # "linux,windows"
    languages_raw: str = Field(default="", alias="RDT_LANGUAGES")  # "en"
    tech_raw: str = Field(default="", alias="RDT_TECH")  # "ray_tracing"
    custom_contingencies_raw: str = Field(default="", alias="RDT_CUSTOM_CONTINGENCIES")

    # --- data freshness thresholds (days) ---
    fresh_days: int = Field(default=14, alias="RDT_FRESH_DAYS")  # green if refreshed within
    stale_days: int = Field(default=60, alias="RDT_STALE_DAYS")  # orange up to here, then red

    # --- display colors (rich styles; override for color-vision needs) ---
    # Default to a high-contrast, color-blind-safe ramp (cyan / orange / red) rather than
    # green / yellow, which red-green color blindness can't separate. Any rich color works
    # (e.g. RDT_CONFIRMED_COLOR="blue", "bright_cyan", "#0072B2").
    confirmed_color: str = Field(default="cyan", alias="RDT_CONFIRMED_COLOR")
    speculative_color: str = Field(default="orange1", alias="RDT_SPECULATIVE_COLOR")
    fresh_color: str = Field(default="cyan", alias="RDT_FRESH_COLOR")
    aging_color: str = Field(default="orange1", alias="RDT_AGING_COLOR")
    stale_color: str = Field(default="red", alias="RDT_STALE_COLOR")

    @property
    def regions(self) -> tuple[str, ...]:
        return tuple(r.strip().upper() for r in self.regions_raw.split(",") if r.strip())

    @property
    def justwatch_regions(self) -> tuple[str, ...]:
        return tuple(r.strip().upper() for r in self.justwatch_regions_raw.split(",") if r.strip())

    @property
    def platforms(self) -> tuple[str, ...]:
        return _csv_lower(self.platforms_raw)

    @property
    def os_targets(self) -> tuple[str, ...]:
        return _csv_lower(self.os_raw)

    @property
    def languages(self) -> tuple[str, ...]:
        return _csv_lower(self.languages_raw)

    @property
    def tech_available(self) -> tuple[str, ...]:
        return _csv_lower(self.tech_raw)

    @property
    def custom_contingencies(self) -> dict[str, frozenset[str]]:
        """Parse "dim:v1|v2;dim2:v3" into {dim: {values}} (lowercased)."""
        out: dict[str, frozenset[str]] = {}
        for clause in self.custom_contingencies_raw.split(";"):
            dim, _, vals = clause.partition(":")
            dim = dim.strip().lower()
            values = frozenset(v.strip().lower() for v in vals.split("|") if v.strip())
            if dim and values:
                out[dim] = values
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
