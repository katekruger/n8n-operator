"""MCP resources.

``registry://workflows``      the active snapshot as the model sees it, excluding
                              n8n_workflow_id, trigger, and every secret_ref
``audit://operations/{id}``   the ordered event chain for one operation

No prompts are exposed in v1 (BUILD_PLAN section 7.1). Registered onto an already
constructed ``MCPServer`` via :func:`register_resources`, rather than built as
free-standing ``Resource`` objects the way ``mcp/tools.py`` builds ``Tool`` objects —
resource URIs carry no client-supplied JSON blob (a template parameter is one path
segment substituted into a fixed shape), so none of the "must be an explicit,
`extra=forbid`ded Pydantic model" reasoning that drove manual ``Tool`` construction
applies here; the ergonomic ``@server.resource(...)`` decorator's own signature
introspection is exactly enough.

Phase 5 (BUILD_PLAN section 12).
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from mcp.server.mcpserver.server import MCPServer

from n8n_operator.core import service
from n8n_operator.errors import OperatorError
from n8n_operator.mcp.tools import ToolDeps, _iso
from n8n_operator.storage.repository import OperationEventRepository
from n8n_operator.storage.session import session_scope

__all__ = ["register_resources"]


def register_resources(server: MCPServer[Any], deps: ToolDeps) -> None:
    """Register the two v1 resources onto ``server`` (MCP_TOOLS.md section 3)."""

    @server.resource(
        "registry://workflows",
        name="registry-workflows",
        description=(
            "The active registry snapshot as the model sees it: registry IDs, titles, "
            "descriptions, input schemas, risk and side-effect classes. Excludes "
            "n8n_workflow_id, trigger, and every secret_ref."
        ),
        mime_type="application/json",
    )
    async def registry_workflows() -> dict[str, Any]:
        with session_scope(deps.session_factory) as session:
            summaries = service.list_workflows(session)
            details = [
                service.describe_workflow(session, workflow_id=s.workflow_id).model_dump(
                    mode="json"
                )
                for s in summaries
            ]
            snapshot = service.get_active_snapshot(session)
        return {
            "workflows": details,
            "registry_snapshot": snapshot.content_hash if snapshot else None,
        }

    @server.resource(
        "audit://operations/{operation_id}",
        name="audit-operation-events",
        description=(
            "The ordered event chain for one operation: transitions, actors, "
            "timestamps, redacted details."
        ),
        mime_type="application/json",
    )
    async def audit_operation(operation_id: str) -> dict[str, Any]:
        with session_scope(deps.session_factory) as session:
            try:
                service.get_operation(
                    session, operation_id=operation_id, principal_id=deps.principal_id
                )
            except OperatorError as exc:
                raise ResourceNotFoundError(exc.message) from exc
            events = OperationEventRepository(session).list_for_operation(operation_id)
        return {
            "operation_id": operation_id,
            "events": [
                {
                    "from_state": event.from_state,
                    "to_state": event.to_state,
                    "transition": event.transition,
                    "actor": event.actor,
                    "detail": event.detail,
                    "occurred_at": _iso(event.occurred_at),
                }
                for event in events
            ],
        }
