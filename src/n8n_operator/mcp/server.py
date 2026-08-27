"""Constructs the ``MCPServer`` and registers tools and resources.

The same tool set is registered regardless of transport, so the stdio and Streamable
HTTP surfaces are provably identical (AC-23) — :func:`build_server` is the one place
either transport (``mcp/transports.py``) gets a server from, and it always registers
the same twelve ``mcp/tools.py`` tools and the same two ``mcp/resources.py`` resources.
``caller_is_local`` is the one thing that legitimately differs per transport (stdio is
always local; a Streamable HTTP bind is local only when it is loopback) — it changes
one tool's result shaping (``prepare_operation``'s ``approval_url``, invariant I12), not
which tools or schemas exist, so it is a parameter here rather than baked in.

This module is the **composition root**: the one place ``n8n.client.N8nClient``,
``n8n.preflight.N8nPreflight``, ``n8n.health.N8nHealth``, and ``n8n.dispatch.N8nDispatch``
are constructed and wired into ``core.service`` through
``core.service.PreflightPort``/``HealthPort``/``DispatchPort``. No tool handler in
``mcp/tools.py`` touches ``n8n/`` directly (ARCHITECTURE.md section 2.1); the wiring that
makes that possible happens exactly once, here.

Phase 5 (BUILD_PLAN section 12).
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.server import MCPServer
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.config import Settings
from n8n_operator.core.models import (
    DispatchOutcome,
    HealthCheckResult,
    PreflightCheck,
    PreflightResult,
    WorkflowContract,
)
from n8n_operator.mcp.resources import register_resources
from n8n_operator.mcp.tools import ToolDeps, build_tools
from n8n_operator.n8n.client import N8nClient
from n8n_operator.n8n.dispatch import N8nDispatch
from n8n_operator.n8n.health import N8nHealth
from n8n_operator.n8n.preflight import N8nPreflight

__all__ = ["build_server"]


class _PreflightAdapter:
    """Bridges ``n8n.preflight.N8nPreflight``'s local, duck-typed result dataclasses
    onto the real ``core.models.PreflightResult``/``PreflightCheck`` this composition
    root — and only this composition root — is allowed to import both sides to do
    (``n8n/`` may not import ``core/``; this module is neither)."""

    def __init__(self, impl: N8nPreflight) -> None:
        self._impl = impl

    def check(self, workflow: WorkflowContract) -> PreflightResult:
        raw = self._impl.check(workflow)
        return PreflightResult(
            ready=raw.ready,
            checks=[
                PreflightCheck(check=c.check, status=c.status, code=c.code, detail=c.detail)  # type: ignore[arg-type]
                for c in raw.checks
            ],
            checked_at=raw.checked_at,
        )


class _HealthAdapter:
    """As :class:`_PreflightAdapter`, for ``n8n.health.N8nHealth``."""

    def __init__(self, impl: N8nHealth) -> None:
        self._impl = impl

    def check(self) -> HealthCheckResult:
        raw = self._impl.check()
        return HealthCheckResult(
            reachable=raw.reachable,
            n8n_version=raw.n8n_version,
            latency_ms=raw.latency_ms,
            reason=raw.reason,
            checked_at=raw.checked_at,
        )


class _DispatchAdapter:
    """As :class:`_PreflightAdapter`/:class:`_HealthAdapter`, for ``n8n.dispatch.N8nDispatch``.

    ``fetch_node_trace`` needs no conversion — ``n8n.client.get_execution_node_trace``
    already returns the plain, allowlist-shaped ``dict[str, Any] | None`` that
    ``core.service.DispatchPort`` expects verbatim.
    """

    def __init__(self, impl: N8nDispatch) -> None:
        self._impl = impl

    def dispatch(
        self, workflow: WorkflowContract, arguments: dict[str, Any], *, timeout_seconds: int
    ) -> DispatchOutcome:
        raw = self._impl.dispatch(workflow, arguments, timeout_seconds=timeout_seconds)
        return DispatchOutcome(
            kind=raw.kind,
            http_status=raw.http_status,
            result=raw.result,
            execution_id=raw.execution_id,
            correlation_available=raw.correlation_available,
        )

    def fetch_node_trace(self, execution_id: str) -> dict[str, Any] | None:
        return self._impl.fetch_node_trace(execution_id)


def build_server(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    caller_is_local: bool,
) -> MCPServer[Any]:
    """Build one fully-wired ``MCPServer``: the real n8n client and its preflight and
    health adapters, the twelve v1 tools, and the two v1 resources."""
    client = N8nClient(
        base_url=str(settings.n8n_base_url),
        api_key=settings.n8n_api_key.get_secret_value(),
        connect_timeout_seconds=float(settings.request_timeout_seconds),
    )
    deps = ToolDeps(
        session_factory=session_factory,
        preflight=_PreflightAdapter(N8nPreflight(client)),
        health=_HealthAdapter(N8nHealth(client)),
        dispatch=_DispatchAdapter(N8nDispatch(client)),
        server_max_argument_bytes=settings.max_argument_bytes,
        caller_is_local=caller_is_local,
        approval_base_url=f"http://{settings.approval_bind}",
        known_secrets=client.known_secrets(),
    )
    server: MCPServer[Any] = MCPServer(
        "n8n-operator",
        title="n8n Operator",
        description=(
            "A governed control plane for discovering, validating, executing, and "
            "debugging approved n8n workflows."
        ),
        tools=build_tools(deps),
    )
    register_resources(server, deps)
    return server
