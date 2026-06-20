"""Tests for the artist-radar store (followed flag + artist_links) and views."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from release_tracker import views
from release_tracker.artists import (
    add_membership,
    build_report,
    ensure_filmography_link,
    parse_link_spec,
    partition_filmography,
)
from release_tracker.config import get_settings
from release_tracker.db import Database
from release_tracker.models import (
    ArtistLink,
    CreditRole,
    Edge,
    Entity,
    LinkTier,
    MediaKind,
    Node,
    NodeKind,
    RelationKind,
    SocialPlatform,
    SourceTier,
)
from release_tracker.sources.tmdb import FilmCredit


def _artist(db: Database, name: str, *, followed: bool = True) -> Node:
    node = Node.create(NodeKind.PERSON, name, owned=True, followed=followed)
    db.upsert_node(node)
    return node


def test_followed_flag_and_listing(tmp_path: Path) -> None:
    db = Database(tmp_path / "a.db")
    _artist(db, "Acme Creator")
    _artist(db, "Other Person", followed=False)
    assert [n.name for n in db.followed_artists()] == ["Acme Creator"]
    # follow the second one later
    other = next(n for n in db.find_nodes("Other Person"))
    assert db.set_followed(other.id, followed=True) is True
    assert {n.name for n in db.followed_artists()} == {"Acme Creator", "Other Person"}


def test_followed_is_sticky_across_resolve(tmp_path: Path) -> None:
    db = Database(tmp_path / "a.db")
    node = _artist(db, "Villeneuve")
    # a later world-resolve (followed=False) must not drop them off the radar
    db.upsert_node(Node(id=node.id, node_kind=NodeKind.PERSON, name="Villeneuve", followed=False))
    assert db.get_node(node.id).followed is True  # type: ignore[union-attr]


def test_artist_link_roundtrip_and_tiering(tmp_path: Path) -> None:
    db = Database(tmp_path / "a.db")
    node = _artist(db, "Acme")
    db.upsert_artist_link(
        ArtistLink(node_id=node.id, platform=SocialPlatform.YOUTUBE, tier=LinkTier.FREE, url="u1")
    )
    db.upsert_artist_link(
        ArtistLink(node_id=node.id, platform=SocialPlatform.PATREON, tier=LinkTier.PAID, url="u2")
    )
    links = db.iter_artist_links(node.id)
    assert {(link.platform, link.tier) for link in links} == {
        (SocialPlatform.YOUTUBE, LinkTier.FREE),
        (SocialPlatform.PATREON, LinkTier.PAID),
    }
    # re-adding the same platform updates tier/url but keeps prior activity
    db.update_link_activity(
        node.id,
        SocialPlatform.YOUTUBE,
        title="New video",
        url="watch-url",
        posted_at=date(2026, 6, 10),
        fetched_at=datetime.now(UTC),
    )
    db.upsert_artist_link(
        ArtistLink(node_id=node.id, platform=SocialPlatform.YOUTUBE, tier=LinkTier.FREE, url="u1b")
    )
    yt = next(
        link for link in db.iter_artist_links(node.id) if link.platform is SocialPlatform.YOUTUBE
    )
    assert yt.url == "u1b"
    assert yt.latest_title == "New video"  # activity preserved across a link re-add
    assert yt.last_post_at == date(2026, 6, 10)


def test_parse_link_spec() -> None:
    spec = parse_link_spec("youtube:free:https://youtube.com/@x")
    assert spec.platform is SocialPlatform.YOUTUBE
    assert spec.tier is LinkTier.FREE
    assert spec.url == "https://youtube.com/@x"  # url keeps its colons
    assert parse_link_spec("madeup:paid:https://x").platform is SocialPlatform.OTHER
    for bad in ("youtube:free", "youtube:notatier:url"):
        try:
            parse_link_spec(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def _link(db: Database, node: Node, platform: SocialPlatform, tier: LinkTier, when: date) -> None:
    db.upsert_artist_link(ArtistLink(node_id=node.id, platform=platform, tier=tier, url="u"))
    db.update_link_activity(
        node.id, platform, title="t", url="latest-url", posted_at=when, fetched_at=datetime.now(UTC)
    )


def test_artists_radar_sorts_by_recency(tmp_path: Path) -> None:
    db = Database(tmp_path / "a.db")
    older = _artist(db, "Older")
    newer = _artist(db, "Newer")
    _artist(db, "Silent")  # no links/activity -> sorts last
    _link(db, older, SocialPlatform.YOUTUBE, LinkTier.FREE, date(2026, 1, 1))
    _link(db, newer, SocialPlatform.BLUESKY, LinkTier.AUXILIARY, date(2026, 6, 10))
    rows = views.artists(db, date(2026, 6, 19), get_settings(), sort="recency")
    assert [r.name for r in rows] == ["Newer", "Older", "Silent"]
    assert rows[0].last_post == (SocialPlatform.BLUESKY, date(2026, 6, 10))
    # name sort is alphabetical
    assert [r.name for r in views.artists(db, date(2026, 6, 19), get_settings(), sort="name")] == [
        "Newer",
        "Older",
        "Silent",
    ]


def test_build_report_links_and_tracked_works(tmp_path: Path) -> None:
    db = Database(tmp_path / "a.db")
    artist = _artist(db, "Denis Villeneuve")
    db.upsert_artist_link(
        ArtistLink(node_id=artist.id, platform=SocialPlatform.YOUTUBE, tier=LinkTier.FREE, url="yt")
    )
    # a credited work, so the report's "works you track" is populated
    work = Entity.create("Dune", MediaKind.MOVIE)
    db.upsert_entity(work)
    db.upsert_node(Node(id=work.id, node_kind=NodeKind.WORK, name="Dune", owned=True))
    db.upsert_edge(
        Edge(
            src_id=artist.id,
            dst_id=work.id,
            relation=RelationKind.CREDITED_ON,
            role=CreditRole.DIRECTOR,
            source_provider="tmdb",
            source_tier=SourceTier.AGGREGATOR,
        )
    )
    report = build_report(db, artist, "Denis Villeneuve", get_settings(), date(2026, 6, 19))
    assert report.found is True
    d = report.to_dict()
    assert d["name"] == "Denis Villeneuve"
    assert [link_["platform"] for link_ in d["links"]] == ["youtube"]  # type: ignore[union-attr]
    assert [w["title"] for w in d["tracked_works"]] == ["Dune"]  # type: ignore[union-attr]
    # an unknown artist -> not found
    assert build_report(db, None, "Nobody", get_settings(), date(2026, 6, 19)).found is False


def test_membership_links_person_and_group(tmp_path: Path) -> None:
    db = Database(tmp_path / "a.db")
    # both nodes are created on demand (membership is independent of following)
    person, group = add_membership(db, "Thomas Grip", "Frictional Games")
    assert person.node_kind is NodeKind.PERSON
    assert group.node_kind is NodeKind.ORG
    assert [n.name for n in views.members_of(db, group)] == ["Thomas Grip"]
    assert [n.name for n in views.groups_of(db, person)] == ["Frictional Games"]
    # the group's report surfaces its members; the person's report surfaces their groups
    s, today = get_settings(), date(2026, 6, 19)
    group_report = build_report(db, group, "Frictional Games", s, today)
    assert [m["name"] for m in group_report.to_dict()["members"]] == ["Thomas Grip"]  # type: ignore[union-attr]
    person_report = build_report(db, person, "Thomas Grip", s, today)
    assert [g["name"] for g in person_report.to_dict()["groups"]] == ["Frictional Games"]  # type: ignore[union-attr]


# --- filmography pipeline (film/TV creators) ------------------------------
def _credit(title: str, when: date | None, role: str = "Director") -> FilmCredit:
    return FilmCredit(
        title=title, media="movie", role=role, when=when, tmdb_id=title, url=f"u/{title}"
    )


def test_partition_filmography_splits_latest_and_slate() -> None:
    today = date(2026, 6, 19)
    credits = (
        _credit("Old", date(2020, 1, 1)),
        _credit("Recent", date(2025, 11, 1)),  # latest released
        _credit("Soon", date(2026, 11, 24)),
        _credit("Later", date(2027, 9, 1)),
        _credit("Undated", None),  # dropped from both halves
    )
    latest, slate = partition_filmography(credits, today)
    assert latest is not None and latest.title == "Recent"
    assert [c.title for c in slate] == ["Soon", "Later"]  # upcoming, soonest first


def test_partition_filmography_honours_slate_limit() -> None:
    today = date(2026, 6, 19)
    credits = tuple(_credit(f"F{i}", date(2027, 1, i + 1)) for i in range(10))
    _, slate = partition_filmography(credits, today, limit=3)
    assert [c.title for c in slate] == ["F0", "F1", "F2"]


def test_ensure_filmography_link_derives_from_tmdb_person_id(tmp_path: Path) -> None:
    db = Database(tmp_path / "a.db")
    node = Node.create(NodeKind.PERSON, "A Director", source="tmdb", source_id="7467", owned=True)
    db.upsert_node(node)
    ensure_filmography_link(db, node)
    (link,) = db.iter_artist_links(node.id)
    assert link.platform is SocialPlatform.FILMOGRAPHY
    assert link.tier is LinkTier.FREE
    assert link.handle == "7467"
    # idempotent — a second call doesn't fork a duplicate
    ensure_filmography_link(db, node)
    assert len(db.iter_artist_links(node.id)) == 1


def test_ensure_filmography_link_skips_non_tmdb_nodes(tmp_path: Path) -> None:
    db = Database(tmp_path / "a.db")
    node = _artist(db, "A YouTuber")  # name-slug id, no canonical TMDB key
    ensure_filmography_link(db, node)
    assert db.iter_artist_links(node.id) == []
