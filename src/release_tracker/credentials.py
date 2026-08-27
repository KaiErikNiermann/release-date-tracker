"""Check a credential against the service that issued it, cheaply.

A key is only ever wrong in two ways — mistyped, or the wrong one of the two things the
provider hands you — and both are invisible until a search quietly returns nothing hours
later. One authenticated request at the moment of pasting turns that into an immediate
answer.

Checking never gates saving. A provider outage, a rate limit or an aeroplane would
otherwise stop someone configuring a key they know is good, which is a worse failure than
storing a typo they can correct.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import httpx

from release_tracker.logging import get_logger
from release_tracker.sources.base import get_json, post_json

log = get_logger("credentials")

__all__ = ["CheckResult", "check_tmdb", "check_twitch"]

_TMDB_CONFIGURATION = "https://api.themoviedb.org/3/configuration"
_TWITCH_TOKEN = "https://id.twitch.tv/oauth2/token"  # noqa: S105 - public OAuth endpoint


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Whether a credential worked, and what to say if it didn't."""

    ok: bool
    detail: str

    @classmethod
    def good(cls, detail: str = "works") -> CheckResult:
        return cls(ok=True, detail=detail)


async def check_tmdb(client: httpx.AsyncClient, key: str) -> CheckResult:
    """One call to ``/configuration`` — the cheapest authenticated endpoint TMDB has.

    The likeliest mistake is not a typo but pasting the wrong credential: the dashboard
    offers an "API Key (v3 auth)" and an "API Read Access Token (v4 auth)", and this
    project authenticates with the v3 key as a query parameter, so the v4 JWT fails with a
    perfectly ordinary 401. Naming that case is most of the value here.
    """
    if not key.strip():
        return CheckResult(ok=False, detail="empty")
    try:
        payload = cast(
            "dict[str, Any]", await get_json(client, _TMDB_CONFIGURATION, params={"api_key": key})
        )
    except httpx.HTTPStatusError as exc:
        return CheckResult(ok=False, detail=f"TMDB said {exc.response.status_code}")
    except Exception as exc:  # offline, DNS, timeout — not the key's fault
        log.warning("credentials.tmdb_check_failed", error=str(exc))
        return CheckResult(ok=False, detail="could not reach TMDB")
    if "images" in payload:
        return CheckResult.good()
    if key.startswith("ey"):
        # A JWT, i.e. the v4 Read Access Token from the same dashboard page.
        return CheckResult(ok=False, detail="that looks like the v4 token — copy the v3 API key")
    return CheckResult(ok=False, detail="rejected")


async def check_twitch(
    client: httpx.AsyncClient, client_id: str, client_secret: str
) -> CheckResult:
    """Exchange the pair for an app token, which is exactly what the IGDB source does.

    A Twitch application only issues a secret when its client type is *Confidential*, so
    the common failure is a well-formed id with no usable secret behind it.
    """
    if not client_id.strip() or not client_secret.strip():
        return CheckResult(ok=False, detail="both an id and a secret are needed")
    try:
        payload = cast(
            "dict[str, Any]",
            await post_json(
                client,
                _TWITCH_TOKEN,
                params={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "client_credentials",
                },
            ),
        )
    except Exception as exc:
        log.warning("credentials.twitch_check_failed", error=str(exc))
        return CheckResult(ok=False, detail="could not reach Twitch")
    if isinstance(payload.get("access_token"), str):
        return CheckResult.good()
    # Twitch answers a bad pair with 400 and a JSON body rather than an error status.
    message = str(payload.get("message") or "rejected")
    return CheckResult(ok=False, detail=f"Twitch said: {message}")
