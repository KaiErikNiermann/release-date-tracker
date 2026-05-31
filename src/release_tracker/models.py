"""Core domain model.

This is intentionally *richer* than the seed (e.g. a Notion table). The unit of
truth is a :class:`ReleaseObservation` — a single sourced claim that "entity X is
released via channel C in region R on date D (with precision P), optionally at
price M, with certainty S". Many observations per entity are expected and wanted:
theatrical vs digital vs physical vs per-store, per-country, from different
sources with different confidence. A :class:`BestEstimate` is a *derived* pick
over those observations.
"""

from __future__ import annotations

import enum
import hashlib
from datetime import date, datetime
from typing import Self

from pydantic import BaseModel, Field, model_validator


class MediaKind(enum.StrEnum):
    """The kind of thing being tracked. Maps from the Notion ``Type`` select."""

    MOVIE = "movie"
    TV = "tv"
    GAME = "game"
    TECH = "tech"
    BOOK = "book"
    MUSIC = "music"
    PODCAST = "podcast"
    COMIC = "comic"
    ANIME = "anime"
    OTHER = "other"


class ReleaseChannel(enum.StrEnum):
    """How/where a release happens — the "location" axis (a format or a store).

    Spans every media kind so a single column can describe "theatrical in DE",
    "digital VOD in US", "on Steam", "at Best Buy", etc. ``region`` carries the
    geography separately.
    """

    # generic / movies & TV
    PREMIERE = "premiere"
    THEATRICAL_LIMITED = "theatrical_limited"
    THEATRICAL = "theatrical"
    DIGITAL = "digital"  # VOD rent/buy, virtual cinema (TMDB type 4)
    STREAMING = "streaming"  # subscription availability (watch providers)
    PHYSICAL = "physical"  # disc / 4K / vinyl
    TV_BROADCAST = "tv_broadcast"

    # games — storefronts
    STEAM = "steam"
    EPIC = "epic"
    GOG = "gog"
    PSN = "psn"
    XBOX = "xbox"
    NINTENDO_ESHOP = "nintendo_eshop"
    APP_STORE = "app_store"
    GOOGLE_PLAY = "google_play"

    # tech / retail
    RETAIL = "retail"  # generic brick-and-mortar / online retailer
    AMAZON = "amazon"
    BEST_BUY = "best_buy"
    MANUFACTURER_DIRECT = "manufacturer_direct"
    PREORDER = "preorder"

    # fallback when the source does not distinguish a channel
    PRIMARY = "primary"


class DatePrecision(enum.StrEnum):
    """How concrete a date is. Maps from IGDB date category & Notion RD Certainty."""

    EXACT = "exact"  # YYYY-MM-DD
    MONTH = "month"  # YYYY-MM
    QUARTER = "quarter"  # YYYY-Qn
    YEAR = "year"  # YYYY
    TBA = "tba"  # no usable date


class Certainty(enum.StrEnum):
    """Epistemic stance of a claim — the basis for confidence scoring."""

    CONFIRMED = "confirmed"  # officially announced by a first party
    RUMORED = "rumored"  # reported as rumor / insider chatter
    LEAKED = "leaked"  # unofficial leak
    ESTIMATED = "estimated"  # press/aggregator best-guess
    PREDICTED = "predicted"  # produced by our own model (e.g. theatrical->digital gap)
    DELAYED = "delayed"  # a superseding claim that pushes a date back


class SourceTier(enum.IntEnum):
    """Trust ranking of a source — higher wins ties and weights confidence."""

    MODEL = 0  # our own prediction
    RUMOR = 1  # rumor blogs, forums
    AGGREGATOR = 2  # generic aggregators
    TRADE_PRESS = 3  # Variety, THR, IGN, GSMArena...
    FIRST_PARTY_STORE = 4  # Steam, PSN store, retailer listing
    OFFICIAL = 5  # studio / publisher / manufacturer PR


class Money(BaseModel):
    """A price in minor units to avoid float rounding (e.g. 5999 == $59.99)."""

    amount_minor: int
    currency: str = Field(min_length=3, max_length=3, description="ISO 4217, e.g. USD")

    @property
    def amount(self) -> float:
        return self.amount_minor / 100

    def __str__(self) -> str:
        return f"{self.amount:.2f} {self.currency}"


class Entity(BaseModel):
    """A tracked title. Stable ``id`` is a slug derived from title+kind."""

    id: str
    title: str
    kind: MediaKind
    aliases: tuple[str, ...] = ()
    # external IDs discovered during resolution: {"tmdb": "12345", "igdb": "...",
    # "steam_appid": "...", "igdb_slug": "..."}
    external_ids: dict[str, str] = Field(default_factory=dict)
    notion_page_id: str | None = None
    notes: str | None = None
    # whether the pipeline should actively re-resolve this entity
    watch: bool = True

    @staticmethod
    def make_id(title: str, kind: MediaKind) -> str:
        norm = "-".join(title.lower().split())
        safe = "".join(c for c in norm if c.isalnum() or c == "-").strip("-")
        digest = hashlib.sha1(f"{kind}:{title}".encode()).hexdigest()[:6]  # noqa: S324
        return f"{kind.value}-{safe[:48]}-{digest}"

    @classmethod
    def create(cls, title: str, kind: MediaKind, **kw: object) -> Self:
        return cls(id=cls.make_id(title, kind), title=title, kind=kind, **kw)  # type: ignore[arg-type]


class ReleaseObservation(BaseModel):
    """One sourced claim about a release. Immutable once written; dedup by ``id``."""

    entity_id: str
    channel: ReleaseChannel
    region: str = "WW"  # ISO 3166-1 alpha-2, or "WW" for worldwide/unknown
    release_date: date | None = None
    date_end: date | None = None  # for windows/ranges; NULL for single dates
    precision: DatePrecision = DatePrecision.TBA
    price: Money | None = None
    certainty: Certainty = Certainty.ESTIMATED
    source_tier: SourceTier = SourceTier.AGGREGATOR
    provider: str = "unknown"  # which puller produced it: tmdb/igdb/steam/openai/model
    source_name: str | None = None
    source_url: str | None = None
    source_quote: str | None = None  # the exact sentence backing the claim
    published_at: date | None = None  # when the source said it
    confidence: float = 0.5  # 0..1, computed by the scorer
    fetched_at: datetime | None = None

    @model_validator(mode="after")
    def _coherence(self) -> Self:
        if self.date_end is not None and self.release_date is None:
            msg = "date_end set without release_date"
            raise ValueError(msg)
        if self.release_date is None and self.precision is not DatePrecision.TBA:
            self.precision = DatePrecision.TBA
        return self

    @property
    def id(self) -> str:
        """Deterministic dedup key: same claim from same source collapses to one row."""
        parts = (
            self.entity_id,
            self.channel.value,
            self.region,
            self.release_date.isoformat() if self.release_date else "",
            self.provider,
            self.source_url or "",
        )
        return hashlib.sha1("|".join(parts).encode()).hexdigest()  # noqa: S324


class BestEstimate(BaseModel):
    """Derived: the chosen date for an (entity, channel, region) and why."""

    entity_id: str
    channel: ReleaseChannel
    region: str
    release_date: date | None
    precision: DatePrecision
    certainty: Certainty
    price: Money | None
    confidence: float
    supporting_observation_ids: tuple[str, ...]
