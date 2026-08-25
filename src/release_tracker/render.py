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
from release_tracker.models import DatePrecision

__all__ = [
    "fmt_cell",
    "fmt_tag",
    "fmt_when",
    "fresh_color",
    "fresh_dot",
    "legend_dots",
    "stance_color",
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


def title_cell(row: views.TrackRow) -> str:
    title = f"{row.title} [yellow]*[/]" if row.has_notes else row.title
    if row.blockers:  # an unsatisfied profile / unresolved blocker — flagged, never hidden
        title += f" [red]⛔[/] [dim]{row.blockers[0]}[/]"
    return title


def wcw_cells(
    row: views.TrackRow, *, who: int = 2, where: int = 2, what: int = 4
) -> tuple[str, str, str]:
    """The who/where/what trio, truncated for display (the model carries them in full)."""
    return (
        ", ".join(row.who[:who]) or "[dim]—[/]",
        ", ".join(row.where[:where]) or "[dim]—[/]",
        ", ".join(fmt_tag(t) for t in row.what[:what]) or "[dim]—[/]",
    )
