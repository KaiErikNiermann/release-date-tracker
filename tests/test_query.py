"""Tests for the query language — lexing, parsing, matching and completion.

Entirely pure: no database, no terminal, no network. Every case builds a ``TrackRow``
directly, so these run in milliseconds and cover the grammar exhaustively.
"""

from __future__ import annotations

from datetime import date

import pytest

from release_tracker import query
from release_tracker.contingency import Resolution, ResolutionStatus
from release_tracker.models import (
    Bucket,
    ConsumptionState,
    CreditRole,
    DatePrecision,
    DescriptorKind,
    MediaKind,
)
from release_tracker.views import CreditLine, DateCell, PlatformLine, TagLine, TrackRow


def _row(
    *,
    title: str = "Reacher: Season 3",
    kind: MediaKind = MediaKind.TV,
    aliases: tuple[str, ...] = (),
    credits: tuple[CreditLine, ...] = (),
    platforms: tuple[PlatformLine, ...] = (),
    tags: tuple[TagLine, ...] = (),
    series: tuple[str, ...] = (),
    season: int | None = None,
    part: int | None = None,
    bucket: Bucket = Bucket.UPCOMING,
    state: ConsumptionState = ConsumptionState.WANT,
    theatrical: date | None = None,
    digital: date | None = None,
    pivot: date | None = None,
    pivot_confirmed: bool = True,
    blockers: tuple[str, ...] = (),
    has_notes: bool = False,
) -> TrackRow:
    """A TrackRow with everything defaulted — each test overrides only what it exercises."""
    return TrackRow(
        entity_id=f"id-{title}",
        title=title,
        kind=kind,
        theatrical=(
            None
            if theatrical is None
            else DateCell(when=theatrical, precision=DatePrecision.EXACT, confirmed=True)
        ),
        digital=(
            None
            if digital is None
            else DateCell(when=digital, precision=DatePrecision.EXACT, confirmed=True)
        ),
        pivot_when=pivot,
        pivot_confirmed=pivot_confirmed,
        available_resolution=Resolution(status=ResolutionStatus.PENDING),
        blockers=blockers,
        credits=credits,
        platforms=platforms,
        what=tags,
        series=series,
        aliases=aliases,
        season=season,
        part=part,
        bucket=bucket,
        freshness=None,
        has_notes=has_notes,
        state=state,
    )


def _keep(source: str, *rows: TrackRow) -> list[str]:
    return [r.title for r in query.filter_rows(query.parse(source), rows)]


# --- lexing ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("reacher kind:tv", ["reacher", "kind:tv"]),
        ('cast:"Alan Ritchson"', ["cast:Alan Ritchson"]),
        ('"some tv show"', ["some tv show"]),
        ('actors:"name 1, name 2"', ["actors:name 1, name 2"]),
        ("-tag:comedy", ["-tag:comedy"]),
        ("   spaced   out   ", ["spaced", "out"]),
        ("", []),
    ],
)
def test_lex_basics(source: str, expected: list[str]) -> None:
    assert [t.text for t in query.lex(source)] == expected


def test_lex_keeps_apostrophes_literal() -> None:
    """The reason shlex was rejected: `'` must never be a quote in a media search box."""
    assert [t.text for t in query.lex("Don't Look Up")] == ["Don't", "Look", "Up"]
    assert [t.text for t in query.lex("The Hitchhiker's Guide")] == [
        "The",
        "Hitchhiker's",
        "Guide",
    ]


def test_lex_tolerates_an_unterminated_quote() -> None:
    """Half-typed quotes are the normal state of a live query bar, not an error."""
    assert [t.text for t in query.lex('cast:"Alan Rit')] == ["cast:Alan Rit"]


def test_lex_spans_round_trip_to_the_source() -> None:
    source = 'reacher cast:"Alan Ritchson" year:2026'
    for tok in query.lex(source):
        assert source[tok.start : tok.end].replace('"', "") == tok.text


# --- parsing --------------------------------------------------------------------------
def test_bare_terms_become_the_search_text() -> None:
    q = query.parse("the odyssey kind:movie")
    assert q.text == "the odyssey"
    assert q.kind_hint is MediaKind.MOVIE


def test_comma_values_are_or_ed_within_a_term() -> None:
    q = query.parse('cast:"Alan Ritchson, Maria Sten"')
    assert q.terms[0].values == ("Alan Ritchson", "Maria Sten")


