"""Runtime configuration, layered so the app can write some of it and you can override all of it.

Sources, first match wins: real environment variables, then ``config.toml`` (the file the
settings screen and ``rdt config set`` write), then the ``.env`` chain. The environment
staying on top is what makes a one-off ``TMDB_API_KEY=… rdt …`` work, and what makes a
CI runner immune to whatever a developer once typed into the TUI.

Paths follow the XDG base directories (via ``platformdirs``), because an installed ``rdt``
is invoked from wherever you happen to be standing and must find the *same* tracker every
time. The one exception is a checkout that already has a ``data/releases.db`` beside it —
see ``_legacy_project_dir``. Every path stays overridable by its ``RDT_*`` variable.
"""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, Literal

from platformdirs import PlatformDirs
from pydantic import Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


def _dirs() -> PlatformDirs:
    """The XDG base dirs for this app, resolved fresh each time.

    Deliberately not a module-level constant: ``platformdirs`` reads ``XDG_*`` at
    construction, so caching one would freeze the layout at import. ``appauthor=False``
    keeps Windows at ``%LOCALAPPDATA%\\rdt`` rather than ``%LOCALAPPDATA%\\rdt\\rdt``.
    """
    return PlatformDirs(appname="rdt", appauthor=False)


CONFIG_FILE_ENV = "RDT_CONFIG_FILE"


class ConfigFileError(RuntimeError):
    """``config.toml`` could not be parsed. Carries the path, so it can be pointed at."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def config_file_path() -> Path:
    """The writable config file. ``RDT_CONFIG_FILE`` wins, else ``<config dir>/config.toml``."""
    override = os.environ.get(CONFIG_FILE_ENV)
    return Path(override) if override else Path(_dirs().user_config_dir) / "config.toml"


def env_file_paths() -> tuple[Path, Path]:
    """The ``.env`` chain, user-wide then project-local — last wins, so a checkout can point
    at its own keys without disturbing the installed CLI."""
    return Path(_dirs().user_config_dir) / ".env", Path(".env")


class _TomlSource(TomlConfigSettingsSource):
    """``config.toml``, with a parse failure that names the file instead of a traceback.

    Unwrapped, a stray quote in that file raises out of *every* ``get_settings()`` call —
    including the command you would use to fix it.
    """

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls, toml_file=config_file_path())

    def _read_file(self, file_path: Path | Traversable) -> dict[str, Any]:
        try:
            with file_path.open("rb") as handle:
                return tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigFileError(Path(str(file_path)), str(exc)) from exc


# "region doesn't gate me" (a VPN). Lives here, beside the `RDT_REGIONS` field it is a value
# of, because both the availability gate (`contingency.matcher_from_settings`) and every
# provider lookup have to recognise it — the gate drops the dimension, a lookup reads *past*
# it to a real market. Re-exported from `contingency` for callers that think in dimensions.
REGION_WILDCARD: frozenset[str] = frozenset({"ANY", "*"})


def _csv_lower(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated accept-list to a lowercased tuple (facet values are lowercased)."""
    return tuple(v.strip().lower() for v in raw.split(",") if v.strip())


def _legacy_project_dir() -> Path | None:
    """The pre-XDG layout — a ``data/`` beside the working directory — if it is in use.

    Honoured only when it already holds a database, so a checkout that has been tracking
    titles keeps its tracker instead of silently opening an empty one under
    ``~/.local/share`` after an upgrade. A fresh install never takes this branch.
    """
    legacy = Path("data")
    return legacy if (legacy / "releases.db").is_file() else None


