"""Tests for the freeform edit surface (`rdt edit …`).

Drives the real Typer commands end-to-end against a temp db (the deterministic
backing both /rd-edit and a future web edit form call), asserting each small
mutation lands the right entity/node/edge state.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from release_tracker import cli, edits
from release_tracker.clock import utc_now
from release_tracker.db import Database
from release_tracker.lookup import RdReport
from release_tracker.models import (
    Certainty,
    ConsumptionState,
    CreditRole,
    DatePrecision,
    DescriptorKind,
    Edge,
    Entity,
    MediaKind,
    Node,
    NodeKind,
    RelationKind,
    ReleaseChannel,
    ReleaseObservation,
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


def test_kind_reclassifies_in_place(edit_db: Path) -> None:
    # an anime film mis-captured as the wrong format -> fix the kind, keep the id
    db0 = Database(edit_db)
    ent = next(db0.iter_entities())  # seeded as MOVIE
    db0.close()
    res = runner.invoke(cli.app, ["edit", "kind", ent.id, "tv"])
    assert res.exit_code == 0
    db = Database(edit_db)
    changed = db.get_entity(ent.id)
    db.close()
    assert changed is not None and changed.kind is MediaKind.TV
    assert changed.id == ent.id  # stable handle survives a re-kind


def test_kind_rejects_unknown_and_noops_on_same(edit_db: Path) -> None:
    db0 = Database(edit_db)
    ent = next(db0.iter_entities())
    db0.close()
    assert runner.invoke(cli.app, ["edit", "kind", ent.id, "nonsense"]).exit_code != 0
    same = runner.invoke(cli.app, ["edit", "kind", ent.id, "movie"])  # already movie
    assert same.exit_code == 0 and "already" in same.output


def test_tag_origin_attaches_origin_descriptor(edit_db: Path) -> None:
    db0 = Database(edit_db)
    ent = next(db0.iter_entities())
    db0.close()
    runner.invoke(cli.app, ["edit", "tag", ent.id, "anime", "--origin"])
    db = Database(edit_db)
    (edge,) = db.edges_from(ent.id, RelationKind.EXHIBITS)
    node = db.get_node(edge.dst_id)
    db.close()
    assert node is not None
    assert node.descriptor_kind is DescriptorKind.ORIGIN
    assert node.name == "anime"


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
    res = runner.invoke(cli.app, ["edit", "credit", ent.id, "X", "director", "--pin", "nocolon"])
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


def test_platform_adds_where_edge_with_predicted_tier(edit_db: Path) -> None:
    db0 = Database(edit_db)
    ent = next(db0.iter_entities())
    db0.close()
    runner.invoke(cli.app, ["edit", "platform", ent.id, "Netflix"])  # hand-stated -> confirmed
    runner.invoke(cli.app, ["edit", "platform", ent.id, "Vimeo", "--predicted"])  # likely -> model
    db = Database(edit_db)
    edges = db.edges_from(ent.id, RelationKind.AVAILABLE_ON)
    nodes = db.get_nodes(e.dst_id for e in edges)
    by_name = {nodes[e.dst_id].name: e for e in edges}
    db.close()
    assert by_name["Netflix"].source_tier is SourceTier.OFFICIAL and by_name["Netflix"].owned
    assert by_name["Vimeo"].source_tier is SourceTier.MODEL  # predicted renders as "~Vimeo"
    assert not by_name["Vimeo"].owned


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


def _manual_obs(db: Database, entity_id: str) -> list[ReleaseObservation]:
    return [o for o in db.iter_observations(entity_id) if o.provider == "manual"]


def test_edit_date_writes_a_partial_uncertain_manual_observation(edit_db: Path) -> None:
    db = Database(edit_db)
    ent = next(db.iter_entities())
    db.close()
    res = runner.invoke(cli.app, ["edit", "date", ent.id, "2026-09~", "--channel", "theatrical"])
    assert res.exit_code == 0
    db = Database(edit_db)
    obs = _manual_obs(db, ent.id)
    db.close()
    assert len(obs) == 1
    (o,) = obs
    assert o.channel is ReleaseChannel.THEATRICAL
    assert o.release_date == date(2026, 9, 1)
    assert o.precision is DatePrecision.MONTH
    assert o.certainty is Certainty.ESTIMATED  # '~' widened to estimated


def test_edit_date_replaces_prior_manual_date_on_same_channel(edit_db: Path) -> None:
    db = Database(edit_db)
    ent = next(db.iter_entities())
    db.close()
    runner.invoke(cli.app, ["edit", "date", ent.id, "2026", "--channel", "theatrical"])
    res = runner.invoke(cli.app, ["edit", "date", ent.id, "2027-09-18", "--channel", "theatrical"])
    assert res.exit_code == 0
    db = Database(edit_db)
    obs = _manual_obs(db, ent.id)
    db.close()
    assert len(obs) == 1  # replaced, not duplicated
    assert obs[0].release_date == date(2027, 9, 18)
    assert obs[0].precision is DatePrecision.EXACT
    assert obs[0].certainty is Certainty.CONFIRMED


def test_edit_date_rejects_a_bad_edtf_literal(edit_db: Path) -> None:
    db = Database(edit_db)
    ent = next(db.iter_entities())
    db.close()
    res = runner.invoke(cli.app, ["edit", "date", ent.id, "2026-13"])
    assert res.exit_code != 0
    db = Database(edit_db)
    assert _manual_obs(db, ent.id) == []  # nothing written
    db.close()


def test_edit_contingency_tags_a_facet_observation(edit_db: Path) -> None:
    db0 = Database(edit_db)
    ent = next(db0.iter_entities())
    db0.close()
    res = runner.invoke(
        cli.app, ["edit", "contingency", ent.id, "platform", "ps5", "--date", "2026-03"]
    )
    assert res.exit_code == 0
    db = Database(edit_db)
    obs = [o for o in db.iter_observations(ent.id) if o.provider == "manual"]
    db.close()
    assert len(obs) == 1
    assert obs[0].contingencies == {"platform": "ps5"}
    assert obs[0].release_date == date(2026, 3, 1)


def test_edit_condition_blocked_by_and_unblock(edit_db: Path) -> None:
    db0 = Database(edit_db)
    ent = next(db0.iter_entities())
    db0.close()
    # author a pending blocker + link the work to it
    runner.invoke(cli.app, ["edit", "condition", "EAC Linux", "pending"])
    runner.invoke(cli.app, ["edit", "blocked-by", ent.id, "EAC Linux"])
    db = Database(edit_db)
    cond_id = Node.make_id(NodeKind.CONDITION, "EAC Linux")
    edges = db.edges_from(ent.id, RelationKind.BLOCKED_BY)
    cond = db.get_conditions([cond_id])
    db.close()
    assert [e.dst_id for e in edges] == [cond_id]
    assert cond[cond_id].status == "pending"

    # resolve it, then unblock the work
    runner.invoke(cli.app, ["edit", "condition", "EAC Linux", "resolved", "2026-03-01"])
    res = runner.invoke(cli.app, ["edit", "unblock", ent.id, "EAC Linux"])
    assert res.exit_code == 0
    db = Database(edit_db)
    resolved = db.get_conditions([cond_id])[cond_id]
    remaining = db.edges_from(ent.id, RelationKind.BLOCKED_BY)
    db.close()
    assert resolved.status == "resolved" and resolved.resolve_date == date(2026, 3, 1)
    assert remaining == []  # the BLOCKED_BY link is gone (condition node kept)


def test_edit_condition_resolved_requires_a_date(edit_db: Path) -> None:
    assert runner.invoke(cli.app, ["edit", "condition", "X", "resolved"]).exit_code != 0
    assert runner.invoke(cli.app, ["edit", "condition", "X", "nonsense"]).exit_code != 0


def test_relate_refuses_self_link_from_substring_collision(edit_db: Path) -> None:
    # "Untitled" only matches the seeded "Untitled Project" -> src==dst; must refuse, not self-loop
    db0 = Database(edit_db)
    ent = next(db0.iter_entities())  # "Untitled Project"
    db0.close()
    res = runner.invoke(cli.app, ["relate", ent.id, "sequel", "Untitled"])
    assert res.exit_code == 1 and "Self-link refused" in res.output
    db = Database(edit_db)
    edges = db.edges_from(ent.id, RelationKind.DERIVED_FROM)
    db.close()
    assert edges == []  # no self-referential lineage edge was written


def test_relate_warns_on_ambiguous_source_but_still_links(edit_db: Path) -> None:
    # two distinct works share a prefix; relating to the bare prefix is ambiguous (no exact title)
    db0 = Database(edit_db)
    ent = next(db0.iter_entities())  # the derivative
    src = Entity.create("Source Saga: Origins", MediaKind.MOVIE)
    other = Entity.create("Source Saga: Reckoning", MediaKind.MOVIE)
    for e in (src, other):
        db0.upsert_entity(e)
        db0.upsert_node(Node(id=e.id, node_kind=NodeKind.WORK, name=e.title, owned=True))
    db0.close()
    res = runner.invoke(cli.app, ["relate", ent.id, "sequel", "Source Saga"])
    assert res.exit_code == 0 and "matched 2 works" in res.output  # warned...
    db = Database(edit_db)
    edges = db.edges_from(ent.id, RelationKind.DERIVED_FROM)
    db.close()
    assert len(edges) == 1  # ...but still wrote the edge (warning, not a hard stop)


def test_add_season_canonical_titles_and_sets_coords(edit_db: Path) -> None:
    # `rdt add "Pluribus" --season 2` (no --now: no network) -> a fully-coorded TV-season entry
    res = runner.invoke(cli.app, ["add", "Pluribus", "--season", "2"])
    assert res.exit_code == 0
    db = Database(edit_db)
    ent = next(e for e in db.iter_entities() if e.title == "Pluribus: Season 2")
    db.close()
    assert ent.kind is MediaKind.TV  # --season implies tv
    assert ent.season == 2 and ent.part is None  # structured coords, not just a parsed title
    assert ent.id.startswith("tv-pluribus-season-2-")


def _stub_lookup(report: RdReport, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `rdt rd --track` resolve to a fixed report (no network), exercising the real CLI."""

    async def _fake(*_a: object, **_k: object) -> RdReport:
        return report

    monkeypatch.setattr(cli, "lookup", _fake)


