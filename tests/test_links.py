"""Tests for the derived per-work source list.

The point of the split is honesty: a card must say what the tool can refetch and what the
user has to go read themselves. These pin the three ways a link is reached — a url a source
recorded, a pinned id run through a formatter, and a pre-built search when there is no id at
all — and that a site which declines automated extraction can never be marked refetchable.
"""

from __future__ import annotations

from release_tracker.links import SourceAccess, SourceLink, work_sources
from release_tracker.models import Entity, MediaKind


def _by_provider(links: tuple[SourceLink, ...]) -> dict[str, SourceLink]:
    return {link.provider: link for link in links}


def test_a_pinned_id_becomes_a_refetchable_link() -> None:
    movie = Entity.create("Dune: Part Two", MediaKind.MOVIE, external_ids={"tmdb": "693134"})
    link = _by_provider(work_sources(movie))["tmdb"]
    assert link.access is SourceAccess.AUTO
    assert link.url == "https://www.themoviedb.org/movie/693134"
    assert link.reason is None


def test_the_tmdb_path_follows_the_kind() -> None:
    show = Entity.create("Severance", MediaKind.TV, external_ids={"tmdb": "95396"})
    assert _by_provider(work_sources(show))["tmdb"].url == "https://www.themoviedb.org/tv/95396"


def test_a_tmdb_id_on_a_kind_with_no_path_is_dropped_not_guessed() -> None:
    """Better no link than one built from a segment we had to invent."""
    odd = Entity.create("Something", MediaKind.BOOK, external_ids={"tmdb": "1"})
    assert work_sources(odd) == ()


def test_a_site_that_declines_extraction_is_link_only_and_says_why() -> None:
    """The whole reason the split exists. GSMArena blocks ClaudeBot and its licence
    prohibits ai-inference, so it must never be presented as something we can re-pull."""
    phone = Entity.create(
        "Sony Xperia 1 VIII",
        MediaKind.TECH,
        external_ids={"wikidata": "Q139719408", "gsmarena": "14660"},
    )
    links = _by_provider(work_sources(phone))

    assert links["gsmarena"].access is SourceAccess.LINK
    assert links["gsmarena"].url == "https://www.gsmarena.com/model-14660.php"
    assert links["gsmarena"].reason
    # ...and the id we hold for it still lands on the exact device page, not a search.
    assert "14660" in links["gsmarena"].url

    assert links["wikidata"].access is SourceAccess.AUTO


def test_techpowerup_ids_deep_link_per_part_type() -> None:
    gpu = Entity.create(
        "GeForce RTX 5090", MediaKind.TECH, external_ids={"techpowerup_gpu": "4216"}
    )
    link = _by_provider(work_sources(gpu))["techpowerup_gpu"]
    assert link.url == "https://www.techpowerup.com/gpu-specs/wd.4216"
    assert link.access is SourceAccess.LINK


def test_a_recorded_url_wins_over_a_constructed_one() -> None:
    """IGDB is the case that forces this: we pin a numeric id but its pages are addressed by
    slug, so the url the source itself recorded is the only one that resolves."""
    game = Entity.create("Cyberpunk 2077", MediaKind.GAME, external_ids={"igdb": "1877"})
    links = work_sources(game, {"igdb": "https://www.igdb.com/games/cyberpunk-2077"})
    igdb = _by_provider(links)["igdb"]
    assert igdb.url == "https://www.igdb.com/games/cyberpunk-2077"
    assert igdb.access is SourceAccess.AUTO
    assert len([link for link in links if link.provider == "igdb"]) == 1, "not duplicated"


def test_our_own_rows_are_not_sources() -> None:
    """A hand-edit and our own prediction have no page to send anyone to."""
    movie = Entity.create("Some Film", MediaKind.MOVIE, external_ids={"tmdb": "1"})
    links = work_sources(movie, {"manual": "", "model": "", "tmdb": "https://x/1"})
    assert set(_by_provider(links)) == {"tmdb"}


def test_tech_with_no_ids_falls_back_to_prebuilt_searches() -> None:
    """The common case — Wikidata knows a small fraction of consumer devices. A query the
    user can click beats a shrug."""
    phone = Entity.create("Poco X7", MediaKind.TECH)
    links = work_sources(phone)
    assert links, "a device we know nothing about still gets somewhere to look"
    assert all(link.access is SourceAccess.LINK for link in links)
    assert any(link.provider == "search:web" for link in links)
    assert all("Poco+X7" in link.url for link in links)


def test_a_known_device_does_not_get_search_noise() -> None:
    """Searches are a fallback, not a garnish: once there is a real link, they'd be clutter."""
    phone = Entity.create("Xperia", MediaKind.TECH, external_ids={"wikidata": "Q1"})
    assert not any(link.provider.startswith("search:") for link in work_sources(phone))


def test_a_movie_with_nothing_pinned_gets_no_search_fallback() -> None:
    """The fallback is tech-only — for a film, an unpinned entity means the *title* was
    wrong, and the fix is to correct it rather than to go searching."""
    assert work_sources(Entity.create("Untitled", MediaKind.MOVIE)) == ()


def test_refetchable_sources_are_listed_first() -> None:
    """The card leads with what pressing update will actually do."""
    phone = Entity.create(
        "Sony Xperia 1 VIII",
        MediaKind.TECH,
        external_ids={"gsmarena": "14660", "wikidata": "Q139719408", "imdb": "tt1"},
    )
    accesses = [link.access for link in work_sources(phone)]
    assert accesses == sorted(accesses, key=lambda a: a is not SourceAccess.AUTO)
    assert accesses[0] is SourceAccess.AUTO


def test_an_official_site_is_used_verbatim() -> None:
    phone = Entity.create(
        "Thing", MediaKind.TECH, external_ids={"official_website": "https://sony.com/x"}
    )
    assert _by_provider(work_sources(phone))["official_website"].url == "https://sony.com/x"


def test_a_pinned_intel_ark_sku_deep_links() -> None:
    """ARK has no puller — no API, a client-rendered search page, and no Wikidata property to
    bridge from — so a SKU only ever arrives by hand. When it does, the numeric id alone
    resolves; Intel redirects it to the slugged url."""
    cpu = Entity.create("Core Ultra 9 285K", MediaKind.TECH, external_ids={"intel_ark": "241060"})
    link = _by_provider(work_sources(cpu))["intel_ark"]
    assert link.access is SourceAccess.LINK
    assert link.url.endswith("/products/sku/241060/specifications.html")
    assert link.reason