def test_negation_accepts_dash_or_bang() -> None:
    assert query.parse("-tag:comedy").terms[0].negated
    assert query.parse("!tag:comedy").terms[0].negated


def test_field_aliases_resolve_to_canonical_names() -> None:
    assert query.parse("actors:x").terms[0].field == CreditRole.CAST.value
    assert query.parse("kind:tv-show").kind_hint is MediaKind.TV
    assert query.parse("on:netflix").terms[0].field == "platform"


@pytest.mark.parametrize(
    ("source", "lo", "hi"),
    [
        ("year:2026", 2026, 2026),
        ("year:2020..2026", 2020, 2026),
        ("year:>=2026", 2026, None),
        ("year:<=2030", None, 2030),
        ("year:>2026", 2027, None),
        ("year:<2030", None, 2029),
    ],
)
def test_year_ranges(source: str, lo: int | None, hi: int | None) -> None:
    rng = query.parse(source).terms[0].ranges[0]
    assert (rng.lo, rng.hi) == (lo, hi)


def test_unknown_field_degrades_to_text_rather_than_vanishing() -> None:
    """`Andor: Season 2` is a title, not a typo'd field — it must still search."""
    q = query.parse("Andor: Season 2")
    assert q.unknown_fields == ("Andor",)
    assert "Andor:" in q.text
    assert _keep("Andor: Season 2", _row(title="Andor: Season 2")) == ["Andor: Season 2"]


def test_kind_anime_desugars_to_the_origin_tag() -> None:
    """anime is a medium, not a kind — the model keeps them orthogonal."""
    term = query.parse("kind:anime").terms[0]
    assert term.field == DescriptorKind.ORIGIN.value
    assert term.values == ("anime",)


def test_a_half_typed_field_constrains_nothing() -> None:
    assert query.parse("kind:").terms == ()


def test_parse_never_raises_on_junk() -> None:
    for junk in ('""', '"', ":::", "-", "year:abc", "  :  ", "a:b:c"):
        assert isinstance(query.parse(junk), query.Query)


# --- matching -------------------------------------------------------------------------
def test_empty_query_matches_everything() -> None:
    assert _keep("", _row(title="A"), _row(title="B")) == ["A", "B"]


def test_bare_term_matches_title_and_aliases() -> None:
    row = _row(title="The Odyssey", aliases=("Odyssey",))
    assert _keep("odyssey", row) == ["The Odyssey"]
    assert _keep("the ody", row) == ["The Odyssey"]


def test_multiple_bare_terms_are_and_ed() -> None:
    row = _row(title="Dune: Part Two")
    assert _keep("dune two", row) == ["Dune: Part Two"]
    assert _keep("dune three", row) == []


def test_credit_role_is_respected() -> None:
    """The whole point of carrying roles: director: and cast: are different questions."""
    row = _row(
        title="Tenet",
        credits=(
            CreditLine(
                role=CreditRole.DIRECTOR, name="Christopher Nolan", node_id="n1", owned=False
            ),
            CreditLine(
                role=CreditRole.CAST, name="John David Washington", node_id="n2", owned=False
            ),
        ),
    )
    assert _keep("director:nolan", row) == ["Tenet"]
    assert _keep("cast:nolan", row) == []
    assert _keep("person:nolan", row) == ["Tenet"]


def test_credits_beyond_the_display_limit_are_still_matchable() -> None:
    """The regression guard for un-truncating TrackRow: a 5th-billed actor must match."""
    row = _row(
        credits=tuple(
            CreditLine(role=CreditRole.CAST, name=f"Actor {i}", node_id=f"n{i}", owned=False)
            for i in range(6)
        )
    )
    assert row.who[:2] == ("Actor 0", "Actor 1")  # what the CLI still displays
    assert _keep('cast:"Actor 5"', row) != []


def test_tag_spans_every_descriptor_kind_while_narrow_fields_do_not() -> None:
    row = _row(
        tags=(
            TagLine(name="Sci-Fi", kind=DescriptorKind.GENRE, predicted=False),
            TagLine(name="body horror", kind=DescriptorKind.THEME, predicted=True),
        )
    )
    assert _keep("tag:sci-fi", row) != []
    assert _keep("tag:body", row) != []
    assert _keep("genre:sci-fi", row) != []
    assert _keep("genre:body", row) == []
    assert _keep('theme:"body horror"', row) != []


