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
    OTHER = "other"
    # NOTE: "anime" is deliberately NOT a kind — it's a medium/origin, orthogonal to
    # format. An anime film is kind=MOVIE, an anime series is kind=TV; the anime-ness
    # is a DescriptorKind.ORIGIN tag (see enrich's JP-animation detection).


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


# The commercial-release tiers whose date starts the home-video (PVOD/digital) clock. A festival
# PREMIERE is deliberately excluded: it precedes the commercial run (often by months), so it must
# never anchor a theatrical->digital estimate — it only *chains* into one. Wide before limited.
COMMERCIAL_THEATRICAL: tuple[ReleaseChannel, ...] = (
    ReleaseChannel.THEATRICAL,
    ReleaseChannel.THEATRICAL_LIMITED,
)


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
    DROPPED = "dropped"  # started, then abandoned
    SKIPPED = "skipped"  # consciously passed on (not for me) — preserved, not deleted


class Bucket(enum.StrEnum):
    """Which of the three consumption surfaces a tracked work lands on.

    Exhaustive and disjoint: every work is in exactly one, so there is no limbo. Derived
    from ``ConsumptionState`` + the resolved availability date (see ``views.bucket_of``),
    which is the single classifier both the CLI views and the query language use.
    """

    AVAILABLE = "available"  # out, confirmed, and you haven't finished it
    UPCOMING = "upcoming"  # not out yet (or out but not available to you), incl. the TBA tail
    WATCHED = "watched"  # finished with: watched / dropped / skipped


class SocialPlatform(enum.StrEnum):
    """A creator's content/social platform, for the artist-radar."""

    # video / streaming
    YOUTUBE = "youtube"
    TWITCH = "twitch"
    TIKTOK = "tiktok"
    NEBULA = "nebula"
    VIMEO = "vimeo"
    # social
    BLUESKY = "bluesky"
    TWITTER = "twitter"
    REDDIT = "reddit"
    INSTAGRAM = "instagram"
    THREADS = "threads"
    TUMBLR = "tumblr"
    MASTODON = "mastodon"
    # art
    ARTSTATION = "artstation"
    DEVIANTART = "deviantart"
    PIXIV = "pixiv"
    CARA = "cara"
    NEWGROUNDS = "newgrounds"
    # music
    SPOTIFY = "spotify"
    BANDCAMP = "bandcamp"
    SOUNDCLOUD = "soundcloud"
    # support / paid
    PATREON = "patreon"
    KOFI = "kofi"
    BUYMEACOFFEE = "buymeacoffee"
    GUMROAD = "gumroad"
    SUBSTACK = "substack"
    FANBOX = "fanbox"
    BOOSTY = "boosty"
    ITCH = "itch"
    # body of work (not a feed they post to — their canonical filmography/discography)
    FILMOGRAPHY = "filmography"
    # catch-all
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
    # structured season/part coordinates for a TV-season entry (the explicit `--season`/`--part`
    # path). Authoritative over title parsing: pullers and enrichment prefer these, falling back
    # to `split_season(title)`/`extract_part(title)` only when they're None (back-compat shorthand).
    season: int | None = None
    part: int | None = None

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
    # extra availability facets beyond region/channel — the open, extensible contingency set
    # (e.g. {"platform": "ps5", "os": "linux", "language": "en"}). A facet absent here is a
    # wildcard against the user's profile; region/channel stay native columns.
    contingencies: dict[str, str] = Field(default_factory=dict[str, str])
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
        """Deterministic dedup key: same claim from same source collapses to one row.

        Facet-tagged rows are distinct claims (a PS5 date != a PC date), so contingencies
        fold into the hash — but ONLY when present, so every pre-contingency row keeps its
        existing id byte-for-byte (no re-pull churn on migration).
        """
        parts = [
            self.entity_id,
            self.channel.value,
            self.region,
            self.release_date.isoformat() if self.release_date else "",
            self.provider,
            self.source_url or "",
        ]
        if self.contingencies:
            parts.append(
                ";".join(f"{k}={self.contingencies[k]}" for k in sorted(self.contingencies))
            )
        return hashlib.sha1("|".join(parts).encode()).hexdigest()  # noqa: S324


class BestEstimate(BaseModel):
    """Derived: the chosen date for an (entity, channel, region) and why."""

    entity_id: str
    channel: ReleaseChannel
    region: str
    contingencies: dict[str, str] = Field(default_factory=dict[str, str])  # carried from winner
    release_date: date | None
    date_end: date | None = None  # upper bound for a window/range; NULL for a single date
    precision: DatePrecision = DatePrecision.TBA
    certainty: Certainty = Certainty.ESTIMATED
    price: Money | None = None
    confidence: float = 0.5
    provider: str = "unknown"  # who produced the winning observation (tmdb / model / manual…)
    supporting_observation_ids: tuple[str, ...] = ()
    fetched_at: datetime | None = None  # when the winning observation was last refreshed


