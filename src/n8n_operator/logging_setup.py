"""Structured JSON logging, correlation IDs, and secret scrubbing.

One line of JSON per log record — ``{timestamp, level, logger, message,
correlation_id, ...extra}`` — so operator logs are grep/`jq`-able and never require a
human to parse a hand-formatted message to find what actually happened.

Every adapter (``cli/``, ``mcp/``, ``approval/``) may import this; it depends on nothing
in this package (stdlib only), so it sits alongside ``config.py``/``errors.py`` rather
than inside any capability package or ``core/`` — there is no layering direction it could
violate. Secret scrubbing is reimplemented locally rather than importing
``core.redaction.scrub_secrets``: the logic is a handful of lines, and giving this module
zero internal dependencies means it can be configured before anything else (including
``core/``) is even imported, and used from any adapter without route-specific plumbing.

A correlation ID identifies one unit of work — one CLI invocation, one MCP request —
across every log line it produces, via a :class:`contextvars.ContextVar` rather than a
parameter threaded through every function call. :func:`correlation_scope` binds one for
the duration of a ``with`` block and restores whatever was bound before on exit (safe to
nest); a process that only ever handles one unit of work at a time (every CLI command)
can instead call :func:`bind_correlation_id` once at startup and never unbind it.

Phase 8 (BUILD_PLAN section 12).
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any, TextIO

__all__ = [
    "REDACTED_MARKER",
    "bind_correlation_id",
    "configure_logging",
    "correlation_scope",
    "get_correlation_id",
    "new_correlation_id",
    "register_secret",
]

LOGGER_NAME = "n8n_operator"

REDACTED_MARKER = "[REDACTED]"

# The stdlib attributes every LogRecord carries — excluded from the "extra fields"
# merge in _JsonFormatter so a caller's own `logger.info(..., extra={...})` fields
# are the only ones that show up beyond the fixed set below.
_STANDARD_LOG_RECORD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "correlation_id",
    }
)

_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "n8n_operator_correlation_id", default=None
)

# Secret *values* to scrub from every log record, wherever they appear — additive and
# process-wide (ADR-006's "never write a credential to a log" made real): registered
# once a secret becomes known (e.g. after config.load_settings() resolves the n8n API
# key), read live by every formatted record from that point on. Never cleared: a secret
# that was ever loggable stays scrubbed for the life of the process.
_known_secrets: list[str] = []


def new_correlation_id() -> str:
    """A fresh correlation ID — a UUID4 hex string. Cheap and unique enough; not tied
    to any one transport's own request-ID scheme."""
    return uuid.uuid4().hex


def get_correlation_id() -> str | None:
    """The correlation ID bound in the current context, or ``None`` if none is bound."""
    return _correlation_id.get()


def bind_correlation_id(correlation_id: str | None = None) -> str:
    """Bind ``correlation_id`` (or a freshly minted one) for the rest of the current
    context, with no matching unbind — the right shape for a short-lived process (any
    CLI command) that handles exactly one unit of work for its whole lifetime and exits
    when it's done. A long-lived process handling many units of work concurrently or in
    sequence (an MCP server request loop) should use :func:`correlation_scope` instead,
    so one request's ID cannot leak into the next's log lines.
    """
    value = correlation_id or new_correlation_id()
    _correlation_id.set(value)
    return value


@contextmanager
def correlation_scope(correlation_id: str | None = None) -> Iterator[str]:
    """Bind ``correlation_id`` (or a freshly minted one) for the duration of this
    block, restoring whatever was bound before on exit — safe to nest, and the right
    shape for a long-lived process handling one request at a time."""
    value = correlation_id or new_correlation_id()
    token = _correlation_id.set(value)
    try:
        yield value
    finally:
        _correlation_id.reset(token)


def register_secret(value: str | None) -> None:
    """Add ``value`` to the set of secret values scrubbed from every log record from
    now on. A no-op for ``None``/empty (an unset secret is not a leak risk, and an
    empty needle would "match" everywhere)."""
    if value and value not in _known_secrets:
        _known_secrets.append(value)


def _reset_registered_secrets_for_tests() -> None:
    """Test-only: clears ``_known_secrets`` so one test's registered secret cannot
    leak into another's assertions. Not exported — the production contract is that a
    registered secret is never cleared (see :func:`register_secret`); this exists only
    because the test suite runs many independent scenarios in one process."""
    _known_secrets.clear()


def _redact(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, REDACTED_MARKER)
    return text


class _CorrelationIdFilter(logging.Filter):
    """Stamps every record with whatever correlation ID is bound in the emitting
    context — ``"-"`` when none is, so the JSON field is always present and every log
    line has the same shape whether or not a caller opted into correlation tracking."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id() or "-"
        return True


class _SecretScrubbingFilter(logging.Filter):
    """Redacts every registered secret value from a record's formatted message —
    matching on the secret's own value, independent of which field or log call put it
    there (the same value-based defense :func:`n8n_operator.core.redaction.scrub_secrets`
    applies to tool results, applied here to log output instead). Reads
    ``_known_secrets`` live on every call, not a snapshot taken at filter-construction
    time, so a secret registered after logging was configured (the common case: the API
    key is only known once ``config.load_settings()`` has run) is still caught.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if _known_secrets:
            record.msg = _redact(record.getMessage(), _known_secrets)
            record.args = ()
        return True


class _JsonFormatter(logging.Formatter):
    """One line of JSON per record — sorted keys, so two runs producing the same log
    content produce byte-identical lines (the same determinism CLI ``--json`` output
    commits to)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", "-"),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in _STANDARD_LOG_RECORD_ATTRS or key in payload:
                continue
            payload[key] = value
        return json.dumps(payload, sort_keys=True, default=str)


def configure_logging(*, level: str = "INFO", stream: TextIO | None = None) -> None:
    """Wire one JSON-emitting handler onto the ``n8n_operator`` logger namespace.

    Every module-level ``logging.getLogger(__name__)`` under this package (e.g.
    ``approval/app.py``'s) propagates up to this logger by Python logging's own
    hierarchical-naming rule, so nothing downstream needs to change to benefit from
    this — configuring it once, here, is enough. Writes to stderr by default, never
    stdout: stdout is where CLI commands print the human- or machine-readable result a
    caller asked for (``--json`` output included), and operational log noise must
    never land in the same stream a script might be parsing.

    Idempotent: safe to call more than once (every CLI invocation calls this at
    startup; so does every test that wants to inspect log output) — replaces the prior
    handler rather than stacking duplicate output.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(_JsonFormatter())
    handler.addFilter(_CorrelationIdFilter())
    handler.addFilter(_SecretScrubbingFilter())
    logger.addHandler(handler)
    logger.propagate = False
