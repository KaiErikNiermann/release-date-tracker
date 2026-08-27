"""Tier-1 gap-filler: extract release claims from article text with a cheap LLM.

Only runs for what Tier 0 leaves as TBA (unannounced digital dates, tech hardware,
rumors). Two cost levers keep this near-free:

1. **Pre-filter locally** (caller's job, see :func:`candidate_snippets`): strip
   boilerplate and keep only sentences that mention both a date token and the
   entity, so the model sees kilobytes, not megabytes.
2. **Batch + structured output**: submit as an OpenAI Batch (50% cheaper, async)
   and force a JSON schema so responses are typed — no hand-parsing.

This module is the plumbing; the search/scrape front-end that produces snippets is
a later tier. The structured-output contract below is the stable interface.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

from openai import OpenAI
from pydantic import BaseModel, Field

from release_tracker.clock import utc_now
from release_tracker.config import Settings, secret
from release_tracker.logging import get_logger
from release_tracker.models import (
    Certainty,
    DatePrecision,
    Money,
    ReleaseChannel,
    ReleaseObservation,
    SourceTier,
)

log = get_logger("extract")

_DATE_HINT = re.compile(
    r"\b(\d{4}|Q[1-4]|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|"
    r"spring|summer|fall|autumn|winter|holiday|release|launch|out on|available)\b",
    re.IGNORECASE,
)

SYSTEM_PROMPT = (
    "You extract concrete release-date claims from article text about movies, TV, "
    "games and tech. Only report claims actually supported by the text. For each "
    "claim, classify the channel (theatrical/digital/physical/streaming/steam/psn/"
    "xbox/nintendo_eshop/retail/amazon/best_buy/preorder/...), the region (ISO "
    "3166-1 alpha-2, or WW), the date with its precision, and the stance "
    "(confirmed/rumored/leaked/estimated/delayed). Quote the exact sentence that "
    "backs each claim. If there is no usable date claim, return an empty list."
)


# --- structured-output contract -------------------------------------------
class ExtractedClaim(BaseModel):
    """One claim the model is allowed to emit. Mirrors ReleaseObservation fields."""

    channel: ReleaseChannel
    region: str = Field(description="ISO 3166-1 alpha-2 country code, or 'WW'")
    release_date: str | None = Field(description="ISO date YYYY-MM-DD if known, else null")
    precision: DatePrecision
    price_amount_minor: int | None = Field(description="price in minor units (cents), or null")
    price_currency: str | None = Field(description="ISO 4217 currency, or null")
    certainty: Certainty
    source_quote: str = Field(description="exact sentence from the text backing this claim")


class ExtractionResult(BaseModel):
    claims: list[ExtractedClaim]


def candidate_snippets(text: str, entity_title: str) -> list[str]:
    """Local pre-filter: keep only sentences mentioning the entity and a date hint."""
    title_tokens = {t for t in re.split(r"\W+", entity_title.lower()) if len(t) > 2}
    out: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        low = sentence.lower()
        if not _DATE_HINT.search(sentence):
            continue
        if title_tokens and not any(tok in low for tok in title_tokens):
            continue
        out.append(sentence.strip())
    return out


# --- batch plumbing -------------------------------------------------------
def strict_schema() -> dict[str, Any]:
    """JSON schema for ExtractionResult, made OpenAI-strict (all required, closed)."""
    schema = ExtractionResult.model_json_schema()
    _make_strict(schema)
    return schema


def _make_strict(node: object) -> None:
    """Recursively enforce additionalProperties=false and required=all keys."""
    if isinstance(node, dict):
        obj = cast("dict[str, Any]", node)
        if obj.get("type") == "object":
            obj["additionalProperties"] = False
            props = obj.get("properties")
            if isinstance(props, dict):
                obj["required"] = list(cast("dict[str, Any]", props).keys())
        for value in obj.values():
            _make_strict(value)
    elif isinstance(node, list):
        for item in cast("list[Any]", node):
            _make_strict(item)


@dataclass(slots=True)
class ExtractionTask:
    entity_id: str
    entity_title: str
    snippets: list[str]
    source_url: str
    source_name: str
    source_tier: SourceTier
    published_at: date | None


def write_batch_jsonl(tasks: list[ExtractionTask], path: Path, model: str) -> int:
    """Serialise extraction tasks to an OpenAI Batch input file. Returns line count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", encoding="utf-8") as fh:
        for task in tasks:
            if not task.snippets:
                continue
            user = f"Entity: {task.entity_title}\n\nText:\n" + "\n".join(task.snippets)
            line = {
                "custom_id": task.entity_id,
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user},
                    ],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "extraction_result",
                            "strict": True,
                            "schema": strict_schema(),
                        },
                    },
                },
            }
            fh.write(json.dumps(line) + "\n")
            written += 1
    log.info("extract.batch.written", tasks=written, path=str(path))
    return written


def _openai(settings: Settings) -> OpenAI:
    """An OpenAI client, or a refusal that names the setting.

    Everywhere else a missing key degrades to an empty result; batch extraction genuinely
    cannot proceed without one, so it stops — but with a sentence that says what to set
    rather than the SDK's own error, which does not know what `rdt` calls this.
    """
    key = secret(settings.openai_api_key)
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set — try `rdt config set OPENAI_API_KEY …`")
    return OpenAI(api_key=key)


def submit_batch(path: Path, settings: Settings) -> str:
    client = _openai(settings)
    upload = client.files.create(file=path.open("rb"), purpose="batch")
    batch = client.batches.create(
        input_file_id=upload.id,
        endpoint="/v1/chat/completions",
        completion_window="24h",
    )
    log.info("extract.batch.submitted", batch_id=batch.id, input_file=upload.id)
    return batch.id


def collect_batch(
    batch_id: str, tasks_by_id: dict[str, ExtractionTask], settings: Settings
) -> list[ReleaseObservation]:
    """Poll a finished batch and map structured outputs to observations."""
    client = _openai(settings)
    batch = client.batches.retrieve(batch_id)
    if batch.status != "completed" or batch.output_file_id is None:
        log.info("extract.batch.pending", batch_id=batch_id, status=batch.status)
        return []
    content = client.files.content(batch.output_file_id).text
    now = utc_now()
    observations: list[ReleaseObservation] = []
    for line in content.splitlines():
        if not line.strip():
            continue
        record = cast("dict[str, Any]", json.loads(line))
        custom_id = str(record.get("custom_id"))
        task = tasks_by_id.get(custom_id)
        if task is None:
            continue
        body = record.get("response", {}).get("body", {})
        choices = body.get("choices", [])
        if not choices:
            continue
        parsed = ExtractionResult.model_validate_json(choices[0]["message"]["content"])
        observations.extend(_claim_to_obs(c, task, now) for c in parsed.claims)
    log.info("extract.batch.collected", batch_id=batch_id, observations=len(observations))
    return observations


def _claim_to_obs(claim: ExtractedClaim, task: ExtractionTask, now: datetime) -> ReleaseObservation:
    price = (
        Money(amount_minor=claim.price_amount_minor, currency=claim.price_currency)
        if claim.price_amount_minor is not None and claim.price_currency
        else None
    )
    return ReleaseObservation(
        entity_id=task.entity_id,
        channel=claim.channel,
        region=claim.region or "WW",
        release_date=_safe_date(claim.release_date),
        precision=claim.precision,
        price=price,
        certainty=claim.certainty,
        source_tier=task.source_tier,
        provider="openai",
        source_name=task.source_name,
        source_url=task.source_url,
        source_quote=claim.source_quote,
        published_at=task.published_at,
        fetched_at=now,
    )


def _safe_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