def test_year_matches_any_release_year_not_just_the_pivot() -> None:
    """A Dec-2026 theatrical with a Feb-2027 digital belongs to both years."""
    row = _row(
        theatrical=date(2026, 12, 20),
        digital=date(2027, 2, 10),
        pivot=date(2026, 12, 20),
    )
    assert _keep("year:2026", row) != []
    assert _keep("year:2027", row) != []
    assert _keep("year:2025", row) == []


def test_tba_row_matches_no_year() -> None:
    assert _keep("year:2026", _row(pivot=None)) == []


def test_negation_inverts_the_term() -> None:
    horror = _row(
        title="H", tags=(TagLine(name="horror", kind=DescriptorKind.GENRE, predicted=False),)
    )
    comedy = _row(
        title="C", tags=(TagLine(name="comedy", kind=DescriptorKind.GENRE, predicted=False),)
    )
    assert _keep("-tag:comedy", horror, comedy) == ["H"]


def test_is_bucket_and_flags() -> None:
    avail = _row(title="A", bucket=Bucket.AVAILABLE)
    upcoming = _row(title="U", bucket=Bucket.UPCOMING, blockers=("region",))
    assert _keep("is:available", avail, upcoming) == ["A"]
    assert _keep("is:blocked", avail, upcoming) == ["U"]
    assert _keep("is:tba", avail, upcoming) == ["A", "U"]


def test_is_bucket_is_a_separate_axis_from_state() -> None:
    """bucket `watched` spans watched|dropped|skipped; state `watched` is only one of them."""
    dropped = _row(title="D", bucket=Bucket.WATCHED, state=ConsumptionState.DROPPED)
    assert _keep("is:watched", dropped) == ["D"]
    assert _keep("state:watched", dropped) == []
    assert _keep("state:dropped", dropped) == ["D"]


def test_terms_compose_with_and() -> None:
    rows = (
        _row(title="A", kind=MediaKind.MOVIE, pivot=date(2026, 5, 1)),
        _row(title="B", kind=MediaKind.TV, pivot=date(2026, 5, 1)),
        _row(title="C", kind=MediaKind.MOVIE, pivot=date(2025, 5, 1)),
    )
    assert _keep("kind:movie year:2026", *rows) == ["A"]


def test_season_part_and_series() -> None:
    row = _row(series=("Reacher",), season=3, part=2)
    assert _keep("series:reacher season:3 part:2", row) != []
    assert _keep("season:4", row) == []


def test_platform_matches() -> None:
    row = _row(platforms=(PlatformLine(name="Prime Video", predicted=False),))
    assert _keep("platform:prime", row) != []
    assert _keep("on:prime", row) != []


# --- search-intent projection ---------------------------------------------------------
def test_one_query_projects_to_the_external_search_knobs() -> None:
    """Searching and adding are the same grammar: these feed capture_candidates directly."""
    q = query.parse('reacher kind:tv year:2026 season:3 cast:"Alan Ritchson"')
    assert q.text == "reacher"
    assert q.kind_hint is MediaKind.TV
    assert q.year_hint == 2026
    assert q.season_hint == 3


def test_a_year_range_is_not_an_exact_hint() -> None:
    assert query.parse("year:2020..2026").year_hint is None


# --- completion -----------------------------------------------------------------------
_VOCAB = query.Vocabulary(
    descriptors=(
        query.VocabEntry(value="horror", uses=9, descriptor_kind=DescriptorKind.GENRE),
        query.VocabEntry(value="body horror", uses=2, descriptor_kind=DescriptorKind.THEME),
    ),
    people=(
        query.VocabEntry(value="Alan Ritchson", uses=3),
        query.VocabEntry(value="Denis Villeneuve", uses=7),
    ),
    orgs=(query.VocabEntry(value="A24", uses=4),),
    platforms=(query.VocabEntry(value="Netflix", uses=12),),
    credits={
        CreditRole.DIRECTOR: (query.VocabEntry(value="Denis Villeneuve", uses=7),),
        CreditRole.CAST: (
            query.VocabEntry(value="Alan Ritchson", uses=3),
            query.VocabEntry(value="Timothee Chalamet", uses=2),
        ),
        CreditRole.STUDIO: (query.VocabEntry(value="A24", uses=4),),
    },
)


