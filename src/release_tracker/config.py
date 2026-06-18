"""Runtime configuration loaded from environment / ``.env`` (never committed)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    @property
    def regions(self) -> tuple[str, ...]:
        return tuple(r.strip().upper() for r in self.regions_raw.split(",") if r.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
