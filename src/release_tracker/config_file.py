"""Reading and writing ``config.toml`` — the one part of the configuration the app owns.

Everything here is deliberately flat and spelled exactly like the environment variables,
because that is the only spelling that works. ``TomlConfigSettingsSource`` matches incoming
keys against each field's *alias*, so ``db_path = …`` and a ``[paths]`` section are both
parsed successfully and then dropped by ``extra="ignore"`` without a word. A file that looks
right and does nothing is the worst outcome available, so the schema is the one the loader
can actually see, and the grouping people want from sections is done with comment banners —
which are regenerated from :data:`FIELD_DOCS` on every write and therefore cannot drift.

The file is the app's to rewrite, so hand-authored comments in it are not preserved. Keys
this version does not recognise *are* preserved, because they are more likely to be a
newer rdt's than a typo.
"""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic_settings import DotEnvSettingsSource

from release_tracker.config import ConfigFileError, Settings, config_file_path, env_file_paths

__all__ = [
    "FIELD_DOCS",
    "SECRET_ALIASES",
    "MigrationReport",
    "known_aliases",
    "mask",
    "migrate_env",
    "origins",
    "read_config",
    "set_values",
]

type TomlScalar = str | int | float | bool

Group = Literal["keys", "paths", "sources", "preferences", "display"]

# Secrets, so they can be masked wherever a value is displayed. Not derived from the type,
# because `str | None` describes half the settings.
SECRET_ALIASES: Final[frozenset[str]] = frozenset(
    {
        "OPENAI_API_KEY",
        "TMDB_API_KEY",
        "TWITCH_CLIENT_ID",
        "TWITCH_CLIENT_SECRET",
        "NOTION_TOKEN",
        "NOTION_DATABASE_ID",
    }
)


@dataclass(frozen=True, slots=True)
class FieldDoc:
    """Where one setting belongs in the file, and what to say about it."""

    alias: str
    group: Group
    blurb: str


# One entry per alias on `Settings`. A test asserts the two sets match exactly, so a new
# setting cannot be added without deciding how it is described here.
FIELD_DOCS: Final[tuple[FieldDoc, ...]] = (
    FieldDoc("TMDB_API_KEY", "keys", "movies and TV: dates, credits, watch providers"),
    FieldDoc("TWITCH_CLIENT_ID", "keys", "games, via IGDB — from a Twitch application"),
    FieldDoc("TWITCH_CLIENT_SECRET", "keys", "the secret for that same application"),
    FieldDoc("OPENAI_API_KEY", "keys", "the gap-filler for what the free sources leave TBA"),
    FieldDoc("OPENAI_MODEL", "keys", "which model that gap-filler uses"),
    FieldDoc("NOTION_TOKEN", "keys", "only for the Notion seed provider"),
    FieldDoc("NOTION_DATABASE_ID", "keys", "the Notion database to seed from"),
    FieldDoc("RDT_DB_PATH", "paths", "the tracker itself — takes effect on next launch"),
    FieldDoc("RDT_SEEDS_PATH", "paths", "hand-authored seeds"),
    FieldDoc("RDT_TREND_CACHE_PATH", "paths", "mined trend cache"),
    FieldDoc("RDT_PLATFORM_DB_PATH", "paths", "the learned platform map"),
    FieldDoc("RDT_JUSTWATCH", "sources", "scan JustWatch for streaming offers"),
    FieldDoc("RDT_JUSTWATCH_REGIONS", "sources", "which markets to scan"),
    FieldDoc("RDT_WHENTOSTREAM", "sources", "corroborate digital dates with WhenToStream"),
    FieldDoc("RDT_REGIONS", "preferences", "regions to prioritise, most wanted first"),
    FieldDoc("RDT_AVAILABILITY_CHANNEL", "preferences", "what counts as available to you"),
    FieldDoc("RDT_PLATFORMS", "preferences", "platforms you can play on"),
    FieldDoc("RDT_OS", "preferences", "operating systems you run"),
    FieldDoc("RDT_LANGUAGES", "preferences", "languages you accept"),
    FieldDoc("RDT_TECH", "preferences", "devices you own"),
    FieldDoc("RDT_CUSTOM_CONTINGENCIES", "preferences", "your own facets, as dim:v1|v2;dim2:v3"),
    FieldDoc("RDT_FRESH_DAYS", "preferences", "a speculative date is fresh for this long"),
    FieldDoc("RDT_STALE_DAYS", "preferences", "...and stale after this long"),
    FieldDoc("RDT_CONFIRMED_COLOR", "display", "a confirmed date"),
    FieldDoc("RDT_SPECULATIVE_COLOR", "display", "a date we predicted"),
    FieldDoc("RDT_FRESH_COLOR", "display", "recently checked"),
    FieldDoc("RDT_AGING_COLOR", "display", "getting old"),
    FieldDoc("RDT_STALE_COLOR", "display", "worth re-checking"),
)