class Condition(BaseModel):
    """An external blocker's resolution (the row backing a CONDITION node).

    ``status`` is "resolved" | "pending" | "never" (the three-valued ResolutionStatus). A
    work BLOCKED_BY this condition is unavailable until it resolves; a ``never`` makes the
    work permanently unavailable to anyone depending on it.
    """

    node_id: str
    status: str
    resolve_date: date | None = None
    precision: DatePrecision = DatePrecision.TBA
    note: str | None = None


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
    CONDITION = (
        "condition"  # an external blocker (e.g. "EAC Linux support"); resolution in `conditions`
    )


class DescriptorKind(enum.StrEnum):
    """Sub-type of a DESCRIPTOR node.

    GENRE is high-trust (sourced from TMDB/IGDB); THEME/MOOD/STYLE are soft,
    model-derived, and treated as falsifiable hypotheses the user can confirm or reject.
    """

    GENRE = "genre"
    THEME = "theme"
    MOOD = "mood"
    STYLE = "style"
    ORIGIN = "origin"  # medium/provenance, e.g. "anime" — sourced, high-trust like GENRE


class RelationKind(enum.StrEnum):
    """The edge type in the uniform node->node graph."""

    CREDITED_ON = "credited_on"  # person/org -> work (qualified by CreditRole)
    AVAILABLE_ON = "available_on"  # work -> platform
    EXHIBITS = "exhibits"  # work -> descriptor
    PART_OF_SERIES = "part_of_series"  # work -> series (ordinal=season, part=mid-season cut)
    MEMBER_OF = "member_of"  # person -> org (a member of a group/studio/band)
    DERIVED_FROM = "derived_from"  # work -> work/series (qualified by WorkRelation)
    INFLUENCED_BY = "influenced_by"  # node -> node (reserved for the later walk)
    BLOCKED_BY = "blocked_by"  # work -> condition (availability gated on the condition resolving)


class WorkRelation(enum.StrEnum):
    """The nature of a DERIVED_FROM edge between two works — cross-media lineage.

    Unifies every "how works relate" link under one edge type + a role vocabulary,
    the same way CreditRole unifies the "who" across mediums.
    """

    SPINOFF = "spinoff"  # Arcane: Noxus -> Arcane
    ADAPTATION = "adaptation"  # a film/series -> the book it adapts
    SEQUEL = "sequel"
    PREQUEL = "prequel"
    # Hardware lineage, where "sequel" reads wrong: a Steam Deck 2 does not continue a
    # story, it replaces a product. No PREDECESSOR counterpart — that is this same edge
    # read backwards, which `derivatives_of` already gives you, unlike PREQUEL which is a
    # genuinely different relation rather than a direction.
    SUCCESSOR = "successor"
    REMAKE = "remake"
    REMASTER = "remaster"
    TIE_IN = "tie_in"  # an art book / soundtrack / companion -> the work
    SAME_UNIVERSE = "same_universe"
    BASED_ON = "based_on"  # looser "inspired by / based on"


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


# Roles a company holds, not a person. The pullers already resolve these to ORG nodes
# (tmdb production companies and networks, igdb developers and publishers), so a
# hand-added credit in one of them has to be an ORG too or it forks a second node of a
# different kind for the same company.
ORG_ROLES: frozenset[CreditRole] = frozenset(
    {
        CreditRole.DEVELOPER,
        CreditRole.PUBLISHER,
        CreditRole.STUDIO,
        CreditRole.ANIMATION_STUDIO,
        CreditRole.NETWORK,
    }
)


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
    # CreditRole for CREDITED_ON; WorkRelation for DERIVED_FROM; else None
    role: CreditRole | WorkRelation | None = None
    ordinal: int | None = None  # the season number for PART_OF_SERIES, if any
    part: int | None = None  # the mid-season cut (Part/Volume/Cour N) within a season
    # ISO-2 the relation holds in, for AVAILABLE_ON. None means unscoped, *not* worldwide:
    # a broadcast network genuinely has no market, and claiming "WW" for an unknown scope is
    # exactly the everywhere-lie this field exists to remove.
    region: str | None = None
    source_provider: str = "unknown"  # tmdb / igdb / steam / openai / user
    source_url: str | None = None
    source_tier: SourceTier = SourceTier.AGGREGATOR
    confidence: float = 0.7
    owned: bool = False  # the user asserted this edge (vs resolved from the world)
    fetched_at: datetime | None = None

    @property
    def id(self) -> str:
        """Deterministic dedup key: same relation from same source collapses to one row.

        Region joins the key for ``AVAILABLE_ON`` only, where "on Netflix in the US" and "on
        Netflix in Japan" are two facts that must not collapse. Every other relation hashes
        exactly as it did — folding ``region or ""`` in unconditionally would move the id of
        every credit, genre and series edge already in the db and duplicate the lot on the
        next enrich.
        """
        parts = (
            self.src_id,
            self.dst_id,
            self.relation.value,
            self.role.value if self.role else "",
            self.source_provider,
        )
        if self.relation is RelationKind.AVAILABLE_ON and self.region:
            parts = (*parts, self.region)
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
