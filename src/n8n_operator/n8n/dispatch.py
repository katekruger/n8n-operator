"""Dispatch adapter — satisfies ``core.service.DispatchPort`` structurally.

The one place a registry workflow entry's ``trigger.path``/``trigger.method`` are
turned into an actual ``N8nClient.dispatch_webhook`` call (ADR-005, ADR-009), and the
one place ``N8nClient.get_execution_node_trace``'s already-allowlisted result is handed
upward for ``get_execution_log``. Neither method here does any additional shaping —
``n8n/client.py`` already returns exactly the safe shape both need; this class exists
only to adapt the registry entry's shape into the client's own argument shape, the same
narrow-adapter role ``N8nPreflight``/``N8nHealth`` play for their own ports.

Phase 7 (BUILD_PLAN section 12).
"""

from __future__ import annotations

from typing import Any

from n8n_operator.n8n.client import DispatchOutcome, N8nClient
from n8n_operator.n8n.preflight import WorkflowLike

__all__ = ["N8nDispatch"]


class N8nDispatch:
    """Satisfies ``core.service.DispatchPort`` structurally — ``dispatch(...)`` and
    ``fetch_node_trace(...)`` methods shaped exactly like the port expects, without
    ``core/`` ever importing this module or ``n8n/`` ever importing ``core/``."""

    def __init__(self, client: N8nClient) -> None:
        self._client = client

    def dispatch(
        self, workflow: WorkflowLike, arguments: dict[str, Any], *, timeout_seconds: int
    ) -> DispatchOutcome:
        return self._client.dispatch_webhook(
            path=workflow.trigger.path,
            method=workflow.trigger.method,
            json_body=arguments,
            timeout_seconds=float(timeout_seconds),
        )

    def fetch_node_trace(self, execution_id: str) -> dict[str, Any] | None:
        return self._client.get_execution_node_trace(execution_id)
