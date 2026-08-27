"""Hand-authored corrections to a tracked work — the write half of the `/rd-edit` path.

Lifted out of ``cli.py`` so it is callable from *any* frontend, on the same terms as
``capture.py``: every function takes the :class:`Database` it should use rather than
opening its own, returns a value instead of printing one, and raises instead of exiting.
The TUI's card editor and ``rdt edit …`` therefore run the *same* code, which is the only
way the two can be guaranteed to answer alike.

**What "manual" means here.** A hand-authored fact is marked by its provenance, not by a
confidence number: observations get ``provider="manual"``, edges ``source_provider="user"``
with ``owned=True``. Confidence is deliberately not a knob — :func:`resolve.rescore`
recomputes it from certainty and tier on every read, so a number typed in here would be
overwritten before anything could display it. What the author *can* say about how firm a
date is, they say in the EDTF literal itself (``2026-09~`` = "September, probably").
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date

from release_tracker.clock import utc_now
from release_tracker.dates_edtf import parse_edtf, to_edtf
from release_tracker.db import Database
from release_tracker.models import (
    ORG_ROLES,
    Certainty,
    CreditRole,
    DatePrecision,
    DescriptorKind,
    Edge,
    Entity,
    Node,
    NodeKind,
    RelationKind,
    ReleaseChannel,
    ReleaseObservation,
    SourceTier,
)

__all__ = [
    "MANUAL_PROVIDER",
    "MANUAL_REGION",
    "MANUAL_SOURCE",
    "USER_PROVIDER",
    "DateEdit",
    "add_credit",
    "add_platform",
    "add_tag",
    "clear_date",
    "manual_dates",
    "remove_credits",
    "remove_platforms",
    "remove_tags",
    "rename",
    "set_date",
]

MANUAL_PROVIDER = "manual"  # observation provenance: a person typed this
MANUAL_SOURCE = "Manual (EDTF)"
MANUAL_REGION = "WW"  # hand-authored dates are worldwide; per-region is a puller's job
USER_PROVIDER = "user"  # edge provenance: a person asserted this


@dataclass(frozen=True, slots=True)
class DateEdit:
    """What :func:`set_date` wrote — the canonical literal plus the stance it decodes to."""

    channel: ReleaseChannel
    edtf: str
    when: date | None
    precision: DatePrecision
    certainty: Certainty
    end: date | None = None


# --- the work itself ----------------------------------------------------------------------
def rename(db: Database, entity: Entity, title: str) -> Entity:
    """Retitle a work, keeping the old title as a search alias.

    The stable id never changes — only the display title — so every observation, edge and
    note stays attached, and the work is still findable under what it used to be called.
    """
    old = entity.title
    aliases = entity.aliases if old in entity.aliases else (*entity.aliases, old)
    renamed = entity.model_copy(update={"title": title, "aliases": aliases})
    db.upsert_entity(renamed)
    if (node := db.get_node(entity.id)) is not None:  # keep the WORK node label in sync
        db.upsert_node(node.model_copy(update={"name": title}))
    return renamed


# --- dates --------------------------------------------------------------------------------
def set_date(db: Database, entity: Entity, channel: ReleaseChannel, edtf: str) -> DateEdit:
    """Hand-author a release date on one channel. Raises ``ValueError`` on a bad literal.

    EDTF folds precision and uncertainty into one token, so "we only know the month, and
    it's a guess" is just ``2026-09~`` and a window is ``2027/2029``. Certainty comes from
    the qualifier rather than a flag, and the tier follows it: an unqualified date is
    someone stating a fact (OFFICIAL), a qualified one is someone hedging (RUMOR).

    The prior manual row is deleted rather than updated: an observation's id is a content
    hash over its date, so a new date mints a new row — and the upsert refreshes neither
    ``release_date`` nor ``date_end``, which would make re-authoring only the end of a
    window a silent no-op.
    """
    parsed = parse_edtf(edtf)
    confirmed = parsed.certainty is Certainty.CONFIRMED
    canonical = to_edtf(
        parsed.when,
        parsed.precision,
        parsed.certainty,
        end=parsed.end,
        end_precision=parsed.end_precision,
    )
    db.delete_channel_observations(entity.id, MANUAL_PROVIDER, channel)
    db.upsert_observation(
        ReleaseObservation(
            entity_id=entity.id,
            channel=channel,
            region=MANUAL_REGION,
            release_date=parsed.when,
            date_end=parsed.end,  # set for an EDTF interval (a release window), else None
            precision=parsed.precision,
            certainty=parsed.certainty,
            source_tier=SourceTier.OFFICIAL if confirmed else SourceTier.RUMOR,
            provider=MANUAL_PROVIDER,
            source_name=MANUAL_SOURCE,
            source_quote=canonical,
            confidence=1.0 if confirmed else 0.5,
            fetched_at=utc_now(),
        )
    )
    return DateEdit(
        channel=channel,
        edtf=canonical,
        when=parsed.when,
        precision=parsed.precision,
        certainty=parsed.certainty,
        end=parsed.end,
    )


def clear_date(db: Database, entity: Entity, channel: ReleaseChannel) -> int:
    """Drop the hand-authored date on a channel, letting a pulled one surface again."""
    return db.clear_observations(entity.id, channel, provider=MANUAL_PROVIDER)


def manual_dates(db: Database, entity_id: str) -> dict[ReleaseChannel, str]:
    """Channel -> the canonical EDTF literal a person authored for it.

    Rebuilt from the stored fields rather than read back from ``source_quote``, so it
    cannot drift from what the observation actually says.
    """
    return {
        obs.channel: to_edtf(obs.release_date, obs.precision, obs.certainty, end=obs.date_end)
        for obs in db.iter_observations(entity_id)
        if obs.provider == MANUAL_PROVIDER
    }


# --- who / where / what -------------------------------------------------------------------
def add_credit(
    db: Database,
    entity: Entity,
    name: str,
    role: CreditRole,
    *,
    org: bool | None = None,
    pin: tuple[str, str] | None = None,
) -> Node:
    """Credit a person or studio on a work (a who-edge).

    Whether the credit is a company follows from the role unless ``org`` says otherwise:
    a studio, network, developer or publisher is one, and getting that wrong forks a
    second node of the wrong kind beside the one the pullers already resolved.

    ``pin`` is a ``(source, source_id)`` key: without it a credit-by-name makes a name-slug
    node (``person:denis-villeneuve``), with it the canonical one (``person:tmdb:287``), so
    the credit collapses onto the same node a resolved credit from that source maps to.
    """
    kind = NodeKind.ORG if (role in ORG_ROLES if org is None else org) else NodeKind.PERSON
    node = (
        Node.create(kind, name, owned=True)
        if pin is None
        else Node.create(kind, name, source=pin[0], source_id=pin[1], owned=True)
    )
    db.upsert_node(node)
    db.upsert_edge(
        Edge(
            src_id=node.id,
            dst_id=entity.id,
            relation=RelationKind.CREDITED_ON,
            role=role,
            source_provider=USER_PROVIDER,
            source_tier=SourceTier.OFFICIAL,
            confidence=1.0,
            owned=True,
        )
    )
    return node


def add_tag(
    db: Database, entity: Entity, name: str, kind: DescriptorKind = DescriptorKind.GENRE
) -> Node:
    """Tag a work with a genre, theme, mood, style or origin (a what-edge)."""
    node = Node.create(NodeKind.DESCRIPTOR, name, descriptor_kind=kind, owned=True)
    db.upsert_node(node)
    db.upsert_edge(
        Edge(
            src_id=entity.id,
            dst_id=node.id,
            relation=RelationKind.EXHIBITS,
            source_provider=USER_PROVIDER,
            source_tier=SourceTier.OFFICIAL,
            confidence=1.0,
            owned=True,
        )
    )
    return node


def add_platform(db: Database, entity: Entity, name: str, *, predicted: bool = False) -> Node:
    """Record a platform a work is (or will likely be) available on (a where-edge)."""
    node = Node.create(NodeKind.PLATFORM, name, owned=not predicted)
    db.upsert_node(node)
    db.upsert_edge(
        Edge(
            src_id=entity.id,
            dst_id=node.id,
            relation=RelationKind.AVAILABLE_ON,
            # predicted -> model-tier (renders as "~name"); hand-stated -> user-authoritative
            source_provider="model" if predicted else USER_PROVIDER,
            source_tier=SourceTier.MODEL if predicted else SourceTier.OFFICIAL,
            confidence=0.4 if predicted else 1.0,
            owned=not predicted,
        )
    )
    return node


def _unlink(
    db: Database,
    entity: Entity,
    node_ids: Collection[str],
    relation: RelationKind,
    *,
    inbound: bool,
) -> int:
    """Delete the edges of one relation between a work and the given nodes.

    Takes ids rather than a name so the caller decides how loose the match is: a picker
    that already knows which row it is on passes one id, while ``rdt edit uncredit`` first
    widens a name to every node that could be meant.
    """
    edges = db.edges_to(entity.id, relation) if inbound else db.edges_from(entity.id, relation)
    wanted = set(node_ids)
    return sum(db.delete_edge(e.id) for e in edges if (e.src_id if inbound else e.dst_id) in wanted)


def remove_credits(db: Database, entity: Entity, node_ids: Collection[str]) -> int:
    return _unlink(db, entity, node_ids, RelationKind.CREDITED_ON, inbound=True)


def remove_tags(db: Database, entity: Entity, node_ids: Collection[str]) -> int:
    return _unlink(db, entity, node_ids, RelationKind.EXHIBITS, inbound=False)


def remove_platforms(db: Database, entity: Entity, node_ids: Collection[str]) -> int:
    return _unlink(db, entity, node_ids, RelationKind.AVAILABLE_ON, inbound=False)
