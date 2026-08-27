"""Credentials are redacted in the program and intact on the wire.

`SecretStr` stops a key leaking through a repr or a traceback, but it introduces the
opposite failure: an unwrapped `SecretStr` interpolated into an f-string renders as
`**********`, which type-checks fine and sends a broken header. The Notion seed builds its
Authorization header exactly that way, so the outbound value is asserted here rather than
trusted.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from release_tracker.config import Settings, get_settings, secret


def _with(**values: str) -> Settings:
    return get_settings().model_copy(update={k: SecretStr(v) for k, v in values.items()})


def test_a_credential_does_not_appear_in_a_repr() -> None:
    """The point of the type. A stray `print(settings)` or an exception repr used to carry
    every key in the program."""
    text = repr(_with(tmdb_api_key="super-secret-value"))
    assert "super-secret-value" not in text


def test_unwrapping_returns_the_real_value() -> None:
    assert secret(_with(tmdb_api_key="abc").tmdb_api_key) == "abc"
    assert secret(None) is None


def test_an_empty_credential_is_still_falsy() -> None:
    """Every guard in the codebase is `if not settings.<key>`, so the conversion had to
    leave that meaning untouched."""
    assert not _with(tmdb_api_key="").tmdb_api_key


@pytest.mark.parametrize(
    ("field", "value"),
    [("notion_token", "notion-abc"), ("tmdb_api_key", "tmdb-abc")],
)
def test_a_credential_is_not_masked_on_its_way_into_a_request(field: str, value: str) -> None:
    """The failure `SecretStr` invites: `f"Bearer {token}"` on a wrapped value sends
    `Bearer **********` and no type checker objects."""
    settings = _with(**{field: value})
    unwrapped = secret(getattr(settings, field))
    assert unwrapped == value
    assert "*" not in f"Bearer {unwrapped}"