def test_rdadd_captures_tech_without_a_canonical_id(
    edit_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # /rd-add is a media AND tech tracker: an unresolvable kind (tech, no Tier-0 id) must still
    # capture as a bare WANT entity (no auto-pull) — the path that seeds gadgets like Steam Frame.
    _stub_lookup(
        RdReport(
            query="AYANEO Pocket PLAY",
            found=False,
            kind=MediaKind.TECH,
            matched_title="AYANEO Pocket PLAY",
            canonical={},  # tech pins nothing
        ),
        monkeypatch,
    )
    res = runner.invoke(
        cli.app, ["rd", "AYANEO Pocket PLAY", "--kind", "tech", "--region", "DE", "--track"]
    )
    assert res.exit_code == 0 and "Tracked" in res.output
    db = Database(edit_db)
    ent = next(e for e in db.iter_entities() if e.title == "AYANEO Pocket PLAY")
    db.close()
    assert ent.kind is MediaKind.TECH
    assert ent.consumption_state is ConsumptionState.WANT
    assert ent.external_ids == {}  # no canonical id, and that's fine


def test_rdadd_skips_unknown_kind(edit_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # the one non-capture: the engine can't even tell what the title is (no kind) -> not tracked
    _stub_lookup(RdReport(query="???", found=False, kind=None), monkeypatch)
    before = {e.title for e in Database(edit_db).iter_entities()}
    res = runner.invoke(cli.app, ["rd", "???", "--track"])
    assert res.exit_code == 2  # a true no-match short-circuits ("No confident match")
    db = Database(edit_db)
    titles = {e.title for e in db.iter_entities()}
    db.close()
    assert titles == before  # the kind-less miss wrote nothing


def _seed_obs(db: Database, entity_id: str, channel: ReleaseChannel, provider: str) -> None:
    db.upsert_observation(
        ReleaseObservation(
            entity_id=entity_id,
            channel=channel,
            region="WW",
            release_date=date(2026, 7, 31),
            precision=DatePrecision.EXACT,
            certainty=Certainty.ESTIMATED,
            source_tier=SourceTier.RUMOR,
            provider=provider,
            source_name=provider,
            confidence=0.3,
            fetched_at=utc_now(),
        )
    )


def test_cleardate_predicted_only_keeps_other_providers(edit_db: Path) -> None:
    db = Database(edit_db)
    eid = next(iter(db.iter_entities())).id
    _seed_obs(db, eid, ReleaseChannel.DIGITAL, "model")  # a stale prediction
    _seed_obs(db, eid, ReleaseChannel.DIGITAL, "manual")  # a hand-authored date
    _seed_obs(db, eid, ReleaseChannel.THEATRICAL, "tmdb")  # a different channel
    db.close()

    res = runner.invoke(cli.app, ["edit", "cleardate", eid, "digital", "--predicted"])
    assert res.exit_code == 0 and "Cleared 1" in res.output

    db = Database(edit_db)
    rows = {(o.channel, o.provider) for o in db.iter_observations(eid)}
    db.close()
    assert (ReleaseChannel.DIGITAL, "model") not in rows  # the prediction is gone
    assert (ReleaseChannel.DIGITAL, "manual") in rows  # the hand date survives
    assert (ReleaseChannel.THEATRICAL, "tmdb") in rows  # the other channel is untouched


def test_cleardate_whole_channel_when_no_flag(edit_db: Path) -> None:
    db = Database(edit_db)
    eid = next(iter(db.iter_entities())).id
    _seed_obs(db, eid, ReleaseChannel.DIGITAL, "model")
    _seed_obs(db, eid, ReleaseChannel.DIGITAL, "manual")
    _seed_obs(db, eid, ReleaseChannel.THEATRICAL, "tmdb")
    db.close()

    res = runner.invoke(cli.app, ["edit", "cleardate", eid, "digital"])
    assert res.exit_code == 0 and "Cleared 2" in res.output

    db = Database(edit_db)
    channels = {o.channel for o in db.iter_observations(eid)}
    db.close()
    assert ReleaseChannel.DIGITAL not in channels  # both digital rows cleared
    assert ReleaseChannel.THEATRICAL in channels  # theatrical preserved


# --- the library seam --------------------------------------------------------------------
# `edits.*` is what both `rdt edit …` and the TUI's card editor call, so it is tested
# directly rather than only through the CLI that wraps it.


@pytest.fixture
def work(tmp_path: Path) -> Iterator[tuple[Database, Entity]]:
    db = Database(tmp_path / "w.db")
    ent = Entity.create("Untitled Project", MediaKind.MOVIE)
    db.upsert_entity(ent)
    db.upsert_node(Node(id=ent.id, node_kind=NodeKind.WORK, name=ent.title, owned=True))
    yield db, ent
    db.close()


def test_set_date_rewrites_a_window_in_place(work: tuple[Database, Entity]) -> None:
    """The upsert trap: an observation's id hashes its *start*, and the conflict clause
    refreshes neither date, so moving only the end of a window silently did nothing."""
    db, ent = work
    edits.set_date(db, ent, ReleaseChannel.PRIMARY, "2027/2029")
    edits.set_date(db, ent, ReleaseChannel.PRIMARY, "2027/2030")
    obs = _manual_obs(db, ent.id)
    assert len(obs) == 1  # replaced, not duplicated
    assert (obs[0].release_date, obs[0].date_end) == (date(2027, 1, 1), date(2030, 1, 1))


def test_set_date_returns_the_canonical_literal(work: tuple[Database, Entity]) -> None:
    db, ent = work
    written = edits.set_date(db, ent, ReleaseChannel.DIGITAL, "2026 Q3~")
    assert written.edtf == "2026-35~"  # normalized on the way in, canonical on the way out
    assert written.certainty is Certainty.ESTIMATED
    assert written.when == date(2026, 7, 1)


def test_manual_dates_reads_back_what_was_authored(work: tuple[Database, Entity]) -> None:
    db, ent = work
    edits.set_date(db, ent, ReleaseChannel.THEATRICAL, "2026-09-18")
    edits.set_date(db, ent, ReleaseChannel.DIGITAL, "2027..2029")
    assert edits.manual_dates(db, ent.id) == {
        ReleaseChannel.THEATRICAL: "2026-09-18",
        ReleaseChannel.DIGITAL: "2027/2029",
    }


def test_a_bad_literal_raises_and_writes_nothing(work: tuple[Database, Entity]) -> None:
    db, ent = work
    with pytest.raises(ValueError, match="EDTF"):
        edits.set_date(db, ent, ReleaseChannel.PRIMARY, "2026-13")
    assert _manual_obs(db, ent.id) == []


def test_clear_date_spares_the_pulled_observation(work: tuple[Database, Entity]) -> None:
    """Clearing a hand-authored date should surface the puller's again, not erase it."""
    db, ent = work
    db.upsert_observation(
        ReleaseObservation(
            entity_id=ent.id,
            channel=ReleaseChannel.DIGITAL,
            release_date=date(2026, 5, 1),
            precision=DatePrecision.EXACT,
            certainty=Certainty.CONFIRMED,
            source_tier=SourceTier.AGGREGATOR,
            provider="tmdb",
            fetched_at=utc_now(),
        )
    )
    edits.set_date(db, ent, ReleaseChannel.DIGITAL, "2027")
    assert edits.clear_date(db, ent, ReleaseChannel.DIGITAL) == 1
    remaining = list(db.iter_observations(ent.id))
    assert [o.provider for o in remaining] == ["tmdb"]


def test_credits_are_removed_by_id_so_one_row_goes_at_a_time(
    work: tuple[Database, Entity],
) -> None:
    db, ent = work
    keep = edits.add_credit(db, ent, "Denis Villeneuve", CreditRole.DIRECTOR)
    drop = edits.add_credit(db, ent, "Hans Zimmer", CreditRole.COMPOSER)
    assert edits.remove_credits(db, ent, [drop.id]) == 1
    left = db.edges_to(ent.id, RelationKind.CREDITED_ON)
    assert [e.src_id for e in left] == [keep.id]


def test_a_hand_authored_credit_is_marked_as_one(work: tuple[Database, Entity]) -> None:
    db, ent = work
    node = edits.add_credit(db, ent, "A24", CreditRole.STUDIO, org=True)
    (edge,) = db.edges_to(ent.id, RelationKind.CREDITED_ON)
    assert node.node_kind is NodeKind.ORG and node.owned
    assert edge.source_provider == edits.USER_PROVIDER
    assert edge.source_tier is SourceTier.OFFICIAL and edge.owned


def test_a_pinned_credit_lands_on_the_canonical_node(work: tuple[Database, Entity]) -> None:
    """Pinning is what stops a hand-added credit forking off the resolved one."""
    db, ent = work
    node = edits.add_credit(db, ent, "Denis Villeneuve", CreditRole.DIRECTOR, pin=("tmdb", "287"))
    assert node.id == "person:tmdb:287"


def test_tags_and_platforms_round_trip(work: tuple[Database, Entity]) -> None:
    db, ent = work
    genre = edits.add_tag(db, ent, "sci-fi", DescriptorKind.GENRE)
    theme = edits.add_tag(db, ent, "grief", DescriptorKind.THEME)
    where = edits.add_platform(db, ent, "Max")
    assert genre.descriptor_kind is DescriptorKind.GENRE
    assert theme.descriptor_kind is DescriptorKind.THEME
    assert edits.remove_tags(db, ent, [theme.id]) == 1
    assert [e.dst_id for e in db.edges_from(ent.id, RelationKind.EXHIBITS)] == [genre.id]
    assert edits.remove_platforms(db, ent, [where.id]) == 1
    assert db.edges_from(ent.id, RelationKind.AVAILABLE_ON) == []


def test_removing_something_that_is_not_there_is_not_an_error(
    work: tuple[Database, Entity],
) -> None:
    db, ent = work
    assert edits.remove_credits(db, ent, ["person:nobody"]) == 0
    assert edits.remove_tags(db, ent, []) == 0


# --- slice coordinates ----------------------------------------------------------------------
@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    """A bare db — these exercise `edits.set_coords` directly, not through Typer."""
    store = Database(tmp_path / "coords.db")
    yield store
    store.close()


def _tv(db: Database, title: str, **kw: object) -> Entity:
    entity = Entity.create(title, MediaKind.TV, **kw)  # type: ignore[arg-type]
    db.upsert_entity(entity)
    return entity


def _series_edge(db: Database, entity: Entity) -> Edge:
    (edge,) = db.edges_from(entity.id, RelationKind.PART_OF_SERIES)
    return edge


def test_set_coords_writes_the_entity_and_the_edge(db: Database) -> None:
    """Both, deliberately: the puller resolves a season off the entity coord while the series
    walk reads the edge, and letting them drift is how a season pulls one date and lists
    under another."""
    entity = _tv(db, "Pluribus: Season 2")
    edits.set_coords(db, entity, season=2, part=1, part_label="Act", series="Pluribus")

    stored = db.get_entity(entity.id)
    assert stored is not None
    assert (stored.season, stored.part, stored.part_label) == (2, 1, "Act")
    edge = _series_edge(db, entity)
    assert (edge.ordinal, edge.part, edge.part_label) == (2, 1, "Act")
    assert edge.owned


def test_a_cut_above_the_season_grid_is_legal(db: Database) -> None:
    """The "Arcane: Noxus (Act 1)" shape — the split *is* the numbering, not a slice of one."""
    entity = _tv(db, "Arcane: Noxus (Act 1)")
    edits.set_coords(db, entity, season=None, part=1, part_label="Act", series="Arcane: Noxus")
    edge = _series_edge(db, entity)
    assert (edge.ordinal, edge.part) == (None, 1)


def test_set_coords_needs_a_series_when_there_is_none(db: Database) -> None:
    with pytest.raises(edits.NoSeriesError):
        edits.set_coords(db, _tv(db, "Pluribus: Season 2"), season=2)


def test_set_coords_refuses_to_guess_between_two_series(db: Database) -> None:
    """`edges[0]` silently picked one, which is also what made --series unusable."""
    entity = _tv(db, "Crossover: Season 1")
    for name in ("Series A", "Series B"):
        edits.set_coords(db, entity, season=1, series=name)
    with pytest.raises(edits.NoSeriesError):
        edits.set_coords(db, entity, season=2)
    # naming one resolves it
    edits.set_coords(db, entity, season=2, series="Series A")


def test_a_coordinate_can_be_cleared(db: Database) -> None:
    """`upsert_entity` COALESCEs the coords so a pull cannot wipe them — which also means an
    upsert alone can never clear one. An explicit edit is the caller that means "unset"."""
    entity = _tv(db, "Show: Season 5, Part 1")
    edits.set_coords(db, entity, season=5, part=1, part_label="Part", series="Show")
    edits.set_coords(db, entity, season=5, part=None, series="Show")

    stored = db.get_entity(entity.id)
    assert stored is not None
    assert (stored.season, stored.part, stored.part_label) == (5, None, None)
