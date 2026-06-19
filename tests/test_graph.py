"""Tests for the media graph: node/edge identity, idempotency, and one-hop walks."""

from __future__ import annotations

from pathlib import Path

from release_tracker.db import Database
from release_tracker.models import (
    CreditRole,
    DescriptorKind,
    Edge,
    Entity,
    MediaKind,
    Node,
    NodeKind,
    RelationKind,
    SourceTier,
)


def _db(tmp_path: Path) -> Database:
    return Database(tmp_path / "g.db")


def test_node_id_is_canonical_and_collapses() -> None:
    # same person from the same source -> one id, regardless of how it's built
    a = Node.create(NodeKind.PERSON, "Denis Villeneuve", source="tmdb", source_id="137427")
    b = Node.create(NodeKind.PERSON, "denis villeneuve!!", source="tmdb", source_id="137427")
    assert a.id == b.id == "person:tmdb:137427"
    # descriptors are namespaced by descriptor kind so genre/theme don't collide
    g = Node.create(NodeKind.DESCRIPTOR, "Action", descriptor_kind=DescriptorKind.GENRE)
    t = Node.create(NodeKind.DESCRIPTOR, "Action", descriptor_kind=DescriptorKind.THEME)
    assert g.id == "descriptor:genre:action"
    assert t.id == "descriptor:theme:action"
    assert g.id != t.id
    # name-slug fallback when no source id
    assert Node.create(NodeKind.PLATFORM, "Netflix").id == "platform:netflix"


def test_edge_id_dedups_by_relation_and_source() -> None:
    e1 = Edge(
        src_id="person:tmdb:1",
        dst_id="movie-x",
        relation=RelationKind.CREDITED_ON,
        role=CreditRole.DIRECTOR,
        source_provider="tmdb",
    )
    e2 = Edge(  # same claim, different confidence -> same id
        src_id="person:tmdb:1",
        dst_id="movie-x",
        relation=RelationKind.CREDITED_ON,
        role=CreditRole.DIRECTOR,
        source_provider="tmdb",
        confidence=0.99,
    )
    e3 = Edge(  # different role -> different id
        src_id="person:tmdb:1",
        dst_id="movie-x",
        relation=RelationKind.CREDITED_ON,
        role=CreditRole.WRITER,
        source_provider="tmdb",
    )
    assert e1.id == e2.id
    assert e1.id != e3.id


def test_node_upsert_is_idempotent_and_owned_is_sticky(tmp_path: Path) -> None:
    db = _db(tmp_path)
    person = Node.create(NodeKind.PERSON, "Hideo Kojima", source="igdb", source_id="5")
    db.upsert_node(person)  # world-resolved (owned=False)
    db.upsert_node(person.model_copy(update={"owned": True}))  # user later claims it
    # re-resolve from the world must NOT un-own it
    db.upsert_node(person)
    stored = db.get_node(person.id)
    assert stored is not None
    assert stored.owned is True


def test_one_hop_works_for_person(tmp_path: Path) -> None:
    db = _db(tmp_path)
    person = Node.create(NodeKind.PERSON, "Denis Villeneuve", source="tmdb", source_id="137427")
    db.upsert_node(person)
    for title in ("Dune", "Arrival"):
        ent = Entity.create(title, MediaKind.MOVIE)
        db.upsert_entity(ent)
        db.upsert_node(Node(id=ent.id, node_kind=NodeKind.WORK, name=title, owned=True))
        db.upsert_edge(
            Edge(
                src_id=person.id,
                dst_id=ent.id,
                relation=RelationKind.CREDITED_ON,
                role=CreditRole.DIRECTOR,
                source_provider="tmdb",
                source_tier=SourceTier.AGGREGATOR,
            )
        )
    works = db.edges_from(person.id, RelationKind.CREDITED_ON)
    assert {e.dst_id for e in works} == {
        Entity.make_id("Dune", MediaKind.MOVIE),
        Entity.make_id("Arrival", MediaKind.MOVIE),
    }
    # and the reverse direction: who is credited on Dune
    dune_id = Entity.make_id("Dune", MediaKind.MOVIE)
    creditors = db.edges_to(dune_id, RelationKind.CREDITED_ON)
    assert [e.src_id for e in creditors] == [person.id]


def test_find_nodes_by_name_and_kind(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.upsert_node(Node.create(NodeKind.PERSON, "Trent Reznor", source="tmdb", source_id="9"))
    db.upsert_node(Node.create(NodeKind.ORG, "Reznor Studios"))
    people = db.find_nodes("reznor", node_kind=NodeKind.PERSON)
    assert [n.name for n in people] == ["Trent Reznor"]
    assert len(db.find_nodes("reznor")) == 2


def test_existing_entity_ids_unchanged_after_slug_refactor() -> None:
    # the make_id refactor must not change historical entity ids
    eid = Entity.make_id("Dune: Part Two", MediaKind.MOVIE)
    assert eid.startswith("movie-dune-part-two-")
    assert len(eid.split("-")[-1]) == 6  # 6-char digest suffix preserved
