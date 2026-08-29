"""The twelve v1 tools.

Inventory is normative in BUILD_PLAN section 7.1; contracts in ``docs/MCP_TOOLS.md``:

    list_workflows        describe_workflow     get_instance_health
    validate_input        preflight_workflow    prepare_operation
    get_operation          execute_operation     cancel_operation
    list_operations       get_execution_result  get_execution_log

**Argument schemas.** Each tool has its own explicit Pydantic v2 model (``extra="forbid"``,
subclassing the MCP SDK's ``ArgModelBase`` so it can serve as a tool's ``fn_metadata.arg_model``
directly). ``Tool`` objects are constructed by hand (:func:`_build_tool`) rather than
through ``MCPServer.tool()``'s own signature-introspection: that path builds its argument
model from the *handler function's* parameters with permissive ``extra`` handling (an
unknown top-level field is silently dropped, never rejected), which cannot satisfy "no
tool accepts an unlisted argument" (boundary B2) no matter how the handler is written.
Constructing ``Tool`` directly makes the *explicit* model both the published schema
(``Tool.parameters = ArgsModel.model_json_schema()``) and the one actually validated
against at call time (``Tool.run`` validates via ``fn_metadata.arg_model``) — the same
model, so the two can never drift apart. No tool argument is ever an n8n workflow ID, an
instance URL, or a raw request body — there is no field for one (boundary B1).

**Result shaping.** Every handler builds its return dict from an explicit key list
matching MCP_TOOLS.md's documented shape — never by dumping a domain object's own
``__dict__`` or forwarding a registry/storage row — so a new internal field added
elsewhere is invisible here by default rather than leaked by default (boundary B5).

**Errors.** A handler catches ``OperatorError`` and returns ``{"error": exc.to_dict()}``
as an ordinary (non-``isError``) tool result: MCP's own ``isError`` channel, when the SDK
raises it for us (e.g. a schema-validation failure caught before a handler even runs),
carries only a prose string with no ``code``/``details``/``retryable`` — using it here too
would mean returning taxonomy-shaped errors two different, incompatible ways depending on
which layer caught the failure. Handlers therefore never let ``OperatorError`` propagate;
whatever the client actually receives for a *business* error (as opposed to a malformed
call) is always the exact MCP_TOOLS.md section 4.1 envelope.

``approval_url`` is gated on caller locality: it is returned only over stdio or a
loopback-bound Streamable HTTP listener — a property of *which transport this server
process is running as*, decided once at startup (``ToolDeps.caller_is_local``), not
per-request (a loopback HTTP bind is unreachable from anywhere but local processes to
begin with; a non-loopback bind is treated as remote for every caller on it, whether or
not any individual request happens to originate from localhost). Remote callers receive
``approval_required``, the operation ID, and human-readable instructions instead of an
address they cannot reach (invariant I12, boundary B13, ADR-010).

Phase 5 (BUILD_PLAN section 12).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.tools.base import Tool
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase, FuncMetadata
from mcp_types import ToolAnnotations
from pydantic import ConfigDict, Field
from sqlalchemy.orm import sessionmaker

from n8n_operator.core import identity, service
from n8n_operator.core.identity import build_whoami
from n8n_operator.core.service import DispatchPort, HealthPort, PreflightPort
from n8n_operator.errors import DispatchIndeterminateError, InvalidArgumentsError, OperatorError
from n8n_operator.storage.repository import (
    ApprovalRepository,
    OperationEventRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import session_scope

__all__ = ["N8nAdapterBundle", "ToolDeps", "build_tools"]

_RISK = Literal["low", "medium", "high"]
_SIDE_EFFECTS = Literal["read_only", "external_write", "irreversible"]


@dataclass(frozen=True)
class N8nAdapterBundle:
    """Preflight/health/dispatch bound to *one* environment's own n8n instance
    (stage 04) — what ``ToolDeps.n8n_client_factory`` returns, mirroring the fixed
    ``preflight``/``health``/``dispatch`` fields below exactly, so a v2 caller with a
    real, resolved environment reaches that environment's own instance rather than
    silently sharing the single v1/dev-mode client every fixed field still points at."""

    preflight: PreflightPort
    health: HealthPort
    dispatch: DispatchPort


@dataclass(frozen=True)
class ToolDeps:
    """Everything a tool handler needs, injected once by the composition root
    (``mcp/server.py``) rather than constructed by any handler — the same "adapters are
    thin, core does the work" seam ``core.service.PreflightPort``/``HealthPort`` already
    establish for n8n I/O (ARCHITECTURE.md section 2.1)."""

    session_factory: sessionmaker[Any]
    preflight: PreflightPort
    health: HealthPort
    dispatch: DispatchPort
    server_max_argument_bytes: int
    principal_id: str = "local"
    environment: str = "default"
    caller_is_local: bool = True
    approval_base_url: str | None = None
    known_secrets: tuple[str, ...] = ()
    # Gates whoami's registration as a 13th tool (BUILD_PLAN section 7.2, stage 02).
    # False (v1, the default) registers exactly the twelve tools AC-23 requires —
    # nothing here changes v1's tool surface unless an operator opts in.
    enable_v2: bool = False
    # Stage 04: resolves a real, v2 environment_id to *that* environment's own n8n
    # instance. `None` (v1, and v2 before an environment resolves) means every call
    # keeps using the fixed `preflight`/`health`/`dispatch` fields above — v1's own
    # n8n I/O is completely unaffected by this field's mere presence.
    n8n_client_factory: Callable[[str], N8nAdapterBundle] | None = None
    # Stage 05: injected by the composition root's `_build_notification_sink` — `None`
    # in v1 and whenever `enable_v2` is False, since `request_approval`/notification
    # delivery are v2-only concepts (ADR-018).
    notification_sink: service.NotificationSink | None = None


def _resolve_principal_id(deps: ToolDeps) -> str:
    """The caller attributed to this request.

    In v2 OIDC mode, ``mcp.server.auth``'s own contextvar (populated per-request by
    ``AuthContextMiddleware``, driven by the composition root's token verifier —
    ``mcp/server.py``) carries the already-resolved ``principal_id`` in
    ``AccessToken.claims`` — set once, in ``verify_token``, after JIT provisioning and
    the disabled-principal check have already run (ADR-014 section 4). Every other case
    (stdio, v1, v2 dev mode — none of which configure a token verifier at all) falls
    back to ``deps.principal_id``, the one fixed principal that mode attributes every
    call to (ADR-014 section 5).
    """
    access_token = get_access_token()
    if access_token is not None and access_token.claims:
        principal_id = access_token.claims.get("principal_id")
        if isinstance(principal_id, str) and principal_id:
            return principal_id
    return deps.principal_id


def _iso(value: datetime | None) -> str | None:
    """RFC 3339, UTC, ``Z`` suffix (MCP_TOOLS.md section 1 "Timestamps")."""
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _error_result(exc: OperatorError) -> dict[str, Any]:
    return {"error": exc.to_dict()}


def _resolved_environment_id(
    deps: ToolDeps, session: Any, *, principal_id: str, environment: str | None
) -> str | None:
    """The environment a discovery/preparation call resolved to, for the result
    envelope (MCP_TOOLS.md's "result fields" column, stage 04) — ``None`` in v1
    (``deps.enable_v2=False``), where the concept does not exist and the result shape
    stays byte-identical to before this stage. Cheap to call ahead of the use case
    itself doing the identical resolution internally (an indexed read, no writes) —
    keeping ``core.service``'s own use cases as the single place resolution *rules*
    live, per this module's existing "adapters are thin" discipline."""
    if not deps.enable_v2:
        return None
    return identity.resolve_environment(
        session, principal_id=principal_id, environment=environment
    ).id


def _adapters_for(
    deps: ToolDeps, resolved_environment_id: str | None
) -> tuple[PreflightPort, HealthPort, DispatchPort]:
    """Which preflight/health/dispatch a call should use: that environment's own
    instance when one resolved and a factory is configured, else the single fixed
    client every v1 (and v2-before-a-factory-is-configured) call already used — never
    a behavior change for a deployment that hasn't set up ``n8n_client_factory``."""
    if resolved_environment_id is not None and deps.n8n_client_factory is not None:
        bundle = deps.n8n_client_factory(resolved_environment_id)
        return bundle.preflight, bundle.health, bundle.dispatch
    return deps.preflight, deps.health, deps.dispatch


def _latest_event_detail(session: Any, operation_id: str) -> dict[str, Any]:
    """The ``detail`` dict of the most recent ``operation_events`` row for
    ``operation_id`` — where ``prepare_operation``'s validation errors, preflight
    checks, and (once populated) an execution outcome's ``truncated`` marker actually
    live, since ``core.models.Operation`` itself carries only the state, not the detail
    of how it got there. Empty if the operation has no events yet, which never happens
    for an operation this function is called with (every reachable state here followed
    at least one transition)."""
    events = OperationEventRepository(session).list_for_operation(operation_id)
    return events[-1].detail if events else {}


class _ToolArgs(ArgModelBase):
    """Base for every tool's argument model: unknown top-level fields are a hard error
    (boundary B2), never silently ignored — the one behavior ``ArgModelBase`` itself
    does not set (see this module's docstring)."""

    model_config = ConfigDict(extra="forbid")


def _build_tool(
    *,
    name: str,
    description: str,
    args_model: type[_ToolArgs],
    handler: Any,
    annotations: ToolAnnotations,
) -> Tool:
    return Tool(
        fn=handler,
        name=name,
        title=None,
        description=description,
        parameters=args_model.model_json_schema(by_alias=True),
        fn_metadata=FuncMetadata(arg_model=args_model),
        is_async=True,
        context_kwarg=None,
        annotations=annotations,
    )


_READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
_READ_ONLY_LIVE = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)


