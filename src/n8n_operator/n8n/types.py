"""Typed models for n8n API responses.

Responses are parsed into typed models; a parse failure yields a structured error
(``ProviderError``) rather than an unhandled exception (threat T-32). n8n output is
untrusted input (ARCHITECTURE.md section 5).

**Structural validation only — not the canonicalization input.** ``WorkflowDefinition``
below confirms a ``GET /workflows/{id}`` response has the minimum shape ``n8n/client.py``'s
callers need (an ``id``, a ``nodes`` list, a ``connections`` mapping) and nothing more; it
uses ``extra="allow"`` so every field n8n actually sent survives on the model, but
``n8n/canonicalization.py`` deliberately canonicalizes the **raw parsed JSON dict**
``n8n/client.py`` also returns, not a value reconstructed through this model. Rule CAN-01
("an unrecognized or newly-introduced field is included, never silently dropped") is a
security property; depending on a specific Pydantic model's round-trip fidelity to prove
it, forever, across every future n8n release, is not a bet this codebase makes when the
raw dict is sitting right there.

Every model here mirrors a shape confirmed empirically against a live instance —
see ``docs/N8N_COMPATIBILITY.md``, not assumed from n8n's public documentation alone.

Phase 4 (BUILD_PLAN section 12).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "ExecutionSummary",
    "HealthStatus",
    "ResponseEnvelope",
    "WorkflowDefinition",
]


class HealthStatus(BaseModel):
    """``GET /healthz`` — unauthenticated, used for the ``instance_reachable`` preflight
    check (docs/N8N_COMPATIBILITY.md section 10)."""

    model_config = ConfigDict(extra="allow")

    status: str


class WorkflowDefinition(BaseModel):
    """``GET /api/v1/workflows/{id}`` — the minimum shape callers need.

    ``activeVersion`` is deliberately **not** modeled: docs/N8N_COMPATIBILITY.md section 5
    found it is ``null`` whenever the workflow happens to be inactive, so nothing here may
    depend on it existing. ``nodes``/``connections``/``settings``/``pinData`` are the
    top-level fields that survive regardless of active state, and are what
    ``n8n/canonicalization.py`` operates on.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    active: bool
    nodes: list[dict[str, Any]]
    connections: dict[str, Any]
    settings: dict[str, Any] = Field(default_factory=dict)
    pinData: dict[str, Any] = Field(default_factory=dict)  # noqa: N815 - matches n8n's own JSON field name verbatim

    @field_validator("settings", "pinData", mode="before")
    @classmethod
    def _null_becomes_empty(cls, value: Any) -> Any:
        """n8n returns these as an explicit JSON ``null``, not an omitted key, when a
        workflow has never had one set — confirmed empirically against a real 2.35.7
        instance (a ``Field(default_factory=...)`` only applies when the key is
        *absent*, not when it is present with a ``null`` value, so without this the
        field would fail validation outright)."""
        return {} if value is None else value


class ExecutionSummary(BaseModel):
    """``GET /api/v1/executions/{id}`` — deliberately **not** the full response.

    n8n's own execution detail includes ``data.resultData.runData``: every node's full
    input and output, including a webhook trigger node's raw inbound request (headers,
    query, body verbatim — docs/N8N_COMPATIBILITY.md section 8). That tree is never
    modeled here and never leaves ``n8n/client.py`` as a return value; only the fields
    ``core.service.record_execution_outcome`` actually needs are. Redaction and size
    capping happen exactly once, in ``core/redaction.py`` — an adapter-side model that
    also carried the raw tree would be a second, unaudited copy of that same policy.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    finished: bool
    mode: str
    status: Literal["success", "error", "running", "waiting", "canceled", "crashed", "new"]
    started_at: str | None = Field(default=None, alias="startedAt")
    stopped_at: str | None = Field(default=None, alias="stoppedAt")
    workflow_id: str = Field(alias="workflowId")


class ResponseEnvelope(BaseModel):
    """The documented Operator response envelope (ADR-009 section 2):

    ``{"n8n_operator": {"execution_id": "..."}, "data": {...}}``

    Parsed from a webhook's own HTTP response body — not from the n8n API — when a
    registry entry declares ``trigger.correlation: response_envelope``. Absence or
    malformation is not an error on its own: ``n8n/client.py``'s dispatch path treats a
    missing or malformed envelope as "no correlation available", classifying the
    dispatch outcome on its own merits (ADR-009 section 2 — "a workflow that returns a
    result is not broken because its envelope is").
    """

    model_config = ConfigDict(extra="allow")

    n8n_operator: dict[str, Any] = Field(default_factory=dict)
    data: Any = None

    @property
    def execution_id(self) -> str | None:
        value = self.n8n_operator.get("execution_id")
        return str(value) if value is not None else None