def _path(kind: Literal["data", "cache", "config"], name: str, legacy: str) -> Path:
    """``name`` under an XDG base dir — or the exact ``legacy`` path if that layout is live.

    The legacy spelling is passed in per file rather than derived, because the old layout
    split them (``data/`` for databases, ``local/`` for hand-authored seeds) and an upgrade
    must not quietly start looking for a seeds file somewhere it was never written.
    """
    if _legacy_project_dir() is not None:
        return Path(legacy)
    dirs = _dirs()
    match kind:
        case "data":
            base = dirs.user_data_dir
        case "cache":
            base = dirs.user_cache_dir
        case "config":
            base = dirs.user_config_dir
    return Path(base) / name


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file_encoding="utf-8", env_prefix="", extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Highest priority first: env, then the written config, then the ``.env`` chain.

        Both file sources are built *here* rather than in ``model_config`` because the
        paths come from ``_dirs()``, which reads ``XDG_*`` when it is called — naming them
        at class-definition time froze the layout at import, which is exactly what
        ``_dirs()`` exists to avoid.
        """
        del dotenv_settings  # rebuilt below, so its paths are resolved now rather than at import
        return (
            init_settings,
            env_settings,
            _TomlSource(settings_cls),
            DotEnvSettingsSource(
                settings_cls, env_file=env_file_paths(), env_file_encoding="utf-8"
            ),
            file_secret_settings,
        )

    # --- LLM gap-filler ---
    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")

    # --- TMDB (movies/TV) ---
    tmdb_api_key: SecretStr | None = Field(default=None, alias="TMDB_API_KEY")

    # --- IGDB (games) via Twitch ---
    twitch_client_id: SecretStr | None = Field(default=None, alias="TWITCH_CLIENT_ID")
    twitch_client_secret: SecretStr | None = Field(default=None, alias="TWITCH_CLIENT_SECRET")

    # --- Notion seed (optional) ---
    notion_token: SecretStr | None = Field(default=None, alias="NOTION_TOKEN")
    notion_database_id: SecretStr | None = Field(default=None, alias="NOTION_DATABASE_ID")

    # --- defaults ---
    # CSV of accepted ISO-2 regions; the sentinel ANY (or *) means "region never gates me"
    # (e.g. a VPN makes region-locks inapplicable) — see contingency.matcher_from_settings.
    # Read it through `provider_regions` before handing it to any API: the sentinel is a
    # profile value, not a market code.
    regions_raw: str = Field(default="US,DE,GB", alias="RDT_REGIONS")
    db_path: Path = Field(
        default_factory=lambda: _path("data", "releases.db", "data/releases.db"),
        alias="RDT_DB_PATH",
    )
    seeds_path: Path = Field(
        default_factory=lambda: _path("config", "seeds.json", "local/seeds.json"),
        alias="RDT_SEEDS_PATH",
    )
    # derived, disposable cache of mined studio release-timing trends (separate
    # from db_path so the stateless `rdt rd` lookup never opens the entity DB)
    trend_cache_path: Path = Field(
        default_factory=lambda: _path("cache", "trends_cache.db", "data/trends_cache.db"),
        alias="RDT_TREND_CACHE_PATH",
    )
    # self-growing distributor -> streaming-home map, learned as we meet new studios.
    # Data, not cache: rebuilding it costs the LLM calls that taught it each studio.
    platform_db_path: Path = Field(
        default_factory=lambda: _path("data", "platforms.db", "data/platforms.db"),
        alias="RDT_PLATFORM_DB_PATH",
    )

    # --- JustWatch offer scan (the "earliest digital + where" source for film/TV) ---
    # On by default (keyless). The basket is the set of early-digital-window markets the offer
    # query fans across to find the global-earliest VOD date + its storefront (a VPN target).
    # Deliberately NOT tied to `regions` (which gates "available to me"): here we want the
    # earliest date *anywhere*, region-blind, then report where it is.
    justwatch_enabled: bool = Field(default=True, alias="RDT_JUSTWATCH")
    justwatch_regions_raw: str = Field(
        default="US,CA,GB,IE,AU,DE,FR,IT,ES,NL,JP,BR", alias="RDT_JUSTWATCH_REGIONS"
    )
    # When To Stream (movies): a US PVOD/SVOD scrape that corroborates the digital window and
    # adds the predicted subscription-drop date + service. Best-effort; on by default.
    whentostream_enabled: bool = Field(default=True, alias="RDT_WHENTOSTREAM")

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
    def provider_regions(self) -> tuple[str, ...]:
        """The markets an *API* lookup should ask about — never the raw `regions`.

        ``regions`` is a contingency profile, so it may hold the ``ANY``/``*`` wildcard meaning
        "region does not gate me". That is not an ISO-2 code any provider keys on: looked up
        literally it matches nothing, and TMDB's whole ~100-market table is not a useful answer
        either. Fall back to the offer-scan basket — the markets already configured as worth
        looking at — so a VPN user gets the majors instead of nothing.
        """
        if REGION_WILDCARD & frozenset(self.regions):
            return self.justwatch_regions
        return self.regions

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


def field_name_for(alias: str) -> str | None:
    """The attribute behind an environment-variable name, or None if there isn't one.

    Settings are addressed by alias everywhere outside this module — the config file, the
    doctor table, the settings screen — so reading one back by name needs this reverse
    lookup. It had grown three copies, which is two more than a mapping this small deserves.
    """
    return next(
        (name for name, field in Settings.model_fields.items() if field.alias == alias), None
    )


def secret(value: SecretStr | None) -> str | None:
    """Unwrap a credential at the point it is actually used.

    The six credentials are ``SecretStr`` so they cannot leak through a ``repr``, a
    traceback or a future ``model_dump``. Every read goes through here, so the places a
    secret escapes into a request are greppable rather than scattered — and an empty
    ``SecretStr`` is falsy either way, so ``if not settings.tmdb_api_key`` still means what
    it always did.
    """
    return value.get_secret_value() if value is not None else None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
