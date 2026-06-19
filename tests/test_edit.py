"""Tests for the freeform edit surface (`rdt edit …`).

Drives the real Typer commands end-to-end against a temp db (the deterministic
backing both /rd-edit and a future web edit form call), asserting each small
mutation lands the right entity/node/edge state.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from release_tracker import cli
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
    WorkRelation,
)

runner = CliRunner()


@pytest.fixture
def edit_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every `cli._db()` at one tmp file, seeded with a single work."""
    path = tmp_path / "e.db"
    db = Database(path)
    ent = Entity.create("Untitled Project", MediaKind.MOVIE)
    db.upsert_entity(ent)
    db.upsert_node(Node(id=ent.id, node_kind=NodeKind.WORK, name=ent.title, owned=True))
    db.close()
    monkeypatch.setattr(cli, "_db", lambda: Database(path))
    return path


def test_rename_keeps_id_and_aliases_old_title(edit_db: Path) -> None:
    before = Database(edit_db)
    ent = before.iter_entities().__next__()
    before.close()
    res = runner.invoke(cli.app, ["edit", "rename", ent.id, "The Announced Name"])
    assert res.exit_code == 0
    db = Database(edit_db)
    renamed = db.get_entity(ent.id)
    node = db.get_node(ent.id)
    db.close()
    assert renamed is not None and renamed.title == "The Announced Name"
    assert renamed.id == ent.id  # the stable handle never moves
    assert "Untitled Project" in renamed.aliases
    assert node is not None and node.name == "The Announced Name"  # node label synced


def test_credit_then_uncredit_round_trips(edit_db: Path) -> None:
    db0 = Database(edit_db)
    ent = next(db0.iter_entities())
    db0.close()
    runner.invoke(cli.app, ["edit", "credit", ent.id, "Denis Villeneuve", "director"])
    runner.invoke(cli.app, ["edit", "credit", ent.id, "Legendary", "studio", "--org"])
    db = Database(edit_db)
    credits = db.edges_to(ent.id, RelationKind.CREDITED_ON)
    by_src = {e.src_id: e for e in credits}
    db.close()
    person = Node.create(NodeKind.PERSON, "Denis Villeneuve", owned=True)
    org = Node.create(NodeKind.ORG, "Legendary", owned=True)
    assert by_src[person.id].role is CreditRole.DIRECTOR
    assert by_src[person.id].owned is True
    assert by_src[org.id].role is CreditRole.STUDIO

    res = runner.invoke(cli.app, ["edit", "uncredit", ent.id, "Legendary"])
    assert res.exit_code == 0
    db = Database(edit_db)
    remaining = {e.src_id for e in db.edges_to(ent.id, RelationKind.CREDITED_ON)}
    db.close()
    assert remaining == {person.id}  # only the org credit was dropped


def test_credit_pin_canonicalizes_onto_resolved_node(edit_db: Path) -> None:
    # a world-resolved (unowned) node already exists at the canonical source key
    db0 = Database(edit_db)
    ent = next(db0.iter_entities())
    db0.upsert_node(
        Node.create(NodeKind.PERSON, "Denis Villeneuve", source="tmdb", source_id="287")
    )
    db0.close()
    res = runner.invoke(
        cli.app,
        ["edit", "credit", ent.id, "Denis Villeneuve", "director", "--pin", "tmdb:287"],
    )
    assert res.exit_code == 0
    db = Database(edit_db)
    villeneuve = db.find_nodes("villeneuve")
    (edge,) = db.edges_to(ent.id, RelationKind.CREDITED_ON)
    db.close()
    # collapses onto the one canonical node (not a separate name-slug node)...
    assert [n.id for n in villeneuve] == ["person:tmdb:287"]
    assert villeneuve[0].owned is True  # ...and the hand-credit claims it (MAX(owned))
    assert edge.src_id == "person:tmdb:287"


def test_credit_rejects_malformed_pin(edit_db: Path) -> None:
    db0 = Database(edit_db)
    ent = next(db0.iter_entities())
    db0.close()
    res = runner.invoke(
        cli.app, ["edit", "credit", ent.id, "X", "director", "--pin", "nocolon"]
    )
    assert res.exit_code != 0


def test_credit_rejects_unknown_role(edit_db: Path) -> None:
    db0 = Database(edit_db)
    ent = next(db0.iter_entities())
    db0.close()
    res = runner.invoke(cli.app, ["edit", "credit", ent.id, "Someone", "bestboy"])
    assert res.exit_code != 0


def test_tag_genre_and_theme_then_untag(edit_db: Path) -> None:
    db0 = Database(edit_db)
    ent = next(db0.iter_entities())
    db0.close()
    runner.invoke(cli.app, ["edit", "tag", ent.id, "Sci-Fi"])
    runner.invoke(cli.app, ["edit", "tag", ent.id, "jihad", "--theme"])
    db = Database(edit_db)
    tags = db.edges_from(ent.id, RelationKind.EXHIBITS)
    nodes = db.get_nodes(e.dst_id for e in tags)
    kinds = {nodes[e.dst_id].name: nodes[e.dst_id].descriptor_kind for e in tags}
    db.close()
    assert kinds["Sci-Fi"] is DescriptorKind.GENRE
    assert kinds["jihad"] is DescriptorKind.THEME

    runner.invoke(cli.app, ["edit", "untag", ent.id, "jihad"])
    db = Database(edit_db)
    remaining = {nodes[e.dst_id].name for e in db.edges_from(ent.id, RelationKind.EXHIBITS)}
    db.close()
    assert remaining == {"Sci-Fi"}


def test_part_links_series_then_updates_in_place(edit_db: Path) -> None:
    db0 = Database(edit_db)
    ent = next(db0.iter_entities())
    db0.close()
    # first call creates the series link (a now-known split attaching to a franchise)
    runner.invoke(cli.app, ["edit", "part", ent.id, "5", "--series", "The Saga"])
    db = Database(edit_db)
    (edge,) = db.edges_from(ent.id, RelationKind.PART_OF_SERIES)
    series_id = edge.dst_id
    db.close()
    assert (edge.ordinal, edge.part) == (5, None)

    # second call adds a part within the same season — same edge, updated in place
    runner.invoke(cli.app, ["edit", "part", ent.id, "5", "2"])
    db = Database(edit_db)
    edges = db.edges_from(ent.id, RelationKind.PART_OF_SERIES)
    db.close()
    assert len(edges) == 1  # not forked into a second edge
    assert edges[0].dst_id == series_id
    assert (edges[0].ordinal, edges[0].part) == (5, 2)


def test_part_without_series_link_errors(edit_db: Path) -> None:
    db0 = Database(edit_db)
    ent = next(db0.iter_entities())
    db0.close()
    res = runner.invoke(cli.app, ["edit", "part", ent.id, "2"])
    assert res.exit_code == 1  # no existing series edge and no --series given


def test_delete_edge_primitive(edit_db: Path) -> None:
    # the new db primitive uncredit/untag/un-relate ride on
    db = Database(edit_db)
    ent = next(db.iter_entities())
    edge = Edge(
        src_id=ent.id,
        dst_id="series:x",
        relation=RelationKind.DERIVED_FROM,
        role=WorkRelation.SEQUEL,
        source_provider="user",
        source_tier=SourceTier.OFFICIAL,
    )
    db.upsert_edge(edge)
    assert db.delete_edge(edge.id) is True
    assert db.delete_edge(edge.id) is False  # already gone
    assert db.edges_from(ent.id, RelationKind.DERIVED_FROM) == []
    db.close()
