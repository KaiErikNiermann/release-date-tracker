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


def slugify(text: str) -> str:
    """Lowercase hyphen-slug: keep alphanumerics and hyphens, collapse the rest."""
    norm = "-".join(text.lower().split())
    return "".join(c for c in norm if c.isalnum() or c == "-").strip("-")


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


class ConsumptionState(enum.StrEnum):
    """The user's watch/play progress for a title (seeded from Notion's Status).

    Orthogonal to availability (whether it's released — that's derived from dates)
    and to ``Entity.watch`` (whether the pipeline should re-resolve it).
    """

    UNSET = "unset"
    WANT = "want"  # to watch / want to watch/play
    WATCHING = "watching"  # currently watching/playing
    WATCHED = "watched"  # watched / played (done)
    DROPPED = "dropped"  # abandoned


class SocialPlatform(enum.StrEnum):
    """A creator's content/social platform, for the artist-radar."""

    YOUTUBE = "youtube"
    BLUESKY = "bluesky"
    REDDIT = "reddit"
    TWITTER = "twitter"
    TWITCH = "twitch"
    PATREON = "patreon"
    BUYMEACOFFEE = "buymeacoffee"
    DEVIANTART = "deviantart"
    ARTSTATION = "artstation"
    PIXIV = "pixiv"
    SPOTIFY = "spotify"
    BANDCAMP = "bandcamp"
    SOUNDCLOUD = "soundcloud"
    INSTAGRAM = "instagram"
    TUMBLR = "tumblr"
    MASTODON = "mastodon"
    WEBSITE = "website"
    OTHER = "other"


class LinkTier(enum.StrEnum):
    """How a creator's link is consumed — the radar stratifies on this."""

    FREE = "free"  # primary free content pipeline
    PAID = "paid"  # primary paid pipeline (Patreon, BuyMeACoffee, ...)
    AUXILIARY = "auxiliary"  # secondary socials (announcements/chatter, not primary output)


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
    # the user's watch/play progress (seeded from Notion Status, set via `rdt state`)
    consumption_state: ConsumptionState = ConsumptionState.UNSET

    @staticmethod
    def make_id(title: str, kind: MediaKind) -> str:
        digest = hashlib.sha1(f"{kind}:{title}".encode()).hexdigest()[:6]  # noqa: S324
        return f"{kind.value}-{slugify(title)[:48]}-{digest}"

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
    fetched_at: datetime | None = None  # when the winning observation was last refreshed


# --- the media graph (who / where / what) ---------------------------------
# A typed, provenance-tracked graph layered on top of entities/observations.
# Works keep their detail in `entities`; every other participant is a Node. Every
# edge carries its source so a claim is auditable, and an `owned` flag separates
# what the user authored from what was resolved from the world.


class NodeKind(enum.StrEnum):
    """The kind of a graph node."""

    WORK = "work"  # mirrors an Entity (shares its id); detail lives in `entities`
    PERSON = "person"  # a credited human (director, developer, composer, ...)
    ORG = "org"  # studio / publisher / developer house / label / network
    PLATFORM = "platform"  # streaming service or store you consume it on
    SERIES = "series"  # franchise / collection a work belongs to
    DESCRIPTOR = "descriptor"  # genre / theme / mood / style (see DescriptorKind)


class DescriptorKind(enum.StrEnum):
    """Sub-type of a DESCRIPTOR node.

    GENRE is high-trust (sourced from TMDB/IGDB); THEME/MOOD/STYLE are soft,
    model-derived, and treated as falsifiable hypotheses the user can confirm or reject.
    """

    GENRE = "genre"
    THEME = "theme"
    MOOD = "mood"
    STYLE = "style"


class RelationKind(enum.StrEnum):
    """The edge type in the uniform node->node graph."""

    CREDITED_ON = "credited_on"  # person/org -> work (qualified by CreditRole)
    AVAILABLE_ON = "available_on"  # work -> platform
    EXHIBITS = "exhibits"  # work -> descriptor
    PART_OF_SERIES = "part_of_series"  # work -> series
    MEMBER_OF = "member_of"  # person -> org (a member of a group/studio/band)
    INFLUENCED_BY = "influenced_by"  # node -> node (reserved for the later walk)