_GROUP_TITLES: Final[dict[Group, str]] = {
    "keys": "API keys — this file is chmod 0600. Every one is optional.",
    "paths": "where things live",
    "sources": "which optional sources to consult",
    "preferences": "what counts as available to you",
    "display": "colours",
}

_HEADER: Final = """\
# rdt configuration, written by `rdt config set` and the TUI settings screen.
#
# Keys are spelled exactly like the environment variables, and a real environment
# variable always wins over anything here. Comments are regenerated on every write.
"""


def known_aliases() -> frozenset[str]:
    """Every setting name this build understands — read off the model, never a copy."""
    return frozenset(
        field.alias for field in Settings.model_fields.values() if field.alias is not None
    )


def read_config(path: Path | None = None) -> dict[str, TomlScalar]:
    """The file's contents, or an empty mapping when there isn't one."""
    target = path or config_file_path()
    if not target.is_file():
        return {}
    try:
        with target.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigFileError(target, str(exc)) from exc
    return {key: value for key, value in raw.items() if _is_scalar(value)}


def _alias_of(name: str) -> str | None:
    """The canonical alias for a spelling, case-insensitively.

    Needed because ``DotEnvSettingsSource`` lowercases what it reads and returns *every*
    line in the file, recognised or not — so the migration has to do its own matching.
    """
    return {alias.lower(): alias for alias in known_aliases()}.get(name.lower())


def coerce(updates: Mapping[str, str]) -> dict[str, TomlScalar]:
    """Validate and type raw strings by running them through ``Settings`` itself.

    So ``RDT_FRESH_DAYS="7"`` is stored as ``7`` and a bad
    ``RDT_AVAILABILITY_CHANNEL`` raises before anything reaches the disk — using the same
    rules the loader will apply, rather than a second parser that can disagree with it.
    """
    if bad := sorted(name for name in updates if _alias_of(name) is None):
        raise KeyError(f"not a setting: {', '.join(bad)}")
    settings = Settings(**updates)  # type: ignore[arg-type] - alias-keyed, which is the contract
    by_alias = {
        field.alias: name
        for name, field in Settings.model_fields.items()
        if field.alias is not None
    }
    out: dict[str, TomlScalar] = {}
    for name in updates:
        alias = _alias_of(name)
        assert alias is not None  # guarded above
        value = getattr(settings, by_alias[alias])
        out[alias] = value if _is_scalar(value) else str(value)
    return out


def set_values(updates: Mapping[str, str | None], *, path: Path | None = None) -> Path:
    """Apply ``updates`` to the file — a ``None`` value removes that key. Returns the path.

    Read-modify-write, so anything already in the file that isn't being changed survives,
    including keys this build doesn't recognise.
    """
    target = path or config_file_path()
    unknown = known_aliases()
    if bad := sorted(set(updates) - unknown):
        raise KeyError(f"not a setting: {', '.join(bad)}")
    current = read_config(target)
    current.update(coerce({key: value for key, value in updates.items() if value is not None}))
    for alias, value in updates.items():
        if value is None:
            current.pop(alias, None)
    return write_config(current, path=target)


def write_config(values: Mapping[str, TomlScalar], *, path: Path | None = None) -> Path:
    """Render and atomically replace the file, 0600, creating the directory if needed."""
    target = path or config_file_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, _render(values))
    return target