# ======================================================================================
# list_workflows
# ======================================================================================


class ListWorkflowsArgs(_ToolArgs):
    tags: list[str] | None = None
    risk: _RISK | None = None
    side_effects: _SIDE_EFFECTS | None = None
    environment: str | None = None
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = None


def _make_list_workflows(deps: ToolDeps) -> Tool:
    async def handler(
        tags: list[str] | None = None,
        risk: _RISK | None = None,
        side_effects: _SIDE_EFFECTS | None = None,
        environment: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        principal_id = _resolve_principal_id(deps)
        with session_scope(deps.session_factory) as session:
            try:
                resolved_environment_id = _resolved_environment_id(
                    deps, session, principal_id=principal_id, environment=environment
                )
                summaries = service.list_workflows(
                    session,
                    tags=tags,
                    risk=risk,
                    side_effects=side_effects,
                    principal_id=principal_id,
                    enable_v2=deps.enable_v2,
                    environment=environment,
                    limit=limit,
                    cursor=cursor,
                )
                snapshot = service.get_active_snapshot(session)
            except OperatorError as exc:
                return _error_result(exc)
        result = {
            "workflows": [s.model_dump(mode="json") for s in summaries],
            "registry_snapshot": snapshot.content_hash if snapshot else None,
            "count": len(summaries),
        }
        if deps.enable_v2:
            result["environment"] = resolved_environment_id
            result["next_cursor"] = summaries[-1].workflow_id if len(summaries) == limit else None
        return result

    return _build_tool(
        name="list_workflows",
        description=(
            "List the registered, enabled workflows this server can prepare and "
            "execute. A workflow live on the n8n instance but absent here is invisible "
            "and cannot be prepared."
        ),
        args_model=ListWorkflowsArgs,
        handler=handler,
        annotations=_READ_ONLY,
    )


# ======================================================================================
# describe_workflow
# ======================================================================================


class DescribeWorkflowArgs(_ToolArgs):
    workflow_id: str
    environment: str | None = None


def _make_describe_workflow(deps: ToolDeps) -> Tool:
    async def handler(workflow_id: str, environment: str | None = None) -> dict[str, Any]:
        principal_id = _resolve_principal_id(deps)
        with session_scope(deps.session_factory) as session:
            try:
                resolved_environment_id = _resolved_environment_id(
                    deps, session, principal_id=principal_id, environment=environment
                )
                detail = service.describe_workflow(
                    session,
                    workflow_id=workflow_id,
                    principal_id=principal_id,
                    enable_v2=deps.enable_v2,
                    environment=environment,
                )
                snapshot = service.get_active_snapshot(session)
            except OperatorError as exc:
                return _error_result(exc)
        shaped = detail.model_dump(mode="json")
        shaped["registry_snapshot"] = snapshot.content_hash if snapshot else None
        if deps.enable_v2:
            shaped["environment"] = resolved_environment_id
        return shaped

    return _build_tool(
        name="describe_workflow",
        description=(
            "The full contract for one workflow: description, input schema, limits, "
            "approval policy, and output shape — everything needed to construct a "
            "valid call and understand what approving it would mean."
        ),
        args_model=DescribeWorkflowArgs,
        handler=handler,
        annotations=_READ_ONLY,
    )


# ======================================================================================
# get_instance_health
# ======================================================================================


class GetInstanceHealthArgs(_ToolArgs):
    environment: str | None = None


def _make_get_instance_health(deps: ToolDeps) -> Tool:
    async def handler(environment: str | None = None) -> dict[str, Any]:
        principal_id = _resolve_principal_id(deps)
        resolved_environment_id: str | None = None
        if deps.enable_v2:
            with session_scope(deps.session_factory) as session:
                try:
                    resolved_environment_id = _resolved_environment_id(
                        deps, session, principal_id=principal_id, environment=environment
                    )
                except OperatorError as exc:
                    return _error_result(exc)
        _, health_port, _ = _adapters_for(deps, resolved_environment_id)
        result = service.get_instance_health(health_port)
        shaped: dict[str, Any] = (
            {"reachable": False, "reason": result.reason, "checked_at": _iso(result.checked_at)}
            if not result.reachable
            else {
                "reachable": True,
                "n8n_version": result.n8n_version,
                "latency_ms": result.latency_ms,
                "checked_at": _iso(result.checked_at),
            }
        )
        if deps.enable_v2:
            shaped["environment"] = resolved_environment_id
        return shaped

    return _build_tool(
        name="get_instance_health",
        description=(
            "Reachability and version of the configured n8n instance. No URL and no "
            "credential are ever returned."
        ),
        args_model=GetInstanceHealthArgs,
        handler=handler,
        annotations=_READ_ONLY_LIVE,
    )


# ======================================================================================
# validate_input
# ======================================================================================


class ValidateInputArgs(_ToolArgs):
    workflow_id: str
    arguments: dict[str, Any]
    environment: str | None = None


def _make_validate_input(deps: ToolDeps) -> Tool:
    async def handler(
        workflow_id: str, arguments: dict[str, Any], environment: str | None = None
    ) -> dict[str, Any]:
        principal_id = _resolve_principal_id(deps)
        with session_scope(deps.session_factory) as session:
            try:
                resolved_environment_id = _resolved_environment_id(
                    deps, session, principal_id=principal_id, environment=environment
                )
                errors = service.validate_input(
                    session,
                    workflow_id=workflow_id,
                    arguments=arguments,
                    principal_id=principal_id,
                    enable_v2=deps.enable_v2,
                    environment=environment,
                )
            except OperatorError as exc:
                return _error_result(exc)
        result: dict[str, Any] = {"valid": not errors, "errors": [e.to_dict() for e in errors]}
        if deps.enable_v2:
            result["environment"] = resolved_environment_id
        return result

    return _build_tool(
        name="validate_input",
        description=(
            "Check arguments against a workflow's input schema without creating an "
            "operation or touching n8n — a cheap self-correction loop before "
            "prepare_operation."
        ),
        args_model=ValidateInputArgs,
        handler=handler,
        annotations=_READ_ONLY,
    )


# ======================================================================================
# preflight_workflow
# ======================================================================================


class PreflightWorkflowArgs(_ToolArgs):
    workflow_id: str
    environment: str | None = None


def _make_preflight_workflow(deps: ToolDeps) -> Tool:
    async def handler(workflow_id: str, environment: str | None = None) -> dict[str, Any]:
        principal_id = _resolve_principal_id(deps)
        with session_scope(deps.session_factory) as session:
            try:
                resolved_environment_id = _resolved_environment_id(
                    deps, session, principal_id=principal_id, environment=environment
                )
                preflight_port, _, _ = _adapters_for(deps, resolved_environment_id)
                result = service.preflight_workflow(
                    session,
                    workflow_id=workflow_id,
                    preflight=preflight_port,
                    principal_id=principal_id,
                    enable_v2=deps.enable_v2,
                    environment=environment,
                )
            except OperatorError as exc:
                return _error_result(exc)
        shaped: dict[str, Any] = {
            "ready": result.ready,
            "checks": [c.model_dump(mode="json") for c in result.checks],
            "checked_at": _iso(result.checked_at),
        }
        if deps.enable_v2:
            shaped["environment"] = resolved_environment_id
        return shaped

    return _build_tool(
        name="preflight_workflow",
        description=(
            "Liveness, active status, definition-drift, credential-binding, and "
            "correlation checks for one workflow, without creating an operation. "
            "Runs the same checks prepare_operation runs."
        ),
        args_model=PreflightWorkflowArgs,
        handler=handler,
        annotations=_READ_ONLY_LIVE,
    )


# ======================================================================================
# prepare_operation
# ======================================================================================


class PrepareOperationArgs(_ToolArgs):
    workflow_id: str
    arguments: dict[str, Any]
    idempotency_key: str | None = None
    reason: str | None = None
    environment: str | None = None


def _shape_prepare_result(
    *,
    operation_id: str,
    state: str,
    workflow_id: str,
    idempotent_replay: bool,
    approval_token: str | None,
    detail: dict[str, Any],
    created_at: str | None,
    approval_expires_at: str | None,
    execution_deadline: str | None,
    environment: str | None,
    deps: ToolDeps,
) -> dict[str, Any]:
    if state == "INVALID":
        result: dict[str, Any] = {
            "operation_id": operation_id,
            "state": state,
            "workflow_id": workflow_id,
            "errors": detail.get("errors", []),
        }
    elif state == "BLOCKED":
        result = {
            "operation_id": operation_id,
            "state": state,
            "workflow_id": workflow_id,
            "checks": detail.get("checks", []),
        }
    elif state == "APPROVED":
        result = {
            "operation_id": operation_id,
            "state": state,
            "workflow_id": workflow_id,
            "approval_required": False,
            "execution_deadline": execution_deadline,
            "created_at": created_at,
            "idempotent_replay": idempotent_replay,
        }
    elif state == "PENDING_APPROVAL":
        result = {
            "operation_id": operation_id,
            "state": state,
            "workflow_id": workflow_id,
            "approval_required": True,
            "approval_instructions": (
                "A human must approve this operation on the Operator machine: run "
                f"`n8n-operator operations approve {operation_id}`. "
                "You cannot approve it yourself."
            ),
            "approval_expires_at": approval_expires_at,
            "created_at": created_at,
            "idempotent_replay": idempotent_replay,
        }
        if deps.caller_is_local and approval_token is not None and deps.approval_base_url:
            result["approval_url"] = f"{deps.approval_base_url}/approve/{approval_token}"
    else:
        # Reachable only via an idempotent replay of an operation that has since moved
        # on (e.g. EXPIRED, CANCELED) — not one of the four documented "fresh call"
        # shapes, so it is reported minimally and honestly rather than forced into one.
        result = {
            "operation_id": operation_id,
            "state": state,
            "workflow_id": workflow_id,
            "idempotent_replay": idempotent_replay,
        }
    if deps.enable_v2:
        result["environment"] = environment
    return result


def _make_prepare_operation(deps: ToolDeps) -> Tool:
    async def handler(
        workflow_id: str,
        arguments: dict[str, Any],
        idempotency_key: str | None = None,
        reason: str | None = None,
        environment: str | None = None,
    ) -> dict[str, Any]:
        principal_id = _resolve_principal_id(deps)
        with session_scope(deps.session_factory) as session:
            try:
                resolved_environment_id = _resolved_environment_id(
                    deps, session, principal_id=principal_id, environment=environment
                )
                preflight_port, _, _ = _adapters_for(deps, resolved_environment_id)
                operation, idempotent_replay, approval_token = service.prepare_operation(
                    session,
                    principal_id=principal_id,
                    environment=environment if deps.enable_v2 else deps.environment,
                    workflow_id=workflow_id,
                    arguments=arguments,
                    preflight=preflight_port,
                    server_max_argument_bytes=deps.server_max_argument_bytes,
                    idempotency_key=idempotency_key,
                    reason=reason,
                    enable_v2=deps.enable_v2,
                )
            except OperatorError as exc:
                return _error_result(exc)
            detail = _latest_event_detail(session, operation.id)
        return _shape_prepare_result(
            operation_id=operation.id,
            state=operation.state,
            workflow_id=operation.workflow_id,
            idempotent_replay=idempotent_replay,
            approval_token=approval_token,
            detail=detail,
            created_at=_iso(operation.created_at),
            approval_expires_at=_iso(operation.approval_expires_at),
            execution_deadline=_iso(operation.execution_deadline),
            # In v2, `Operation.environment` is the resolved environment's own id (this
            # module's `prepare_operation` docstring / core.service's own comment) — the
            # same value an idempotent replay would have recorded originally, so this
            # stays correct across a replay too, not just a fresh call.
            environment=operation.environment if deps.enable_v2 else None,
            deps=deps,
        )

    return _build_tool(
        name="prepare_operation",
        description=(
            "Validate arguments, preflight the workflow, and mint an operation "
            "handle. The only way to obtain the authority to execute. Runs nothing "
            "against n8n; creates durable Operator state only."
        ),
        args_model=PrepareOperationArgs,
        handler=handler,
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )


# ======================================================================================
# get_operation
# ======================================================================================


class GetOperationArgs(_ToolArgs):
    operation_id: str


def _make_get_operation(deps: ToolDeps) -> Tool:
    async def handler(operation_id: str) -> dict[str, Any]:
        with session_scope(deps.session_factory) as session:
            try:
                operation = service.get_operation(
                    session,
                    operation_id=operation_id,
                    principal_id=_resolve_principal_id(deps),
                    enable_v2=deps.enable_v2,
                )
            except OperatorError as exc:
                return _error_result(exc)
            approval_row = ApprovalRepository(session).get_by_operation_id(operation_id)
        if approval_row is None:
            approval_block: dict[str, Any] = {
                "required": False,
                "decided": False,
                "decision": None,
                "decided_at": None,
            }
        else:
            approval_block = {
                "required": True,
                "decided": approval_row.decision is not None,
                "decision": approval_row.decision,
                "decided_at": _iso(approval_row.decided_at),
            }
        result: dict[str, Any] = {
            "operation_id": operation.id,
            "workflow_id": operation.workflow_id,
            "state": operation.state,
            "created_at": _iso(operation.created_at),
            "state_changed_at": _iso(operation.updated_at),
            "approval_expires_at": _iso(operation.approval_expires_at),
            "execution_deadline": _iso(operation.execution_deadline),
            "approval": approval_block,
            "handle_used": operation.handle_burned_at is not None,
            "arguments": operation.arguments,
        }
        if deps.enable_v2:
            result["environment"] = operation.environment
        return result

    return _build_tool(
        name="get_operation",
        description=(
            "Current state, timestamps, deadlines, and approval status of one "
            "operation. The polling tool while awaiting approval."
        ),
        args_model=GetOperationArgs,
        handler=handler,
        annotations=_READ_ONLY,
    )


# ======================================================================================
# execute_operation
# ======================================================================================


class ExecuteOperationArgs(_ToolArgs):
    operation_id: str
    handle: str


def _make_execute_operation(deps: ToolDeps) -> Tool:
    async def handler(operation_id: str, handle: str) -> dict[str, Any]:
        # Burns the handle and moves to EXECUTING (ARCHITECTURE.md section 4.3 steps
        # 0-6), in its own transaction. Dispatching to n8n and resolving to
        # SUCCEEDED/FAILED/UNKNOWN (steps 7-14) is a *separate* call, to a *separate*
        # use case (core.service.dispatch_operation) that manages its own transaction
        # pair around the network call — never inside the transaction above.
        principal_id = _resolve_principal_id(deps)
        # Resolved once, up front: this operation's own environment was fixed at
        # `prepare_operation` time (`operation.environment` — a real environment id in
        # v2, ``"default"`` in v1) and never changes, so the preflight/dispatch this
        # call uses is that same environment's own instance, not whatever `environment`
        # happened to resolve to on this particular call (there is no such argument
        # here at all — the operation ID alone already pins it).
        with session_scope(deps.session_factory) as session:
            try:
                pinned_operation = service.get_operation(
                    session,
                    operation_id=operation_id,
                    principal_id=principal_id,
                    enable_v2=deps.enable_v2,
                )
            except OperatorError as exc:
                return _error_result(exc)
        pinned_environment_id = pinned_operation.environment if deps.enable_v2 else None
        preflight_port, _, dispatch_port = _adapters_for(deps, pinned_environment_id)

        with session_scope(deps.session_factory) as session:
            try:
                service.execute_operation(
                    session,
                    operation_id=operation_id,
                    handle=handle,
                    principal_id=principal_id,
                    preflight=preflight_port,
                    enable_v2=deps.enable_v2,
                )
            except OperatorError as exc:
                return _error_result(exc)

        try:
            operation = service.dispatch_operation(
                deps.session_factory,
                operation_id=operation_id,
                principal_id=principal_id,
                dispatch=dispatch_port,
                known_secrets=deps.known_secrets,
            )
        except OperatorError as exc:
            return _error_result(exc)

        if operation.state == "UNKNOWN":
            # ADR-009/ADR-005: never inferred to be a failure, never retried. The
            # message is written to tell a model plainly not to retry.
            if operation.n8n_execution_id is not None:
                correlation: dict[str, Any] = {
                    "available": True,
                    "execution_id": operation.n8n_execution_id,
                }
            else:
                correlation = {"available": False, "reason": "NO_EXECUTION_CORRELATION"}
            indeterminate = DispatchIndeterminateError()
            unknown_result: dict[str, Any] = {
                "operation_id": operation.id,
                "state": operation.state,
                "code": indeterminate.code,
                "message": indeterminate.message,
                "started_at": _iso(operation.updated_at),
                "correlation": correlation,
            }
            if deps.enable_v2:
                unknown_result["environment"] = operation.environment
            return unknown_result

        with session_scope(deps.session_factory) as session:
            try:
                result = service.get_execution_result(
                    session,
                    operation_id=operation_id,
                    principal_id=principal_id,
                    enable_v2=deps.enable_v2,
                )
            except OperatorError as exc:
                return _error_result(exc)
            truncated = bool(_latest_event_detail(session, operation_id).get("truncated", False))

        duration_ms: int | None = None
        if result.started_at is not None and result.finished_at is not None:
            duration_ms = int((result.finished_at - result.started_at).total_seconds() * 1000)

        if operation.state == "FAILED":
            failed_result: dict[str, Any] = {
                "operation_id": operation.id,
                "state": operation.state,
                "started_at": _iso(result.started_at),
                "finished_at": _iso(result.finished_at),
                "duration_ms": duration_ms,
                "error": {**(result.error or {}), "truncated": truncated},
            }
            if deps.enable_v2:
                failed_result["environment"] = operation.environment
            return failed_result
        succeeded_result: dict[str, Any] = {
            "operation_id": operation.id,
            "state": operation.state,
            "started_at": _iso(result.started_at),
            "finished_at": _iso(result.finished_at),
            "duration_ms": duration_ms,
            "result": {**result.redacted_payload, "truncated": truncated},
        }
        if deps.enable_v2:
            succeeded_result["environment"] = operation.environment
        return succeeded_result

    return _build_tool(
        name="execute_operation",
        description=(
            "Burn the handle and dispatch to n8n. The only tool in the product that "
            "causes an external side effect."
        ),
        args_model=ExecuteOperationArgs,
        handler=handler,
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )


# ======================================================================================
# cancel_operation
# ======================================================================================


class CancelOperationArgs(_ToolArgs):
    operation_id: str
    reason: str | None = None


def _make_cancel_operation(deps: ToolDeps) -> Tool:
    async def handler(operation_id: str, reason: str | None = None) -> dict[str, Any]:
        with session_scope(deps.session_factory) as session:
            try:
                operation = service.cancel_operation(
                    session,
                    operation_id=operation_id,
                    principal_id=_resolve_principal_id(deps),
                    reason=reason,
                    enable_v2=deps.enable_v2,
                )
            except OperatorError as exc:
                return _error_result(exc)
        result: dict[str, Any] = {
            "operation_id": operation.id,
            "state": operation.state,
            "canceled_at": _iso(operation.updated_at),
        }
        if deps.enable_v2:
            result["environment"] = operation.environment
        return result

    return _build_tool(
        name="cancel_operation",
        description="Terminate a PENDING_APPROVAL or APPROVED operation before it runs.",
        args_model=CancelOperationArgs,
        handler=handler,
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )


# ======================================================================================
# list_operations
# ======================================================================================


class ListOperationsArgs(_ToolArgs):
    workflow_id: str | None = None
    state: list[str] | None = None
    since: str | None = None
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = None
    environment: str | None = None


def _make_list_operations(deps: ToolDeps) -> Tool:
    async def handler(
        workflow_id: str | None = None,
        state: list[str] | None = None,
        since: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
        environment: str | None = None,
    ) -> dict[str, Any]:
        since_dt: datetime | None = None
        if since is not None:
            try:
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            except ValueError:
                return _error_result(
                    InvalidArgumentsError(
                        f"'since' is not a valid RFC 3339 timestamp: {since!r}.",
                        details={"since": since},
                    )
                )
        with session_scope(deps.session_factory) as session:
            try:
                operations = service.list_operations(
                    session,
                    principal_id=_resolve_principal_id(deps),
                    # v1: the legacy exact-match filter, unchanged. v2: a real
                    # environment argument, standard-resolved the same way every
                    # other v2 tool resolves one (MCP_TOOLS.md section 5.9).
                    environment=environment if deps.enable_v2 else deps.environment,
                    workflow_id=workflow_id,
                    states=state,
                    since=since_dt,
                    limit=limit,
                    cursor=cursor,
                    enable_v2=deps.enable_v2,
                )
            except OperatorError as exc:
                return _error_result(exc)
        items = [
            {
                "operation_id": op.id,
                "workflow_id": op.workflow_id,
                "state": op.state,
                "created_at": _iso(op.created_at),
                "state_changed_at": _iso(op.updated_at),
                **({"environment": op.environment} if deps.enable_v2 else {}),
            }
            for op in operations
        ]
        next_cursor = operations[-1].id if len(operations) == limit else None
        return {"operations": items, "next_cursor": next_cursor}

    return _build_tool(
        name="list_operations",
        description=(
            "Filterable history of operations for this principal — the model's memory "
            "of what it has already done."
        ),
        args_model=ListOperationsArgs,
        handler=handler,
        annotations=_READ_ONLY,
    )


# ======================================================================================
# get_execution_result
# ======================================================================================


class GetExecutionResultArgs(_ToolArgs):
    operation_id: str


def _make_get_execution_result(deps: ToolDeps) -> Tool:
    async def handler(operation_id: str) -> dict[str, Any]:
        with session_scope(deps.session_factory) as session:
            try:
                operation = service.get_operation(
                    session,
                    operation_id=operation_id,
                    principal_id=_resolve_principal_id(deps),
                    enable_v2=deps.enable_v2,
                )
                result = service.get_execution_result(
                    session,
                    operation_id=operation_id,
                    principal_id=_resolve_principal_id(deps),
                    enable_v2=deps.enable_v2,
                )
            except OperatorError as exc:
                return _error_result(exc)
            truncated = bool(_latest_event_detail(session, operation_id).get("truncated", False))
        if result.status == "error":
            error_result: dict[str, Any] = {
                "operation_id": operation.id,
                "state": operation.state,
                "error": result.error,
                "truncated": truncated,
            }
            if deps.enable_v2:
                error_result["environment"] = operation.environment
            return error_result
        ok_result: dict[str, Any] = {
            "operation_id": operation.id,
            "state": operation.state,
            "status": result.status,
            "started_at": _iso(result.started_at),
            "finished_at": _iso(result.finished_at),
            "result": result.redacted_payload,
            "truncated": truncated,
        }
        if deps.enable_v2:
            ok_result["environment"] = operation.environment
        return ok_result

    return _build_tool(
        name="get_execution_result",
        description="The redacted, size-capped result of a completed operation.",
        args_model=GetExecutionResultArgs,
        handler=handler,
        annotations=_READ_ONLY,
    )


# ======================================================================================
# get_execution_log
# ======================================================================================


class GetExecutionLogArgs(_ToolArgs):
    operation_id: str
    include_node_data: bool = False


def _make_get_execution_log(deps: ToolDeps) -> Tool:
    async def handler(operation_id: str, include_node_data: bool = False) -> dict[str, Any]:
        with session_scope(deps.session_factory) as session:
            try:
                operation = service.get_operation(
                    session,
                    operation_id=operation_id,
                    principal_id=_resolve_principal_id(deps),
                    enable_v2=deps.enable_v2,
                )
                result = service.get_execution_result(
                    session,
                    operation_id=operation_id,
                    principal_id=_resolve_principal_id(deps),
                    enable_v2=deps.enable_v2,
                )
                # Not re-authorized against the caller's own workflow-scope here:
                # `get_operation` above already proved the caller may see this exact
                # operation (and therefore its workflow) — a second, independent
                # workflow-scope check against the same workflow_id would only be able
                # to agree or disagree with a decision already made this same request,
                # never add real coverage.
                workflow = service.describe_workflow(session, workflow_id=operation.workflow_id)
            except OperatorError as exc:
                return _error_result(exc)
            truncated = bool(_latest_event_detail(session, operation_id).get("truncated", False))
        nodes: list[dict[str, Any]] = []
        failed_node: str | None = None
        # `include_node_data` is the caller's request; the registry's own
        # `output.include_node_trace` policy is what actually gates it — requesting it
        # against a workflow that doesn't allow it is never an error (MCP_TOOLS.md 2.12).
        if workflow.output.include_node_trace and result.node_trace:
            nodes = result.node_trace.get("nodes", [])
            failed_node = result.node_trace.get("failed_node")
        log_result: dict[str, Any] = {
            "operation_id": operation.id,
            "state": operation.state,
            "nodes": nodes,
            "failed_node": failed_node,
            "truncated": truncated,
        }
        if deps.enable_v2:
            log_result["environment"] = operation.environment
        return log_result

    return _build_tool(
        name="get_execution_log",
        description=(
            "A redacted structural trace for debugging: node names, order, per-node "
            "status, and the failure point."
        ),
        args_model=GetExecutionLogArgs,
        handler=handler,
        annotations=_READ_ONLY,
    )


# ======================================================================================
# whoami (v2 — stage 02, MCP_TOOLS.md section 5.1)
# ======================================================================================


class WhoAmIArgs(_ToolArgs):
    pass


def _make_whoami(deps: ToolDeps) -> Tool:
    async def handler() -> dict[str, Any]:
        principal_id = _resolve_principal_id(deps)
        with session_scope(deps.session_factory) as session:
            principal = PrincipalRepository(session).get(principal_id)
            if principal is None:  # pragma: no cover - defensive; an authenticated
                # caller's principal_id always names a real row (JIT-provisioned or
                # dev/service-configured) by the time a handler runs
                return _error_result(OperatorError(details={"principal_id": principal_id}))
            who = build_whoami(session, principal)
        return {
            "principal_id": who.principal_id,
            "kind": who.kind,
            "display_name": who.display_name,
            "organizations": [
                {
                    "organization_id": org.organization_id,
                    "name": org.name,
                    "roles": org.roles,
                    "environments": [
                        {
                            "environment_id": env.environment_id,
                            "name": env.name,
                            "is_production": env.is_production,
                        }
                        for env in org.environments
                    ],
                }
                for org in who.organizations
            ],
        }

    return _build_tool(
        name="whoami",
        description=(
            "Resolved identity: who the caller is, and every organization, role set, "
            "and environment they can see. The one tool a caller needs before naming "
            "anything else."
        ),
        args_model=WhoAmIArgs,
        handler=handler,
        annotations=_READ_ONLY,
    )


# ======================================================================================
# list_environments (v2 — stage 04, MCP_TOOLS.md section 5.9)
# ======================================================================================


class ListEnvironmentsArgs(_ToolArgs):
    pass


def _make_list_environments(deps: ToolDeps) -> Tool:
    async def handler() -> dict[str, Any]:
        principal_id = _resolve_principal_id(deps)
        with session_scope(deps.session_factory) as session:
            try:
                environments = service.list_environments(session, principal_id=principal_id)
            except OperatorError as exc:
                return _error_result(exc)
        return {
            "environments": [
                {
                    "environment_id": env.environment_id,
                    "organization_id": env.organization_id,
                    "name": env.name,
                    "is_production": env.is_production,
                    "archived": env.archived,
                    "approval_policy_summary": env.approval_policy_summary,
                }
                for env in environments
            ]
        }

    return _build_tool(
        name="list_environments",
        description=(
            "Every environment visible to this caller, with a safe approval-policy "
            "summary. No instance URL, workflow ID, or secret reference is ever "
            "returned. The tool to call before naming an environment anywhere else."
        ),
        args_model=ListEnvironmentsArgs,
        handler=handler,
        annotations=_READ_ONLY,
    )


# ======================================================================================
# request_approval (v2 — stage 05, MCP_TOOLS.md section 5.3)
# ======================================================================================


class RequestApprovalArgs(_ToolArgs):
    operation_id: str
    approvers: list[str] | None = None
    message: str | None = None


def _make_request_approval(deps: ToolDeps) -> Tool:
    async def handler(
        operation_id: str, approvers: list[str] | None = None, message: str | None = None
    ) -> dict[str, Any]:
        principal_id = _resolve_principal_id(deps)
        assert deps.notification_sink is not None  # only registered when enable_v2
        with session_scope(deps.session_factory) as session:
            try:
                result = service.request_approval(
                    session,
                    operation_id=operation_id,
                    principal_id=principal_id,
                    sink=deps.notification_sink,
                    approvers=approvers,
                    message=message,
                    enable_v2=deps.enable_v2,
                )
            except OperatorError as exc:
                return _error_result(exc)
        return {
            "operation_id": result.operation_id,
            "quorum_count": result.quorum_count,
            "approval_policy_snapshot": result.approval_policy_snapshot,
            "notified": result.notified,
            "state": result.state,
        }

    return _build_tool(
        name="request_approval",
        description=(
            "Route a pending operation's approval to its eligible approvers and send "
            "notifications. Routing only — this tool can never approve, choose a "
            "weaker quorum, or add an approver outside the operation's own "
            "approval-policy snapshot."
        ),
        args_model=RequestApprovalArgs,
        handler=handler,
        annotations=ToolAnnotations(
            read_only_hint=False, idempotent_hint=True, open_world_hint=True
        ),
    )


# ======================================================================================
# get_approval_status (v2 — stage 05, MCP_TOOLS.md section 5.4)
# ======================================================================================


class GetApprovalStatusArgs(_ToolArgs):
    operation_id: str


def _make_get_approval_status(deps: ToolDeps) -> Tool:
    async def handler(operation_id: str) -> dict[str, Any]:
        principal_id = _resolve_principal_id(deps)
        with session_scope(deps.session_factory) as session:
            try:
                status = service.get_approval_status(
                    session,
                    operation_id=operation_id,
                    principal_id=principal_id,
                    enable_v2=deps.enable_v2,
                )
            except OperatorError as exc:
                return _error_result(exc)
        return {
            "operation_id": status.operation_id,
            "quorum_count": status.quorum_count,
            "approval_policy_snapshot": status.approval_policy_snapshot,
            "decisions": [
                {
                    "principal_id": d.principal_id,
                    "decision": d.decision,
                    "decided_at": _iso(d.decided_at),
                }
                for d in status.decisions
            ],
            "outstanding": status.outstanding,
            "ready": status.ready,
        }

    return _build_tool(
        name="get_approval_status",
        description=(
            "A scoped, redacted read of a pending operation's approval quorum: who has "
            "decided, who is still outstanding, and whether quorum is reached."
        ),
        args_model=GetApprovalStatusArgs,
        handler=handler,
        annotations=_READ_ONLY,
    )


def build_tools(deps: ToolDeps) -> list[Tool]:
    """Every v1 tool, bound to ``deps`` — the list ``mcp/server.py`` hands to
    ``MCPServer(tools=...)``. Exactly BUILD_PLAN section 7.1's twelve; a contract test
    (``tests/contract/test_mcp_tool_inventory.py``) asserts this list's names against
    that inventory in both directions. ``whoami`` (BUILD_PLAN section 7.2),
    ``list_environments`` (stage 04), ``request_approval``, and ``get_approval_status``
    (stage 05) are appended, a thirteenth through sixteenth tool, only when
    ``deps.enable_v2`` is set — v1's exact twelve-tool surface (AC-23) is otherwise
    untouched."""
    tools = [
        _make_list_workflows(deps),
        _make_describe_workflow(deps),
        _make_get_instance_health(deps),
        _make_validate_input(deps),
        _make_preflight_workflow(deps),
        _make_prepare_operation(deps),
        _make_get_operation(deps),
        _make_execute_operation(deps),
        _make_cancel_operation(deps),
        _make_list_operations(deps),
        _make_get_execution_result(deps),
        _make_get_execution_log(deps),
    ]
    if deps.enable_v2:
        tools.append(_make_whoami(deps))
        tools.append(_make_list_environments(deps))
        tools.append(_make_request_approval(deps))
        tools.append(_make_get_approval_status(deps))
    return tools