class CreditRole(enum.StrEnum):
    """The credited role on a CREDITED_ON edge.

    Spans every medium's authorship structure so one PERSON/ORG node type unifies the
    "who" across kinds (the artist-divergence problem) — the role carries the medium.
    """

    DIRECTOR = "director"
    WRITER = "writer"
    CREATOR = "creator"
    SHOWRUNNER = "showrunner"
    DEVELOPER = "developer"
    PUBLISHER = "publisher"
    STUDIO = "studio"
    ANIMATION_STUDIO = "animation_studio"
    NETWORK = "network"
    COMPOSER = "composer"
    ARTIST = "artist"
    AUTHOR = "author"
    HOST = "host"
    CAST = "cast"
    VOICE = "voice"
    OTHER = "other"


class Node(BaseModel):
    """A participant in the media graph. ``owned`` marks user-authored vs world/resolved."""

    id: str
    node_kind: NodeKind
    name: str
    descriptor_kind: DescriptorKind | None = None  # set iff node_kind is DESCRIPTOR
    owned: bool = False
    followed: bool = False  # on the artist-radar (a creator the user actively tracks)
    external_ids: dict[str, str] = Field(default_factory=dict)

    @staticmethod
    def make_id(
        node_kind: NodeKind,
        name: str,
        *,
        source: str | None = None,
        source_id: str | None = None,
        descriptor_kind: DescriptorKind | None = None,
    ) -> str:
        """Canonical id so the same person/genre/platform collapses across works.

        Prefers a stable source key (``person:tmdb:287``) so a creative is one node
        across their whole filmography; falls back to a name slug, namespaced by
        descriptor kind for descriptors (``descriptor:genre:action``).
        """
        if source and source_id:
            return f"{node_kind.value}:{source}:{source_id}"
        if descriptor_kind is not None:
            return f"{node_kind.value}:{descriptor_kind.value}:{slugify(name)}"
        return f"{node_kind.value}:{slugify(name)}"

    @classmethod
    def create(cls, node_kind: NodeKind, name: str, **kw: object) -> Self:
        descriptor_kind = kw.get("descriptor_kind")
        node_id = cls.make_id(
            node_kind,
            name,
            source=kw.get("source"),  # type: ignore[arg-type]
            source_id=kw.get("source_id"),  # type: ignore[arg-type]
            descriptor_kind=descriptor_kind
            if isinstance(descriptor_kind, DescriptorKind)
            else None,
        )
        kw.pop("source", None)
        kw.pop("source_id", None)
        return cls(id=node_id, node_kind=node_kind, name=name, **kw)  # type: ignore[arg-type]


class Edge(BaseModel):
    """One provenance-tracked relation between two nodes. Dedup by ``id``."""

    src_id: str
    dst_id: str
    relation: RelationKind
    role: CreditRole | None = None  # set for CREDITED_ON
    ordinal: int | None = None  # the season/part number for PART_OF_SERIES, if any
    source_provider: str = "unknown"  # tmdb / igdb / steam / openai / user
    source_url: str | None = None
    source_tier: SourceTier = SourceTier.AGGREGATOR
    confidence: float = 0.7
    owned: bool = False  # the user asserted this edge (vs resolved from the world)
    fetched_at: datetime | None = None

    @property
    def id(self) -> str:
        """Deterministic dedup key: same relation from same source collapses to one row."""
        parts = (
            self.src_id,
            self.dst_id,
            self.relation.value,
            self.role.value if self.role else "",
            self.source_provider,
        )
        return hashlib.sha1("|".join(parts).encode()).hexdigest()  # noqa: S324


class ArtistLink(BaseModel):
    """A followed creator's link on one platform, with its latest surfaced content.

    One row per (artist node, platform). ``latest_*``/``last_post_at`` are filled by a
    fetcher where one exists (YouTube/Bluesky/Reddit); otherwise the link is just
    documented (stratified by :class:`LinkTier`).
    """

    node_id: str
    platform: SocialPlatform
    tier: LinkTier
    url: str
    handle: str | None = None
    latest_title: str | None = None
    latest_url: str | None = None
    last_post_at: date | None = None
    fetched_at: datetime | None = None
