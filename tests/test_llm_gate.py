"""Themes are optional, so a call that cannot succeed must not be paid for on every add."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from release_tracker import enrich as enrich_mod
from release_tracker.config import Settings
from release_tracker.models import Entity, MediaKind
from release_tracker.sources.base import MediaGraph


# --- LLM themes: a standing billing/auth failure is not paid for twice -----------------------
def _api_error(code: str, err_type: str = "insufficient_quota") -> Exception:
    """A 429 shaped like OpenAI's. `code`/`type` are the two fields the gate reads.

    Built with ``httpx2``, not our own ``httpx``. The two coexist: openai 3.x moved to
    httpx 2.x, which ships under a *new distribution name* rather than as an upgrade, so
    ``APIStatusError.__init__`` is annotated against a ``Response`` class that is not the
    one our client uses. It is duck-typed at runtime and both work, but naming the wrong
    one here would be a type error over a response nothing reads — the gate looks only at
    the body. ``httpx`` stays right below for ``ConnectError``, which really is ours.
    """
    import httpx2
    from openai import RateLimitError

    request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
    return RateLimitError(
        "429",
        response=httpx2.Response(429, request=request),
        body={"message": "rate limited", "type": err_type, "code": code},
    )


@pytest.mark.parametrize("code", ["credit_balance_exhausted", "insufficient_quota"])
def test_standing_failure_recognises_a_billing_stop(code: str) -> None:
    assert enrich_mod.standing_llm_failure(_api_error(code)) is not None


def test_standing_failure_lets_a_transient_error_through() -> None:
    # a plain rate-limit blip is worth retrying — it must NOT latch themes off
    blip = _api_error("rate_limit_exceeded", err_type="requests")
    assert enrich_mod.standing_llm_failure(blip) is None
    assert enrich_mod.standing_llm_failure(httpx.ConnectError("boom")) is None


async def test_themes_stop_being_attempted_after_a_billing_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class _Fake:
        def __init__(self, **_: object) -> None:
            pass

        @property
        def beta(self) -> Any:
            return self

        @property
        def chat(self) -> Any:
            return self

        @property
        def completions(self) -> Any:
            return self

        async def parse(self, **_: object) -> object:
            nonlocal calls
            calls += 1
            raise _api_error("credit_balance_exhausted")

    monkeypatch.setattr(enrich_mod, "AsyncOpenAI", _Fake)
    settings = Settings().model_copy(update={"openai_api_key": SecretStr("sk-test")})
    entity = Entity.create("A Film", MediaKind.MOVIE)
    graph = MediaGraph(credits=(), genres=("Drama",), summary="A film.")

    assert await enrich_mod.llm_themes(settings, entity, graph) == ()
    assert await enrich_mod.llm_themes(settings, entity, graph) == ()
    assert calls == 1, "the second add must not pay for a call that cannot succeed"

    enrich_mod.reset_theme_gate()
    assert await enrich_mod.llm_themes(settings, entity, graph) == ()
    assert calls == 2, "resetting the gate re-arms it (new key / new credits)"
