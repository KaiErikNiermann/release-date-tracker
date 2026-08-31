"""Shared cell formatting for a TrackRow — the strings both frontends display.

Rich markup, no widgets and no Table assembly, so the CLI prints these into a
``rich.table.Table`` and the TUI puts the *same* strings into a Textual ``DataTable``.
Keeping the formatting here rather than in either frontend is what makes a row look
identical in both, and means a change to how a speculative date reads happens once.

Column *widths* and truncation stay with the caller: those are layout, and a terminal
table and a scrollable app have different room.
"""

from __future__ import annotations

from datetime import date

from release_tracker import views
from release_tracker.config import get_settings
from release_tracker.links import SourceAccess, SourceLink
from release_tracker.models import BestEstimate, ConsumptionState, DatePrecision

__all__ = [
    "fmt_cell",
    "fmt_source",
    "fmt_tag",
    "fmt_when",
    "fresh_color",
    "fresh_dot",
    "legend_dots",
    "linked",
    "provenance",
    "source_legend",
    "stance_color",
    "state_label",
    "title_cell",
    "wcw_cells",
]


def fmt_tag(tag: views.TagLine) -> str:
    return f"[dim]~{tag.name}[/]" if tag.predicted else tag.name


def fmt_when(when: date | None, precision: DatePrecision) -> str:
    """Render a date at its own precision, so the string conveys how firm it is."""
    if when is None:
        return "—"
    match precision:
        case DatePrecision.YEAR:
            return str(when.year)
        case DatePrecision.QUARTER:
            return f"{when.year} Q{(when.month - 1) // 3 + 1}"
        case DatePrecision.MONTH:
            return when.strftime("%Y-%m")
        case _:
            return when.isoformat()


def stance_color(confirmed: bool) -> str:
    """Configurable confirmed/speculative color (color-blind-safe cyan/orange by default)."""
    s = get_settings()
    return s.confirmed_color if confirmed else s.speculative_color


def fresh_color(freshness: views.Freshness) -> str:
    """Configurable freshness-dot color for the fresh/aging/stale ramp."""
    s = get_settings()
    return {"fresh": s.fresh_color, "aging": s.aging_color, "stale": s.stale_color}[freshness]


def legend_dots() -> str:
    """The fresh/aging/stale legend swatch, using the configured colors."""
    s = get_settings()
    return f"[{s.fresh_color}]●[/]fresh [{s.aging_color}]●[/]aging [{s.stale_color}]●[/]stale"


def provenance(est: BestEstimate) -> str:
    """Where a date came from, and how sure it is — as far as that means anything.

    A confidence figure on a hand-authored date is noise: the resolver recomputes every
    score from certainty and tier on read, so the number a person typed never survives to
    be displayed. What is true of such a date is that someone asserted it, and how firmly
    they hedged it — which the EDTF qualifier already said.
    """
    if est.provider == "manual":
        return f"manual · {est.certainty.value}"
    known = f"{est.provider}  " if est.provider != "unknown" else ""
    return f"{known}conf {est.confidence:.2f}"


def fmt_cell(cell: views.DateCell | None) -> str:
    """A date colored by stance (confirmed vs speculative); colors are configurable.

    A window renders as ``start-end`` (e.g. 2027-2029), preserving the ambiguity.
    """
    if cell is None or cell.when is None:
        return "[dim]—[/]"
    text = fmt_when(cell.when, cell.precision)
    if cell.end is not None:
        text = f"{text}–{fmt_when(cell.end, cell.precision)}"  # noqa: RUF001
    return f"[{stance_color(cell.confirmed)}]{text}[/]"


def fresh_dot(freshness: views.Freshness | None) -> str:
    return f"[{fresh_color(freshness)}]●[/]" if freshness else "[dim]·[/]"


