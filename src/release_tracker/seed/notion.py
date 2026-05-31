"""Live Notion seed provider.

Generic, committable: it reads *your* database via *your* integration token (set
``NOTION_TOKEN`` and ``NOTION_DATABASE_ID`` in ``.env``). Nothing personal is
hardcoded. Maps the database's ``Type`` select to ``MediaKind`` and turns any
hand-authored ``Release Date`` + ``RD Certainty`` into a low-tier observation, so
your manual curation becomes just another (sourced) signal.

Create an internal integration and share the database with it:
https://www.notion.so/my-integrations
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, cast

import httpx

from release_tracker.config import Settings
from release_tracker.logging import get_logger
from release_tracker.models import (
    Certainty,
    DatePrecision,
    Entity,
    ReleaseChannel,
    ReleaseObservation,
    SourceTier,
)
from release_tracker.seed.base import SeedBundle, parse_kind

log = get_logger("seed.notion")

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Notion "RD Certainty" status -> (certainty, precision). Workflow-only statuses
# (Not started / In progress / Done) carry no date semantics and are skipped.
_CERTAINTY_MAP: dict[str, tuple[Certainty, DatePrecision]] = {
    "Exact": (Certainty.CONFIRMED, DatePrecision.EXACT),
    "Confirmed": (Certainty.CONFIRMED, DatePrecision.EXACT),
    "Approximate": (Certainty.ESTIMATED, DatePrecision.MONTH),
    "TBA": (Certainty.ESTIMATED, DatePrecision.TBA),
}


class NotionSeed:
    name = "notion"

    def load(self, settings: Settings) -> SeedBundle:
        token, db_id = settings.notion_token, settings.notion_database_id
        if not token or not db_id:
            log.warning("seed.notion.skip", reason="no NOTION_TOKEN/NOTION_DATABASE_ID")
            return SeedBundle()

        headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        bundle = SeedBundle()
        cursor: str | None = None
        now = datetime.now(UTC)
        with httpx.Client(timeout=30.0, headers=headers) as client:
            while True:
                body: dict[str, Any] = {"page_size": 100}
                if cursor:
                    body["start_cursor"] = cursor
                resp = client.post(f"{API}/databases/{db_id}/query", json=body)
                resp.raise_for_status()
                payload = cast("dict[str, Any]", resp.json())
                for page in cast("list[dict[str, Any]]", payload.get("results", [])):
                    self._consume_page(page, bundle, now)
                if not payload.get("has_more"):
                    break
                cursor = cast("str | None", payload.get("next_cursor"))

        log.info(
            "seed.notion.loaded",
            entities=len(bundle.entities),
            observations=len(bundle.observations),
        )
        return bundle

    def _consume_page(self, page: dict[str, Any], bundle: SeedBundle, now: datetime) -> None:
        props = cast("dict[str, Any]", page.get("properties", {}))
        title = _title(props.get("Name"))
        if not title:
            return
        type_name = _select(props.get("Type"))
        kind = parse_kind(type_name or "other")
        entity = Entity.create(
            title=title,
            kind=kind,
            notes=_rich_text(props.get("Notes")) or None,
            notion_page_id=str(page.get("id")),
        )
        bundle.entities.append(entity)

        rel = _date(props.get("Release Date"))
        certainty_name = _status(props.get("RD Certainty"))
        if rel is not None and certainty_name in _CERTAINTY_MAP:
            certainty, precision = _CERTAINTY_MAP[certainty_name]
            bundle.observations.append(
                ReleaseObservation(
                    entity_id=entity.id,
                    channel=ReleaseChannel.PRIMARY,
                    region="WW",
                    release_date=rel,
                    precision=precision,
                    certainty=certainty,
                    source_tier=SourceTier.RUMOR,  # hand-authored: real but unverified
                    provider=self.name,
                    source_name="Notion (manual)",
                    source_url=str(page.get("url")) or None,
                    fetched_at=now,
                )
            )


# --- Notion property extractors (defensive against missing/empty values) ---
def _as_dict(prop: object) -> dict[str, Any] | None:
    return cast("dict[str, Any]", prop) if isinstance(prop, dict) else None


def _rich_join(prop: object, key: str) -> str:
    obj = _as_dict(prop)
    if obj is None:
        return ""
    runs = cast("list[dict[str, Any]]", obj.get(key, []))
    return "".join(str(run.get("plain_text", "")) for run in runs).strip()


def _title(prop: object) -> str | None:
    return _rich_join(prop, "title") or None


def _rich_text(prop: object) -> str:
    return _rich_join(prop, "rich_text")


def _named(prop: object, key: str) -> str | None:
    obj = _as_dict(prop)
    inner = _as_dict(obj.get(key)) if obj else None
    return str(inner["name"]) if inner and inner.get("name") else None


def _select(prop: object) -> str | None:
    return _named(prop, "select")


def _status(prop: object) -> str | None:
    return _named(prop, "status")


def _date(prop: object) -> date | None:
    obj = _as_dict(prop)
    inner = _as_dict(obj.get("date")) if obj else None
    if inner is None or not inner.get("start"):
        return None
    try:
        return date.fromisoformat(str(inner["start"])[:10])
    except ValueError:
        return None