def mask(alias: str, value: str) -> str:
    """A secret as it is safe to show: enough to recognise, not enough to use."""
    if alias not in SECRET_ALIASES or not value:
        return value
    return f"{value[:4]}…{value[-2:]}" if len(value) > 10 else "…"


def origins(path: Path | None = None) -> dict[str, str]:
    """Where each *set* value is actually coming from — the answer to "why didn't my key
    take effect", which is almost always a leftover environment variable."""
    from_file = read_config(path)
    dotenv = DotEnvSettingsSource(Settings, env_file=env_file_paths(), env_file_encoding="utf-8")()
    out: dict[str, str] = {}
    for alias in known_aliases():
        if alias in os.environ:
            out[alias] = "environment"
        elif alias in from_file:
            out[alias] = "config.toml"
        elif alias in dotenv:
            out[alias] = ".env"
    return out


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """What a one-time import lifted out of the ``.env`` chain."""

    path: Path
    aliases: tuple[str, ...]


def migrate_env(*, path: Path | None = None) -> MigrationReport | None:
    """Seed ``config.toml`` from the ``.env`` files, once. None if there is nothing to do.

    Never called from ``Settings`` or ``get_settings`` — a configuration *load* with a
    filesystem side effect would fire in every test and in CI, writing into whatever
    machine happened to run it. Callers do it explicitly, at startup.

    The ``.env`` keeps working afterwards; this only lifts its values one layer up so the
    settings screen has somewhere writable to put the next one.
    """
    target = path or config_file_path()
    if target.exists():
        return None
    # The dotenv source lowercases what it reads and hands back every line in the file,
    # recognised or not — so anything that isn't a setting is dropped here rather than
    # carried into a config file that would then refuse to load.
    found = DotEnvSettingsSource(Settings, env_file=env_file_paths(), env_file_encoding="utf-8")()
    values = {
        alias: str(value)
        for key, value in found.items()
        if (alias := _alias_of(str(key))) is not None and value is not None and str(value) != ""
    }
    if not values:
        return None
    write_config(coerce(values), path=target)
    return MigrationReport(path=target, aliases=tuple(sorted(values)))


# --- rendering ----------------------------------------------------------------------------
def _is_scalar(value: object) -> bool:
    # bool first: it is an int, and `true` must not render as `1`.
    return isinstance(value, bool | int | float | str)


def _fmt(value: TomlScalar) -> str:
    """One scalar as TOML.

    ``json.dumps`` is doing the load-bearing work for strings: TOML basic strings accept a
    superset of JSON's escapes, so it is correct for Windows paths full of backslashes and
    for anything non-ASCII — which is exactly where a hand-rolled quoter goes wrong.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    return json.dumps(value)


def _render(values: Mapping[str, TomlScalar]) -> str:
    """The whole file: a header, then a banner per group, then anything unrecognised."""
    lines = [_HEADER]
    documented = {doc.alias for doc in FIELD_DOCS}
    for group, title in _GROUP_TITLES.items():
        present = [doc for doc in FIELD_DOCS if doc.group == group and doc.alias in values]
        if not present:
            continue
        lines.append(f"\n# --- {title} ---")
        for doc in present:
            lines.append(f"# {doc.blurb}")
            lines.append(f"{doc.alias} = {_fmt(values[doc.alias])}")
    if leftovers := sorted(set(values) - documented):
        # Most likely written by a newer rdt than this one; dropping them on a round-trip
        # through an older build would be a nasty way to lose a setting.
        lines.append("\n# --- not recognised by this version of rdt, kept as-is ---")
        lines.extend(f"{alias} = {_fmt(values[alias])}" for alias in leftovers)
    return "\n".join(lines).rstrip() + "\n"


def _atomic_write(path: Path, text: str) -> None:
    """Replace ``path`` in one step, never leaving a half-written config behind.

    The mode is set at creation rather than after, so a secret is never briefly
    world-readable, and the temporary file is a sibling so the rename stays within one
    filesystem (``os.replace`` across devices fails).
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    handle = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as sink:
            sink.write(text)
            sink.flush()
            os.fsync(sink.fileno())
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
