"""Contingency resolution: "available *to me*" = the max over my required contingencies.

A work is available to the user only when ALL their required contingency dimensions
(country, platform, OS, language, + custom) resolve. Each contingency is three-valued
(`resolved@date | pending | never`); the effective availability is the **max** over them
(AND across dimensions), while within a dimension the user accepts any of several values
(OR). A ``never`` propagates (the "if, not when" case — e.g. a Windows-only anticheat for a
Linux user); a ``pending`` means "at best pending."

Pure module — no DB, no terminal — like ``resolve.py`` and ``dates_edtf.py``.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date

from release_tracker.config import Settings
from release_tracker.models import BestEstimate, Certainty


class ContingencyDimension(enum.StrEnum):
    """Built-in availability dimensions. Custom dimensions are free-string keys."""

    REGION = "region"
    PLATFORM = "platform"  # hardware: ps5 / switch / pc ...
    OS = "os"  # software: windows / linux / macos / proton
    LANGUAGE = "language"  # audio/subs: jp native -> later sub/dub
    TECH = "tech"  # a required capability: ray_tracing, a specific engine feature ...


class ResolutionStatus(enum.StrEnum):
    RESOLVED = "resolved"  # available at ``when``
    PENDING = "pending"  # awaited, no firm date yet (the "if, not when" middle)
    NEVER = "never"  # can never satisfy this profile (a hard blocker)


# the certainty stances that count as a firm, gating date (mirror resolve._CONFIRMED)
_CONFIRMED: frozenset[Certainty] = frozenset({Certainty.CONFIRMED, Certainty.DELAYED})


@dataclass(frozen=True, slots=True)
class Resolution:
    """Three-valued availability of one contingency — or of the combined whole."""

    status: ResolutionStatus
    when: date | None = None  # set iff RESOLVED
    blocker: str | None = None  # human label of the governing pending/never cause

    @property
    def is_resolved(self) -> bool:
        return self.status is ResolutionStatus.RESOLVED


@dataclass(frozen=True, slots=True)
class ProfileMatcher:
    """The user's required contingencies: dimension -> accepted values (OR within a dim).

    An empty matcher (no constrained dims) accepts everything — so until the user opts in,
    availability is unchanged.
    """

    accepted: Mapping[str, frozenset[str]]

    def matches(self, facets: Mapping[str, str]) -> bool:
        """AND across my constrained dims; a facet absent on the observation is a wildcard."""
        for dim, value in facets.items():
            allowed = self.accepted.get(dim)
            if allowed is not None and value not in allowed:
                return False
        return True


def estimate_resolution(est: BestEstimate | None) -> Resolution:
    """Lift a chosen (already profile-matched) estimate into a Resolution.

    None (nothing matched my profile) or a speculative date -> PENDING; a confirmed dated
    estimate -> RESOLVED. Facets alone never yield NEVER (that's reserved for conditions).
    """
    if est is None:
        return Resolution(ResolutionStatus.PENDING, blocker="not released for your profile")
    if est.release_date is not None and est.certainty in _CONFIRMED:
        return Resolution(ResolutionStatus.RESOLVED, est.release_date)
    return Resolution(ResolutionStatus.PENDING, blocker="no confirmed date for your profile")


def combine(parts: Iterable[Resolution]) -> Resolution:
    """AND across dimensions/conditions: ``never`` ≻ ``pending`` ≻ ``max(resolved dates)``."""
    items = list(parts)
    nevers = [p for p in items if p.status is ResolutionStatus.NEVER]
    if nevers:
        return nevers[0]
    pendings = [p for p in items if p.status is ResolutionStatus.PENDING]
    if pendings:
        return Resolution(ResolutionStatus.PENDING, blocker=pendings[0].blocker)
    dates = [p.when for p in items if p.when is not None]
    if dates:
        return Resolution(ResolutionStatus.RESOLVED, max(dates))
    return Resolution(ResolutionStatus.PENDING)  # nothing to go on


# "region doesn't gate me" (a VPN). Public because tech has to read *past* it:
# a stream can be region-hopped, a device cannot.
REGION_WILDCARD: frozenset[str] = frozenset({"ANY", "*"})


def matcher_from_settings(settings: Settings) -> ProfileMatcher:
    """Build the user's profile matcher; an empty set for a dim => unconstrained (wildcard).

    Region is otherwise privileged — every observation carries a region facet and the matcher
    always folds in ``WW``, so region can't go wildcard via the empty-set path the other dims
    use. The ``ANY``/``*`` sentinel in ``RDT_REGIONS`` is the deliberate escape: it drops region
    entirely (the "I bypass region, e.g. via VPN, so it's inapplicable to me" case).
    """
    region = (
        frozenset[str]()
        if REGION_WILDCARD & frozenset(settings.regions)
        else frozenset({*settings.regions, "WW"})
    )
    accepted: dict[str, frozenset[str]] = {
        ContingencyDimension.REGION.value: region,
        ContingencyDimension.PLATFORM.value: frozenset(settings.platforms),
        ContingencyDimension.OS.value: frozenset(settings.os_targets),
        ContingencyDimension.LANGUAGE.value: frozenset(settings.languages),
        ContingencyDimension.TECH.value: frozenset(settings.tech_available),
        **{dim: frozenset(values) for dim, values in settings.custom_contingencies.items()},
    }
    # an empty accepted-set means "I don't constrain this dim" -> drop it so it's a wildcard
    return ProfileMatcher({dim: vals for dim, vals in accepted.items() if vals})