def state_label(state: ConsumptionState) -> str:
    """Style a consumption state so a change to it reads at a glance, not just textually.

    Only ``dropped``/``skipped`` carry a hue (unchanged from what the watched view has
    always shown); the rest lean on dim/bold, which stays legible under any colour vision.
    """
    match state:
        case ConsumptionState.UNSET:
            return "[dim]unset[/]"
        case ConsumptionState.WATCHING:
            return "[bold]watching[/]"
        case ConsumptionState.WATCHED:
            return "[dim]watched[/]"
        case ConsumptionState.DROPPED:
            return "[red]dropped[/]"
        case ConsumptionState.SKIPPED:
            return "[yellow]skipped[/]"
        case _:
            return state.value


def title_cell(row: views.TrackRow) -> str:
    title = f"{row.title} [yellow]*[/]" if row.has_notes else row.title
    if row.blockers:  # an unsatisfied profile / unresolved blocker — flagged, never hidden
        title += f" [red]⛔[/] [dim]{row.blockers[0]}[/]"
    return title


def fmt_platform(p: views.PlatformLine, *, home: frozenset[str] = frozenset()) -> str:
    """One where-line for a card: name, the markets it is live in, and how sure we are.

    Shared by both cards so they cannot drift on what a "~" or a market list means.
    """
    mark = "[dim]~[/]" if p.predicted else ""
    where = f" [dim]({', '.join(p.regions)})[/]" if p.regions else ""
    reach = "" if p.live_in(home) else " [yellow]*[/]"
    return f"{mark}{p.name}{where}{reach}"


def wcw_cells(
    row: views.TrackRow,
    *,
    who: int = 2,
    where: int = 2,
    what: int = 4,
    home: frozenset[str] = frozenset(),
) -> tuple[str, str, str]:
    """The who/where/what trio, truncated for display (the model carries them in full).

    Platforms reachable from ``home`` sort first and the rest carry a ``*``. Truncation is
    what makes that matter: without it the two names this column has room for could both be
    markets the reader cannot get to, which reads as "available" and is not.
    """
    platforms = sorted(row.platforms, key=lambda p: not p.live_in(home))
    return (
        ", ".join(row.who[:who]) or "[dim]—[/]",
        ", ".join(p.name if p.live_in(home) else f"{p.name}[yellow]*[/]" for p in platforms[:where])
        or "[dim]—[/]",
        ", ".join(fmt_tag(t) for t in row.what[:what]) or "[dim]—[/]",
    )


def linked(text: str, url: str | None) -> str:
    """Wrap cell text in an OSC-8 terminal hyperlink (degrades to plain text if unsupported)."""
    return f"[link={url}]{text}[/link]" if url else text


def fmt_source(link: SourceLink) -> str:
    """One Sources line: a marker for whether we can refetch it, then a clickable label.

    The marker is the whole point of the section — a filled dot is something the update key
    will actually re-pull, a hollow one is a page only the user can read. The reason follows
    the hollow ones so "why can't it just fetch this" is answered in place.
    """
    auto = link.access is SourceAccess.AUTO
    marker = "[cyan]\u25cf[/]" if auto else "[dim]\u25cb[/]"
    tail = f"  [dim]{link.reason}[/]" if link.reason else ""
    # A search url is all query string and unreadable; its label already says where it goes.
    shown = "" if link.provider.startswith("search:") else _short_url(link.url)
    body = f"[bold]{link.label}[/]  [dim]{shown}[/]" if shown else f"[bold]{link.label}[/]"
    return f"  {marker} {linked(body, link.url)}{tail}"


def _short_url(url: str) -> str:
    """Drop the scheme and a leading www — the noise in front of every url."""
    for prefix in ("https://www.", "http://www.", "https://", "http://"):
        if url.startswith(prefix):
            return url[len(prefix) :]
    return url


def source_legend() -> str:
    """Fully dim, so it reads as a caption rather than another source line.

    Frontend-neutral on purpose: the TUI names its update key in the card footer, and
    the CLI has `rdt refresh` — the fact both share is which sources can be refetched
    at all.
    """
    return "[dim]\u25cf refetchable  ·  \u25cb open the link and hand-edit[/]"