def test_suggests_field_names_at_a_token_start() -> None:
    out = query.suggest("ca", 2, _VOCAB)
    assert "cast:" in [s.insert for s in out]


def test_suggests_values_after_a_colon() -> None:
    out = query.suggest("cast:rit", 8, _VOCAB)
    assert out[0].insert == 'cast:"Alan Ritchson"'
    assert out[0].label == "Alan Ritchson"


def test_credit_completion_is_scoped_to_the_role() -> None:
    """`director:` offering an actor suggests a name that provably cannot match."""
    assert [s.label for s in query.suggest("director:", 9, _VOCAB)] == ["Denis Villeneuve"]
    assert [s.label for s in query.suggest("cast:", 5, _VOCAB)] == [
        "Alan Ritchson",
        "Timothee Chalamet",
    ]
    # ...while person: still spans everyone, whatever they are credited as
    assert set(s.label for s in query.suggest("person:", 7, _VOCAB)) == {
        "Denis Villeneuve",
        "Alan Ritchson",
    }


def test_value_suggestions_are_ranked_by_usage() -> None:
    out = query.suggest("person:", 7, _VOCAB)
    assert [s.label for s in out] == ["Denis Villeneuve", "Alan Ritchson"]


def test_org_roles_complete_against_orgs_not_people() -> None:
    assert [s.label for s in query.suggest("studio:", 7, _VOCAB)] == ["A24"]


def test_a_role_nobody_holds_offers_nothing_rather_than_everyone() -> None:
    assert query.suggest("composer:", 9, _VOCAB) == ()


def test_enum_fields_complete_without_a_vocabulary() -> None:
    assert "kind:movie" in [s.insert for s in query.suggest("kind:mov", 8, _VOCAB)]
    assert "state:watching" in [s.insert for s in query.suggest("state:watch", 11, _VOCAB)]


def test_narrow_descriptor_field_only_offers_its_own_kind() -> None:
    assert [s.label for s in query.suggest("genre:", 6, _VOCAB)] == ["horror"]
    assert [s.label for s in query.suggest("theme:", 6, _VOCAB)] == ["body horror"]


def test_value_completion_is_scoped_to_the_typed_prefix() -> None:
    """A typed character has to narrow the list, or tab walks through what it ruled out."""
    labels = [s.label for s in query.suggest("is:a", 4, _VOCAB, limit=20)]
    assert labels == ["aging", "available"]  # not `dated`, `stale`, `speculative`, ...


def test_a_word_start_counts_as_a_prefix() -> None:
    """Names are looked up by whichever part of them comes to mind."""
    assert query.suggest("cast:rit", 8, _VOCAB)[0].label == "Alan Ritchson"


def test_a_mid_word_fragment_still_finds_something_rather_than_nothing() -> None:
    """Nothing starts with `itch`, so the substring fallback takes over."""
    assert query.suggest("cast:itch", 9, _VOCAB)[0].label == "Alan Ritchson"


def test_field_and_value_suggestions_are_told_apart() -> None:
    """The widget previews a value completion and splices a field one — hence the tag."""
    assert all(s.kind == "field" for s in query.suggest("ca", 2, _VOCAB))
    assert all(s.kind == "value" for s in query.suggest("cast:", 5, _VOCAB))


def test_rank_values_is_the_shared_half_of_completion() -> None:
    """The card's `director` field wants the same candidates the query bar would offer."""
    ranked = query.rank_values("director", _VOCAB, "")
    assert [e.value for e, _ in ranked] == ["Denis Villeneuve"]
    assert [e.value for e, _ in query.rank_values("cast", _VOCAB, "rit")] == ["Alan Ritchson"]
    assert query.rank_values("cast", _VOCAB, "zzz") == ()


def test_rank_values_resolves_a_field_alias() -> None:
    assert query.rank_values("actor", _VOCAB, "") == query.rank_values("cast", _VOCAB, "")


def test_apply_is_the_splice_the_widget_relies_on() -> None:
    source = "kind:tv cast:rit year:2026"
    assert query.apply(source, query.suggest(source, 16, _VOCAB)[0]) == (
        'kind:tv cast:"Alan Ritchson" year:2026'
    )


