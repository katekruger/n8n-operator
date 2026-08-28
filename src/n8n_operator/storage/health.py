"""Database connectivity health check.

Mirrors ``n8n/health.py``'s shape (reachability + latency, never a credential) for the
storage layer's own dependency: is the configured database actually reachable right now.
Used by ``n8n-operator db status`` and available to any future adapter that wants a
storage health signal without importing the CLI.

Never returns ``database_url`` itself — only the dialect name, which is not sensitive
and is useful for confirming "this is the Postgres I meant to point at" in a support
conversation. A caller that also wants to display the URL must redact it separately via
:func:`n8n_operator.config.redact_database_url` — this module has no opinion on display
formatting, only on what it is safe to compute.

Phase 10 (v2) stage 01.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import Engine, select


@dataclass(frozen=True)
class DatabaseHealth:
    """The result of one connectivity probe. Never carries a credential or a URL."""

    reachable: bool
    dialect: str
    latency_ms: float | None
    pool_size: int | None
    checked_out_connections: int | None
    error: str | None


def check_database_health(engine: Engine) -> DatabaseHealth:
    """Open one connection, run ``SELECT 1``, and report reachability and latency.

    A failure is captured as ``reachable=False`` with a short, credential-free error
    summary (the exception's type name, not its full message — a driver's connection
    error can itself echo back a DSN or a password, and this function's entire purpose
    is to be safe to log unconditionally).
    """
    dialect = engine.dialect.name
    pool = engine.pool
    pool_size = getattr(pool, "size", lambda: None)() if hasattr(pool, "size") else None
    checked_out = (
        getattr(pool, "checkedout", lambda: None)() if hasattr(pool, "checkedout") else None
    )

    started = time.monotonic()
    try:
        with engine.connect() as connection:
            connection.execute(select(1))
    except Exception as exc:
        return DatabaseHealth(
            reachable=False,
            dialect=dialect,
            latency_ms=None,
            pool_size=pool_size,
            checked_out_connections=checked_out,
            error=type(exc).__name__,
        )
    latency_ms = (time.monotonic() - started) * 1000
    return DatabaseHealth(
        reachable=True,
        dialect=dialect,
        latency_ms=round(latency_ms, 2),
        pool_size=pool_size,
        checked_out_connections=checked_out,
        error=None,
    )


__all__ = ["DatabaseHealth", "check_database_health"]
