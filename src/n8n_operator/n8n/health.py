"""Instance reachability adapter — satisfies ``core.service.HealthPort`` structurally.

``get_instance_health`` (MCP_TOOLS.md section 2.3) is a discovery tool: no URL, no
credential, and — per docs/N8N_COMPATIBILITY.md section 10 — no true release version,
since n8n exposes no endpoint that returns one. ``n8n_version`` here is the n8n Public
API's own spec version (``get_api_version_info()``), the best available proxy, never
fabricated beyond what n8n itself reports.

Phase 5 (BUILD_PLAN section 12).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from n8n_operator.errors import InstanceUnreachableError, ProviderError
from n8n_operator.n8n.client import N8nClient

__all__ = ["HealthCheckResult", "N8nHealth"]


@dataclass(frozen=True)
class HealthCheckResult:
    """Structurally identical to ``core.models.HealthCheckResult`` — see
    ``n8n/preflight.py``'s ``PreflightResult`` docstring for why this is a parallel
    definition rather than an import (capability packages must not depend on ``core/``)."""

    reachable: bool
    n8n_version: str | None = None
    latency_ms: int | None = None
    reason: str | None = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class N8nHealth:
    """Satisfies ``core.service.HealthPort`` structurally — a ``check() -> HealthCheckResult``
    method, exactly like ``N8nPreflight`` satisfies ``PreflightPort`` (``n8n/preflight.py``)."""

    def __init__(self, client: N8nClient) -> None:
        self._client = client

    def check(self) -> HealthCheckResult:
        started = time.monotonic()
        try:
            self._client.health_check()
        except ProviderError:
            return HealthCheckResult(
                reachable=False,
                reason=InstanceUnreachableError.code,
                checked_at=datetime.now(UTC),
            )
        latency_ms = round((time.monotonic() - started) * 1000)
        version = self._client.get_api_version_info()
        return HealthCheckResult(
            reachable=True,
            n8n_version=version,
            latency_ms=latency_ms,
            checked_at=datetime.now(UTC),
        )