def test_suggestion_splices_back_into_the_source() -> None:
    """The contract the widget relies on: source[:start] + insert + source[end:]."""
    source = "kind:tv cast:rit year:2026"
    top = query.suggest(source, 16, _VOCAB)[0]
    assert (
        source[: top.start] + top.insert + source[top.end :]
        == 'kind:tv cast:"Alan Ritchson" year:2026'
    )


def test_completion_mid_string_leaves_the_tail_alone() -> None:
    source = "ca year:2026"
    top = query.suggest(source, 2, _VOCAB)[0]
    assert source[: top.start] + top.insert + source[top.end :] == "cast: year:2026"


def test_completes_only_the_segment_under_the_caret_in_a_comma_list() -> None:
    source = "cast:Denis,rit"
    top = query.suggest(source, len(source), _VOCAB)[0]
    assert top.insert == 'cast:"Denis,Alan Ritchson"'


def test_negation_is_preserved_through_completion() -> None:
    assert query.suggest("-genre:hor", 10, _VOCAB)[0].insert == "-genre:horror"


def test_external_carries_the_hints_and_drops_local_only_filters() -> None:
    """Refining in the browse bar then pressing `a` should not mean retyping the kind."""
    q = query.parse('dune kind:movie year:2026 is:available tag:horror cast:"Timothee Chalamet"')
    assert q.external == "dune kind:movie year:2026"


def test_external_of_a_bare_query_is_just_the_text() -> None:
    assert query.parse("the odyssey").external == "the odyssey"


def test_every_consumption_state_has_a_label() -> None:
    """A state with no case would silently render as the bare enum value."""
    from release_tracker import render

    labels = {s: render.state_label(s) for s in ConsumptionState}
    assert len(set(labels.values())) == len(ConsumptionState)
    # the two the watched view has always coloured must keep their hues
    assert labels[ConsumptionState.DROPPED] == "[red]dropped[/]"
    assert labels[ConsumptionState.SKIPPED] == "[yellow]skipped[/]"


# --- region-scoped platform filtering -----------------------------------------------------
def _where(*lines: tuple[str, tuple[str, ...]]) -> tuple[PlatformLine, ...]:
    return tuple(PlatformLine(n, predicted=False, regions=r) for n, r in lines)


def test_platform_at_region_binds_the_two_together() -> None:
    """`on:netflix@us` asks one question about one edge."""
    row = _row(platforms=_where(("Netflix", ("US", "CA"))))
    assert query.matches(query.parse("on:netflix@us"), row)
    assert not query.matches(query.parse("on:netflix@jp"), row)


def test_the_at_form_avoids_the_cross_product_the_separate_filters_allow() -> None:
    """This is why `@` exists: two independent predicates both hold on the wrong row."""
    row = _row(platforms=_where(("Netflix", ("DE",)), ("Paramount+", ("US",))))
    # both loose filters hold — a Netflix edge exists, a US edge exists — but not together
    assert query.matches(query.parse("on:netflix region:us"), row)
    assert not query.matches(query.parse("on:netflix@us"), row)


def test_a_bare_platform_term_stays_region_agnostic() -> None:
    """Back-compat: `on:netflix` must keep matching regardless of market."""
    assert query.matches(query.parse("on:netflix"), _row(platforms=_where(("Netflix", ("JP",)))))


def test_an_unscoped_platform_cannot_answer_a_region_question() -> None:
    """ "We don't know where" is not "yes, in the US" — a network must not claim a market."""
    row = _row(platforms=_where(("Showtime", ())))
    assert query.matches(query.parse("on:showtime"), row)
    assert not query.matches(query.parse("on:showtime@us"), row)
    assert not query.matches(query.parse("region:us"), row)


def test_a_bare_region_asks_only_where() -> None:
    row = _row(platforms=_where(("U-NEXT", ("JP",))))
    assert query.matches(query.parse("region:jp"), row)
    assert query.matches(query.parse("in:jp"), row)  # alias
    assert not query.matches(query.parse("region:us"), row)


def test_region_scoped_platforms_negate() -> None:
    row = _row(platforms=_where(("Netflix", ("US",))))
    assert not query.matches(query.parse("-on:netflix@us"), row)
    assert query.matches(query.parse("-on:netflix@jp"), row)
