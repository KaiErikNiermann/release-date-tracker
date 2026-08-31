"""The add palette: the same query syntax, pointed at the outside world.

Typing here searches the external sources rather than the tracker, but it is the *same*
grammar — `dune kind:movie year:2026` narrows a local list and hints an external search
identically, because one parsed Query feeds both. Explicit terms become the search's
kind/year/season hints; the bare words are the search text.

Network work is debounced and runs in an exclusive worker, so a later keystroke cancels
the request in flight instead of racing it.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import ClassVar

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from release_tracker import drafts, query
from release_tracker.capture import capture_work
from release_tracker.config import secret
from release_tracker.drafts import Draft
from release_tracker.lookup import (
    DETECT_KINDS,
    MATCH_FLOOR,
    capture_candidates,
    report_for_candidate,
)
from release_tracker.models import Entity, MediaKind
from release_tracker.sources import unavailable_for
from release_tracker.sources.base import Candidate
from release_tracker.sources.tmdb import SeasonRef, TmdbSource
from release_tracker.tech import looks_like_tech
from release_tracker.titles import extract_slice, slice_suffix, strip_trailing_season
from release_tracker.tui.draft import DraftScreen

KeyT = tuple[str, "MediaKind | None"]

# what to take off the search text once a cut has been read out of it
_PART_TAIL_RE = re.compile(
    r"[\s:,\-]*\b(?:part|pt\.?|vol(?:ume)?\.?|cour|act|chapter|ch\.?|book)"
    r"\s*(?:\d+|[ivxl]+|one|two|three|four|five)\b\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Typed:
    """What the bar resolves to: the text the sources see, the coords, and where they came from.

    One value read by search, capture and review alike. They used to each re-parse the bar and
    disagree — `enter` honoured a `season:` that `e` silently dropped — which is invisible from
    the outside because both paths "work".
    """

    text: str  # the stem, with any season words taken off: what a source should be asked
    kind: MediaKind | None
    year: int | None
    season: int | None = None
    part: int | None = None
    part_label: str | None = None  # what the cut is called; None reads as "Part"
    reasons: tuple[str, ...] = ()  # provenance, printed verbatim by the review screen

    @property
    def key(self) -> KeyT:
        """The memo/staleness key. On the *stem*, so typing " season 2" onto a searched title
        does not invalidate the results it is narrowing."""
        return (self.text.strip().lower(), self.kind)


def resolve(source: str) -> Typed:
    """Parse the bar into the coordinate every path downstream should use.

    An explicit ``season:`` is the user's own word and wins. Otherwise the text is read for a
    trailing season phrase, which is both a coordinate *and* a better search string — "yellow
    jackets season 2" is a worse query to TMDB than "yellowjackets".
    """
    parsed = query.parse(source)
    reasons: list[str] = []
    text = parsed.text

    # The cut comes off first, because it sits at the end and would otherwise hide the season
    # from `strip_trailing_season`, which is end-anchored: "stranger things season 5 act 1".
    #
    # The *word* a cut was sold under is only ever read back from what the user typed. No
    # source models a slice at all, and guessing the label from outside is worse than useless —
    # Wikipedia's Stranger Things article says "Chapter" because that is its episode-title
    # convention, while the split is actually sold as volumes. Unstated, it reads as "Part".
    part, label = parsed.part_hint, None
    if (cut := extract_slice(text)) is not None and cut.number is not None:
        stem = _PART_TAIL_RE.sub("", text).strip()
        if stem:  # a bare "act 1" is not a title, so only take the tail when a stem survives
            text = stem
            if part is None:
                part, label = cut.number, cut.label.title()
                reasons.append(f"read “{cut.token}” as {label} {part}")

    season = parsed.season_hint
    if season is None:
        text, season = strip_trailing_season(text)
        if season is not None:
            reasons.append(f"“season {season}” in what you typed names a season")
    return Typed(text, parsed.kind_hint, parsed.year_hint, season, part, label, tuple(reasons))


_DEBOUNCE = 0.6
_MIN_CHARS = 3
_MEMO_TTL = 120.0


@dataclass(frozen=True, slots=True)
class _SeasonList:
    """The picker's state: whose seasons these are, and what TMDB listed.

    Rendered into the same OptionList rather than a new modal — the pick has to come back to
    `AddScreen.capture` either way, and a second screen would need its own copy of that.
    """

    kind: MediaKind
    candidate: Candidate
    seasons: tuple[SeasonRef, ...]

    def at(self, index: int) -> SeasonRef | None:
        """The season a row index selects; ``None`` for row 0, the whole-show row."""
        return self.seasons[index - 1] if 1 <= index <= len(self.seasons) else None


def _coord_tail(season: int | None, part: int | None = None, label: str | None = None) -> str:
    """The " · Season 5 · Act 1" a row wears once a coordinate is in play, or nothing."""
    bits = [f"Season {season}"] if season is not None else []
    if cut := slice_suffix(part, label):
        bits.append(cut)
    return "".join(f" · {b}" for b in bits)


class AddScreen(ModalScreen[Entity | None]):
    """Search the external sources and capture a pick. Dismisses with the new entity."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "back", "Back", show=False),
        # The bar is one line, so `down` is dead in it; the OptionList binds its own, and
        # a widget's bindings beat the screen's — so this only ever fires from the bar.
        Binding("down", "focus_candidates", "Into the candidates", show=False),
        # Printable keys are swallowed while the Input has focus, so these only reach the
        # list — the same arrangement the browse table uses.
        Binding("j", "highlight(1)", "Down", show=False),
        Binding("k", "highlight(-1)", "Up", show=False),
        Binding("e", "review", "Review before adding", show=False),
        Binding("s", "seasons", "Browse this show's seasons", show=False),
        Binding("slash", "focus_query", "Search", show=False),
        Binding("tab", "move_focus(1)", "Focus next", show=False),
        Binding("shift+tab", "move_focus(-1)", "Focus previous", show=False),
    ]

    def __init__(self, initial: str = "") -> None:
        super().__init__()
        self._initial = initial
        self._timer: Timer | None = None
        self._hits: list[tuple[MediaKind, Candidate]] = []
        # The "add it yourself" row, always offered below the hits once a search has run.
        # Never auto-added: everything on it is inferred, so it only ever opens review.
        self._freeform: Draft | None = None
        # The key the shown hits came from, so `enter` can tell "act on these" from
        # "you have typed past them" without guessing at the debounce.
        self._shown_key: tuple[str, MediaKind | None] | None = None
        # Session-scoped, so backspacing through a query costs nothing. Deliberately not
        # in the library: a process-global search cache would surprise CLI callers.
        self._memo: dict[KeyT, tuple[float, list[tuple[MediaKind, Candidate]], Draft]] = {}
        # Set while the list is showing a show's seasons instead of the search hits.
        self._seasons: _SeasonList | None = None
        self._season_memo: dict[str, tuple[SeasonRef, ...]] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="add"):
            yield Static(
                Text.from_markup("[bold]Add a title[/]  [dim]— searches TMDB / IGDB / Steam[/]")
            )
            yield Input(placeholder="e.g. dune kind:movie year:2026", id="add-query")
            yield Static("", id="add-status")
            yield OptionList(id="candidates")

    def on_mount(self) -> None:
        bar = self.query_one("#add-query", Input)
        # Carry over the terms an external search can act on (text + kind/year/season);
        # the browse query's local-only filters (is:, tag:, state:) drop out.
        bar.value = query.parse(self._initial).external
        bar.focus()
        if bar.value:
            self._schedule()

    # --- searching -----------------------------------------------------------------
    @on(Input.Changed, "#add-query")
    def _on_change(self) -> None:
        self._schedule()

    @on(Input.Submitted, "#add-query")
    def _on_submit(self) -> None:
        """Enter means "act on what I typed", which is two things depending on the screen.

        If the listed candidates are the ones this query produced, enter steps into them —
        the browse bar's `enter -> table` move. If the query has moved on since (the
        debounce has not fired, or nothing has been searched yet), it runs the search now
        rather than focusing a stale list.
        """
        if self._hits and self._shown_key == self._current_key():
            self.action_focus_candidates()
        else:
            self._schedule(now=True)

    def _typed(self) -> Typed:
        """The bar as every path downstream should read it."""
        return resolve(self.query_one("#add-query", Input).value)

    def _current_key(self) -> KeyT:
        return self._typed().key

    def _schedule(self, *, now: bool = False) -> None:
        if self._timer is not None:
            self._timer.stop()
        typed = self._typed()
        if len(typed.text.strip()) < _MIN_CHARS:
            self._status("[dim]keep typing…[/]")
            return
        if now:
            self.search(typed)
        else:
            self._timer = self.set_timer(_DEBOUNCE, lambda: self.search(typed))

    def _status(self, markup: str) -> None:
        self.query_one("#add-status", Static).update(Text.from_markup(markup))

    @work(exclusive=True, group="add-search")
    async def search(self, typed: Typed) -> None:
        """Exclusive within its own group: a newer keystroke cancels this request rather
        than racing it — but never cancels a capture, which is a write."""
        from release_tracker.tui.app import RdtApp

        assert isinstance(self.app, RdtApp)
        text, kind_hint, key = typed.text, typed.kind, typed.key
        cached = self._memo.get(key)
        if cached is not None and time.monotonic() - cached[0] < _MEMO_TTL:
            self._show(cached[1], key, cached[2])
            return
        self._status(f"[dim]searching “{text}”…[/]")
        # Cleared by whoever finishes: `_show` on success, the handler below on failure.
        # Deliberately not a `finally` — a cancelled search must leave the spinner alone,
        # because the newer keystroke that cancelled it has already raised its own.
        self._candidates.loading = True
        try:
            hits = await capture_candidates(
                await self.app.http(), text, self.app.settings, kind_hint=kind_hint
            )
            # Tech is deliberately absent from the unhinted sweep: Wikidata label-matches
            # everything at score 1.0, so "Dune" would return a sand dune and a Klaus Schulze
            # album alongside the film. But once the media DBs have come back empty there is
            # nothing left to drown — so when the text plainly names a device, retry as tech.
            # Same routing `lookup` already does via `looks_like_tech`.
            if not hits and kind_hint is None and looks_like_tech(text):
                hits = await capture_candidates(
                    await self.app.http(), text, self.app.settings, kind_hint=MediaKind.TECH
                )
            # What we would write if none of the above is it. Always built, because a search
            # that found things can still have found the wrong things, and the unannounced
            # case — a device, a film nobody has listed yet, an album — is the one a release
            # tracker exists for. The hits feed the inference rather than suppressing it.
            freeform = await drafts.infer_freeform(
                await self.app.http(),
                text,
                kind_hint=kind_hint,
                year_hint=typed.year,
                season_hint=typed.season,
                hits=hits,
            )
        except Exception as exc:  # a dead provider must not kill the palette
            # The escape hatch matters most when the search itself is down, so the row still
            # appears — from the offline half of the inference. `_show` resets the hit list
            # with it, which a bare `return` here would leave stale and mis-indexed.
            self._show(
                [],
                key,
                drafts.prefill(
                    text, kind_hint=kind_hint, year_hint=typed.year, season_hint=typed.season
                ),
                status=f"[red]search failed:[/] {exc}",
            )
            return
        self._memo[key] = (time.monotonic(), hits, freeform)
        missing = (
            unavailable_for(self._searched_kinds(kind_hint), self.app.settings) if not hits else {}
        )
        self._show(hits, key, freeform, missing)

    def _searched_kinds(self, kind_hint: MediaKind | None) -> tuple[MediaKind, ...]:
        """The kinds this query actually reached, so only their sources are reported on."""
        return (kind_hint,) if kind_hint is not None else DETECT_KINDS

    def _show(
        self,
        hits: list[tuple[MediaKind, Candidate]],
        key: KeyT,
        freeform: Draft,
        missing: dict[str, str] | None = None,
        *,
        status: str | None = None,
    ) -> None:
        self._hits = hits
        self._freeform = freeform
        self._shown_key = key
        self._seasons = None  # these hits are not the show whose seasons were open
        typed = self._typed()
        options = self._candidates
        options.loading = False
        options.clear_options()
        for kind, cand in hits:
            year = f" [dim]({cand.year})[/]" if cand.year else ""
            extra = f"  [dim]{cand.extra[:60]}[/]" if cand.extra else ""
            # only on a series: a season means nothing on a film, and printing it there
            # would advertise a coordinate the capture is (correctly) about to drop.
            coord = (
                f" [cyan]{_coord_tail(typed.season, typed.part, typed.part_label).lstrip(' ·')}[/]"
                if kind is MediaKind.TV
                else ""
            )
            options.add_option(
                Option(
                    Text.from_markup(
                        f"[bold]{cand.title}[/]{coord}{year}  [dim]{kind.value} · {cand.id_key}"
                        f":{cand.canonical_id} · {cand.score:.2f}[/]{extra}"
                    )
                )
            )
        options.add_option(Option(Text.from_markup(self._freeform_label(freeform, hits))))
        self._status(status if status is not None else self._search_status(hits, missing, key))

    def _freeform_label(self, draft: Draft, hits: list[tuple[MediaKind, Candidate]]) -> str:
        """The one row, worded for the situation it is standing in.

        A device with a lineage keeps the fullest form — it is the case with the most inferred
        and so the most worth stating up front. Otherwise the wording turns on whether anything
        credible came back: with real matches this is the quiet "none of these", and without
        them it is the main way forward and says so.
        """
        if draft.version is not None:
            version = f" {draft.version.token}"
            follows = (
                f"  [dim]follows {draft.predecessor.label}[/]"
                if draft.predecessor is not None
                else "  [dim]no lineage found[/]"
            )
            return (
                f"[bold]{draft.title}[/]  [dim]not announced · track it as new{version}[/]{follows}"
            )
        coord = _coord_tail(draft.season, draft.part, draft.part_label)
        kind = f"  [dim]as {draft.kind.value}[/][cyan]{coord}[/]"
        if any(cand.score >= MATCH_FLOOR for _, cand in hits):
            return f"[bold]+ Add “{draft.title}” myself[/]  [dim]— none of these[/]{kind}"
        return f"[bold]+ Add “{draft.title}” as a new entry[/]  [dim]— nothing matched[/]{kind}"

    def _read_as(self) -> str:
        """How the bar was interpreted, when that differs from what is in it.

        Only shown for an *inference*. An explicit `season:2` is already on screen — repeating
        it would be noise — but text silently becoming a coordinate has to be visible, or a
        wrong guess is indistinguishable from a wrong search.
        """
        typed = self._typed()
        if not typed.reasons or typed.season is None:
            return ""
        return f"[cyan]read as[/] [bold]{typed.text}[/] [cyan]+ season:{typed.season}[/] · "

    def _search_status(
        self,
        hits: list[tuple[MediaKind, Candidate]],
        missing: dict[str, str] | None,
        key: KeyT,
    ) -> str:
        """What the search itself found. The freeform row is *extra* to this line, never a
        replacement for it — an empty result still has to say why it was empty, or a typo and
        an unconfigured source both read as "this thing does not exist"."""
        if hits:
            return (
                f"{self._read_as()}[dim]{len(hits)} candidate(s) · ↓ into the list, enter adds,"
                " e reviews first · esc back[/]"
            )
        prefix, tail = self._read_as(), " [dim]— or add it yourself, last row[/]"
        if missing:
            # An unconfigured source returns an empty list exactly like a real miss, so
            # without this the answer to "why is Dune not here" is indistinguishable from
            # "Dune does not exist". Name what was never asked.
            named = "; ".join(sorted(missing.values()))
            return f"{prefix}[yellow]nothing matched[/] [dim]— {named}[/]{tail}"
        # Only worth saying when it would actually change the outcome: a hinted search
        # already scoped itself, and a name we recognised as a device has already been
        # retried as tech, so telling either to add `kind:tech` sends them nowhere.
        already_tried = key[1] is not None or looks_like_tech(key[0])
        hint = "" if already_tried else " [dim]— for a device, add[/] [bold]kind:tech[/]"
        return f"{prefix}[yellow]no matches[/]{hint}{tail}"

    # --- seasons -------------------------------------------------------------------
    def action_seasons(self) -> None:
        """Open (or close) the highlighted show's season list.

        A show is the default and stays selectable — a limited series must never be pushed
        into a season it does not have — so this is opt-in, and says why when it declines.
        """
        if self._seasons is not None:  # already open: `s` toggles back to the hits
            self._close_seasons()
            return
        index = self._candidates.highlighted
        if index is None or not 0 <= index < len(self._hits):
            return
        kind, cand = self._hits[index]
        if kind is not MediaKind.TV:
            self._status("[dim]seasons are a TV idea — this row is not a series[/]")
            return
        if cand.id_key != "tmdb":
            self._status("[dim]no TMDB id on this match — nothing to list seasons from[/]")
            return
        self.load_seasons(kind, cand)

    @work(exclusive=True, group="add-seasons")
    async def load_seasons(self, kind: MediaKind, cand: Candidate) -> None:
        """Fetch the show's seasons and swap the list over to them.

        Its own worker group: sharing `add-search` would let the keystroke that opened this
        cancel the fetch, and sharing `add-capture` would let it cancel a write.
        """
        from release_tracker.tui.app import RdtApp

        assert isinstance(self.app, RdtApp)
        key = secret(self.app.settings.tmdb_api_key)
        if not key:
            self._status("[yellow]TMDB is not configured[/] [dim]— `rdt doctor`[/]")
            return
        seasons = self._season_memo.get(cand.canonical_id)
        if seasons is None:
            self._candidates.loading = True
            try:
                seasons = await TmdbSource().tv_seasons(
                    await self.app.http(), key, cand.canonical_id
                )
            except Exception as exc:
                self._candidates.loading = False
                self._status(f"[red]could not list seasons:[/] {exc}")
                return
            self._season_memo[cand.canonical_id] = seasons
            self._candidates.loading = False
        # A limited series has exactly one season and no choice to make. Say so rather than
        # opening a one-row picker, which reads as a broken keybinding.
        if len([x for x in seasons if not x.specials]) <= 1:
            self._status(f"[dim]“{cand.title}” has one season — the row is the whole thing[/]")
            return
        self._seasons = _SeasonList(kind, cand, seasons)
        self._show_seasons()

    def _show_seasons(self) -> None:
        """Render the season rows: the whole show first, then each season."""
        sl = self._seasons
        if sl is None:
            return
        # Specials are listed by TMDB but are not what anyone means by "season"; reachable
        # deliberately via `season:0`, not by sitting in the middle of this list.
        wanted = [x for x in sl.seasons if not x.specials]
        self._seasons = _SeasonList(sl.kind, sl.candidate, tuple(wanted))
        options = self._candidates
        options.clear_options()
        options.add_option(
            Option(
                Text.from_markup(
                    f"[bold]◂ {sl.candidate.title}[/]  [dim]the whole show · stays the default[/]"
                )
            )
        )
        for season in wanted:
            when = season.air_date.isoformat() if season.air_date else "[dim]—[/]"
            eps = f"[dim]{season.episodes} ep[/]" if season.episodes else ""
            options.add_option(
                Option(Text.from_markup(f"  [bold]Season {season.number}[/]  {when}  {eps}"))
            )
        options.highlighted = 0
        options.focus()
        self._status(
            f"[dim]{len(wanted)} seasons of[/] [bold]{sl.candidate.title}[/]"
            " [dim]· enter adds one, e reviews first, s or esc back[/]"
        )

    def _close_seasons(self) -> None:
        """Back to the search hits, exactly as they were."""
        self._seasons = None
        if self._freeform is not None and self._shown_key is not None:
            self._show(self._hits, self._shown_key, self._freeform)
        self._candidates.focus()

    # --- capturing -----------------------------------------------------------------
    @on(OptionList.OptionSelected, "#candidates")
    def _on_pick(self, event: OptionList.OptionSelected) -> None:
        if (sl := self._seasons) is not None:
            season = sl.at(event.option_index)
            self.capture(sl.kind, sl.candidate, season=season.number if season else None)
        elif 0 <= event.option_index < len(self._hits):
            kind, cand = self._hits[event.option_index]
            self.capture(kind, cand)
        elif self._freeform is not None and event.option_index == len(self._hits):
            self._review(self._freeform)

    def action_review(self) -> None:
        """Open the highlighted row as a draft instead of adding it outright.

        The same door for both kinds of row: a search hit arrives with its title and kind
        already filled in, a synthetic one with whatever the lineage gave up. Printable
        keys are swallowed while the bar has focus, so this only fires from the list.
        """
        index = self._candidates.highlighted
        if index is None:
            return
        if (sl := self._seasons) is not None:
            season = sl.at(index)
            self._review(
                drafts.for_candidate(
                    sl.candidate.title,
                    sl.kind,
                    sl.candidate,
                    season=season.number if season else None,
                    reasons=(f"season {season.number} picked from the show's season list",)
                    if season
                    else (),
                )
            )
            return
        if 0 <= index < len(self._hits):
            kind, cand = self._hits[index]
            typed = self._typed()
            self._review(
                drafts.for_candidate(
                    typed.text.strip() or cand.title,
                    kind,
                    cand,
                    season=typed.season,
                    part=typed.part,
                    part_label=typed.part_label,
                    reasons=typed.reasons,
                )
            )
        elif self._freeform is not None and index == len(self._hits):
            self._review(self._freeform)

    def _review(self, draft: Draft) -> None:
        """Hand off to the review screen and adopt whatever it decides."""

        def done(entity: Entity | None) -> None:
            if entity is not None:
                self.dismiss(entity)

        self.app.push_screen(DraftScreen(draft), done)

    @work(exclusive=True, group="add-capture")
    async def capture(self, kind: MediaKind, cand: Candidate, *, season: int | None = None) -> None:
        """Report on the *chosen* candidate then persist — no second search.

        Its own worker group. Sharing the default one with `search` meant a keystroke
        landing mid-capture cancelled it — after the report had been fetched and
        somewhere around the writes, leaving a half-enriched work behind.
        """
        from release_tracker.tui.app import RdtApp

        assert isinstance(self.app, RdtApp)
        typed = self._typed()
        # An explicit pick off the season list is an act on this screen, so it outranks
        # whatever the bar says; the bar still supplies the coord on the ordinary path.
        season = (season if season is not None else typed.season) if kind is MediaKind.TV else None
        cut = typed.part if kind is MediaKind.TV else None
        tail = _coord_tail(season, cut, typed.part_label)
        self._busy(True, f"[dim]adding “{cand.title}”{tail} — pulling dates…[/]")
        client = await self.app.http()
        try:
            report = await report_for_candidate(
                client, typed.text, kind, cand, self.app.settings, season=season
            )
            entity = await capture_work(
                self.app.db,
                self.app.settings,
                typed.text,
                report,
                season=season,
                part=typed.part if kind is MediaKind.TV else None,
                part_label=typed.part_label if kind is MediaKind.TV else None,
                client=client,
            )
        except Exception as exc:
            self._busy(False, f"[red]add failed:[/] {exc}")
            return
        if entity is None:
            self._busy(False, "[yellow]not tracked[/] — unknown kind, or no canonical id to pin")
            return
        self.dismiss(entity)

    def _busy(self, flag: bool, markup: str) -> None:
        """Show a capture as running: a spinner where the result will land, and a dead bar.

        A capture is several seconds of network — a pull, then enrichment — and the only
        sign of it was a line of grey text that read much like the search line above it.
        The spinner is the widget's own `loading`, and disabling the Input says the
        keystroke would not be read anyway (it no longer cancels the write, but it would
        queue a search behind it and land on a screen that is about to close).
        """
        self._candidates.loading = flag
        self.query_one("#add-query", Input).disabled = flag
        self._status(markup)

    # --- movement -------------------------------------------------------------------
    @property
    def _candidates(self) -> OptionList:
        return self.query_one("#candidates", OptionList)

    def action_focus_candidates(self) -> None:
        """Down out of the query bar lands on the list, as it does out of any search box.

        An empty list has nowhere to land, so the bar keeps focus rather than going dead.
        """
        options = self._candidates
        if options.option_count:
            if options.highlighted is None:
                options.highlighted = 0
            options.focus()

    def action_focus_query(self) -> None:
        self.query_one("#add-query", Input).focus()

    def action_highlight(self, delta: int) -> None:
        options = self._candidates
        if options.option_count:
            options.highlighted = max(
                0, min((options.highlighted or 0) + delta, options.option_count - 1)
            )

    def action_move_focus(self, delta: int) -> None:
        """With only a bar and a list here, either direction is the way to the other one."""
        if delta > 0:
            self.focus_next()
        else:
            self.focus_previous()

    def action_back(self) -> None:
        """Escape walks out: seasons -> candidates -> query bar, then closes the palette."""
        if self._seasons is not None:
            self._close_seasons()
            return
        bar = self.query_one("#add-query", Input)
        if self.focused is not bar:
            bar.focus()
        else:
            self.dismiss(None)
