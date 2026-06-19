"""One-off: flag likely-wrong canonical matches after a full pull.

The Notion seed carries a hand-authored date (provider="notion"); the Tier-0
sources carry resolved dates (tmdb/igdb). When the two disagree by more than a
year, the title probably matched the wrong canonical record — the exact ambiguity
manual resolution exists to fix. Also flags entities that never resolved.

Run: poetry run python scripts/audit_resolution.py
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from release_tracker import matching
from release_tracker.config import get_settings
from release_tracker.db import Database
from release_tracker.models import Entity


@dataclass(slots=True)
class Finding:
    entity: Entity
    flag: str
    detail: str


def _years(dates: list[int]) -> str:
    return ",".join(str(y) for y in sorted(set(dates))) or "—"


def audit(db: Database) -> list[Finding]:
    findings: list[Finding] = []
    for entity in db.iter_entities():
        obs = list(db.iter_observations(entity.id))
        notion_years = [
            o.release_date.year for o in obs if o.provider == "notion" and o.release_date
        ]
        source_by_provider: dict[str, list[int]] = defaultdict(list)
        for o in obs:
            if o.provider in ("tmdb", "igdb") and o.release_date:
                source_by_provider[o.provider].append(o.release_date.year)
        source_years = [y for ys in source_by_provider.values() for y in ys]

        resolvable = matching.is_resolvable(entity.kind)
        resolved = not matching.needs_resolution(entity)

        if resolvable and not resolved:
            findings.append(Finding(entity, "UNRESOLVED", "no canonical id pinned"))
            continue
        if resolved and not source_years:
            findings.append(
                Finding(
                    entity,
                    "NO_SOURCE_DATE",
                    f"notion={_years(notion_years)} but source has no date",
                )
            )
            continue
        if notion_years and source_years:
            gap = min(abs(n - s) for n in notion_years for s in source_years)
            if gap > 1:
                findings.append(
                    Finding(
                        entity,
                        "YEAR_MISMATCH",
                        f"notion={_years(notion_years)} vs "
                        f"source={_years(source_years)} (gap {gap}y)",
                    )
                )
    return findings


def main() -> None:
    db = Database(get_settings().db_path)
    total = sum(1 for _ in db.iter_entities())
    findings = audit(db)
    by_flag: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        by_flag[f.flag].append(f)
    print(f"Audited {total} entities — {len(findings)} flagged\n")
    for flag in ("YEAR_MISMATCH", "UNRESOLVED", "NO_SOURCE_DATE"):
        group = by_flag.get(flag, [])
        if not group:
            continue
        print(f"## {flag} ({len(group)})")
        for f in sorted(group, key=lambda x: x.entity.title):
            ids = ",".join(f"{k}={v}" for k, v in f.entity.external_ids.items()) or "no-id"
            print(f"  [{f.entity.kind.value:5}] {f.entity.title:<38} {f.detail}   <{ids}>")
        print()


if __name__ == "__main__":
    main()
