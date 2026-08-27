"""Structured logging via structlog. JSON in non-tty, pretty console in a tty."""

from __future__ import annotations

import logging
import sys
from typing import TextIO, cast

import structlog


class _LateStderr:
    """A stand-in for ``sys.stderr`` that resolves it on every write.

    ``PrintLoggerFactory`` holds whatever object it is given, and with
    ``cache_logger_on_first_use`` that reference outlives the call that set it. Anything
    that swaps ``sys.stderr`` for a buffer it later closes — Click's ``CliRunner``, a
    ``capsys`` teardown — therefore leaves every subsequent log write raising
    ``ValueError: I/O operation on closed file`` from an unrelated part of the program.

    Binding late costs nothing here because the default sink *is* whatever stderr happens
    to be. A caller that owns the terminal still passes its own stream explicitly, and that
    one is held, which is the behaviour ``tui.app.run`` depends on.
    """

    def write(self, message: str) -> int:
        return sys.stderr.write(message)

    def flush(self) -> None:
        sys.stderr.flush()

    def isatty(self) -> bool:
        return sys.stderr.isatty()


def configure_logging(
    *,
    level: int = logging.INFO,
    json_logs: bool | None = None,
    stream: TextIO | None = None,
) -> None:
    """Point structlog at ``stream``, defaulting to stderr.

    The sink is bound *here*, not resolved per write, so a caller that owns the terminal
    has to pass its own: ``contextlib.redirect_stderr`` (what Textual wraps a running app
    in) rebinds ``sys.stderr``, but a logger configured beforehand still holds the real
    one and would paint over the frame. See ``tui.app.run``.
    """
    # `PrintLoggerFactory` is typed for a full TextIO but only ever calls write/flush (and
    # `isatty` is ours, below), so the narrow stand-in is cast rather than made to implement
    # an interface nothing reads.
    sink: TextIO = stream if stream is not None else cast("TextIO", _LateStderr())
    if json_logs is None:
        json_logs = not sink.isatty()

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        # never stdout: that stays clean for data (e.g. `rdt rd --json`)
        logger_factory=structlog.PrintLoggerFactory(file=sink),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
