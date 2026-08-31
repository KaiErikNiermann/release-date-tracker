"""Tests for reading a season's release blocks off its episode air dates.

No canonical source models an intra-season slice — TMDB carries every one of the shows below
as a plain season named "Season N" — so the air dates are the only sourced signal there is.
Every fixture here is the real broadcast schedule, trimmed to the dates the detector reads.

The two known false positives are asserted as *firing*, not skipped. They are the cost of
catching a weekly show's mid-season break, and a future tuning change that silently kills
them should fail here rather than quietly narrow what the feature finds.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from release_tracker.slices import Episode, scan_slices


def _numbered(days: list[date]) -> list[Episode]:
    """Episodes numbered 1..n over a run of air dates."""
    return [Episode(n, day) for n, day in enumerate(days, start=1)]


def _binge(*blocks: tuple[str, int]) -> list[Episode]:
    """A season released in all-at-once blocks: (drop date, episode count)."""
    return _numbered([date.fromisoformat(w) for w, count in blocks for _ in range(count)])


def _weekly(*blocks: tuple[str, int]) -> list[Episode]:
    """A season released weekly within each block: (block start, episode count)."""
    return _numbered(
        [date.fromisoformat(w) + timedelta(weeks=i) for w, count in blocks for i in range(count)]
    )


def _sizes(episodes: list[Episode]) -> tuple[int, ...]:
    return tuple(p.episodes for p in scan_slices(episodes).proposals)


# --- marketed splits, which must be found ---------------------------------------------------
def test_a_binge_season_dropped_in_three_blocks() -> None:
    """Cobra Kai S6 — 5+5+5 across July, November and February."""
    assert _sizes(_binge(("2024-07-18", 5), ("2024-11-15", 5), ("2025-02-13", 5))) == (5, 5, 5)


def test_a_binge_season_dropped_in_two_volumes() -> None:
    """Stranger Things S4 — a 35-day gap on an otherwise 0-day cadence."""
    assert _sizes(_binge(("2022-05-27", 7), ("2022-07-01", 2))) == (7, 2)


def test_a_weekly_season_with_a_mid_season_break() -> None:
    """Better Call Saul S6 — 49 days against a 7-day beat, which no absolute floor would see
    as different from Grey's Anatomy's ordinary three-week pause."""
    assert _sizes(_weekly(("2022-04-18", 7), ("2022-07-11", 6))) == (7, 6)


def test_a_very_long_break_is_still_one_cut() -> None:
    """Attack on Titan's Final Season pauses 287 days between its parts."""
    assert _sizes(_weekly(("2020-12-07", 16), ("2022-01-10", 12))) == (16, 12)


# --- non-splits, which must not be invented --------------------------------------------------
@pytest.mark.parametrize(
    "episodes",
    [
        pytest.param(_weekly(("2025-03-04", 9)), id="plain weekly run"),
        pytest.param(_weekly(("1998-09-24", 23)), id="long weekly run"),
        pytest.param(_binge(("2024-05-11", 8)), id="single binge drop"),
    ],
)
def test_an_unsplit_season_reports_one_block(episodes: list[Episode]) -> None:
    """One proposal is a real answer — "we looked and there is no split" — not an empty one."""
    scan = scan_slices(episodes)
    assert not scan.split
    assert len(scan.proposals) == 1
    assert scan.proposals[0].episodes == len(episodes)


def test_an_ordinary_three_week_pause_is_not_a_split() -> None:
    """Grey's Anatomy S20's break sits exactly on the floor and must not clear it."""
    assert not scan_slices(_weekly(("2024-03-14", 5), ("2024-04-04", 5))).split


# --- the known false positives, asserted rather than hidden ----------------------------------
def test_a_network_winter_hiatus_still_reads_as_a_split() -> None:
    """The Flash S8: an 85-day break over New Year. Suppressing this would also kill Cobra
    Kai S6, so the caveat is surfaced instead — a wrong proposal costs one keystroke."""
    scan = scan_slices(_weekly(("2021-11-16", 5), ("2022-03-09", 15)))
    assert scan.split
    assert any("network hiatus" in r for r in scan.reasons)


def test_the_hiatus_caveat_stays_off_a_genuinely_long_split() -> None:
    """287 days is not a scheduling gap, whatever month it crosses."""
    scan = scan_slices(_weekly(("2020-12-07", 16), ("2022-01-10", 12)))
    assert scan.split
    assert not any("network hiatus" in r for r in scan.reasons)


# --- the near miss, which is how a real block gets missed -------------------------------------
def test_a_block_below_the_bar_is_named_rather_than_dropped() -> None:
    """Stranger Things S5 is really three blocks — Vol 1, Vol 2, then the finale six days
    later. Six days clears no sane bar, so the detector finds two and *says* what it nearly
    found, turning a silent under-count into something fixable in one keystroke."""
    scan = scan_slices(_binge(("2025-11-26", 4), ("2025-12-25", 3), ("2025-12-31", 1)))
    assert len(scan.proposals) == 2
    assert any("6d before ep 8" in r for r in scan.reasons)


def test_a_run_with_nothing_notable_says_nothing_notable() -> None:
    assert not any("nearest gap" in r for r in scan_slices(_weekly(("2025-03-04", 9))).reasons)


# --- shape and degenerate input ---------------------------------------------------------------
def test_the_cadence_and_bar_are_reported() -> None:
    """The reasons are printed verbatim by the review surfaces, so they carry the working."""
    scan = scan_slices(_weekly(("2022-04-18", 7), ("2022-07-11", 6)))
    assert scan.cadence_days == 7
    assert scan.threshold_days == 21
    assert scan.reasons[0] == "cadence 7d (median of 12 gaps)"


def test_proposals_carry_their_episode_bounds_and_gap() -> None:
    first, second = scan_slices(_binge(("2022-05-27", 7), ("2022-07-01", 2))).proposals
    assert (first.first_episode, first.last_episode, first.gap_before_days) == (1, 7, None)
    assert (second.first_episode, second.last_episode, second.gap_before_days) == (8, 9, 35)
    assert second.starts == date(2022, 7, 1)


@pytest.mark.parametrize("count", [0, 1, 3])
def test_too_few_episodes_yields_no_proposals_and_says_so(count: int) -> None:
    """Distinct from "no split": there was nothing to read a cadence from."""
    scan = scan_slices(_weekly(("2025-01-01", count)))
    assert scan.proposals == ()
    assert scan.cadence_days is None
    assert "too few" in scan.reasons[0]


def test_episodes_out_of_order_are_sorted_before_scanning() -> None:
    episodes = _binge(("2022-05-27", 7), ("2022-07-01", 2))
    assert _sizes(list(reversed(episodes))) == (7, 2)
