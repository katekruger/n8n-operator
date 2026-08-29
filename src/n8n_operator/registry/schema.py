"""Pydantic v2 models for the registry document.

Mirrors BUILD_PLAN sections 6.1 through 6.5: document shape, workflow entries,
``trigger``, ``output``, and ``limits``. Every model here is **structurally** typed
only — required-ness, basic field types, and closed enumerations (``Literal``) are
enforced by Pydantic itself. The cross-field and cross-entry *semantic* rules R1
through R12 (BUILD_PLAN section 6.6) are deliberately **not** implemented as Pydantic
validators: they are checked explicitly in ``registry/loader.py`` so that every
violation is reported under its own rule number with uniform, predictable phrasing,
rather than a mix of Pydantic's own error text and this codebase's.

Every model is frozen: a loaded or resolved registry value is never mutated in place.
:func:`resolve_workflow_entry` returns a **new** entry with defaults merged in; it does
not modify the one it was given.

Also defines the public, response-shaping projections (:class:`WorkflowSummary`,
:class:`WorkflowDetail`) that ``list_workflows``/``describe_workflow`` will use in a
later phase: their field sets are an explicit allowlist that structurally cannot carry
``n8n_workflow_id``, ``trigger`` (and therefore no ``secret_ref``), or any URL — the
fields simply do not exist on these classes (ADR-006, boundary B5).

Phase 2 (BUILD_PLAN section 12).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------------------
# Document shape (BUILD_PLAN section 6.1)
# --------------------------------------------------------------------------------------


class RegistryMetadata(BaseModel):
    """``metadata`` block: identifies the registry itself, for logs and audit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str | None = None


class RegistryDefaults(BaseModel):
    """``defaults`` block: per-workflow fields fall back to these when unset."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    approval: Literal["none", "required"] = "required"
    timeout_seconds: int = 60
    approval_ttl_seconds: int = 900
    execution_ttl_seconds: int = 300


# --------------------------------------------------------------------------------------
# Workflow entry sub-objects (sections 6.3, 6.4, 6.5)
# --------------------------------------------------------------------------------------


class Trigger(BaseModel):
    """How Operator invokes the workflow (BUILD_PLAN section 6.3).

    ``secret_ref`` is required whenever ``auth`` is not ``"none"`` — checked as rule R6
    in ``registry/loader.py``, not here, since R6 also verifies the value is an
    *indirect* reference (``env:``/``keyring:``), not merely present.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["webhook"]  # v1 supports webhook only (api is reserved for v2)
    method: Literal["POST", "GET"]
    path: str
    auth: Literal["none", "header", "basic"]
    secret_ref: str | None = None
    correlation: Literal["none", "response_envelope"] = "none"


class Output(BaseModel):
    """Redaction and shaping of results (BUILD_PLAN section 6.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    redact: list[str] = Field(default_factory=list)
    max_bytes: int = 65536
    include_node_trace: bool = False


class Limits(BaseModel):
    """Per-workflow limits (BUILD_PLAN section 6.5).

    ``timeout_seconds``, ``approval_ttl_seconds``, and ``execution_ttl_seconds`` are
    ``None`` when the entry does not override ``defaults`` — resolved by
    :func:`resolve_workflow_entry`, never left ``None`` in a loaded, active registry.
    ``max_concurrent`` and ``rate_limit_per_minute`` have no ``defaults``-block
    equivalent and carry their own concrete defaults directly. ``max_argument_bytes``
    stays ``None`` to mean "use the server ceiling" — resolved per-operation, not at
    registry-load time, since the server ceiling is a deployment setting, not a
    property of the registry document itself (ADR-011).

    ``quorum_count`` (stage 05, ADR-017) is the N in N-of-M team approval — always
    concrete like ``max_concurrent``, never ``None``, since "how many distinct
    approvers" is meaningful even for a workflow that never overrides it (v1's own
    single-approver behavior *is* ``quorum_count: 1``, not a different code path).
    Meaningless when ``approval: none`` (such a workflow never enters
    ``PENDING_APPROVAL`` to begin with) — rule R15 rejects a registry entry that sets
    both. Strengthen-only under an environment overlay, same direction as
    ``approval_ttl_seconds`` (raise only — more distinct approvers is the stricter
    direction, ADR-016).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout_seconds: int | None = None
    approval_ttl_seconds: int | None = None
    execution_ttl_seconds: int | None = None
    max_concurrent: int = 1
    rate_limit_per_minute: int | None = None
    max_argument_bytes: int | None = None
    quorum_count: int = Field(default=1, ge=1)


# --------------------------------------------------------------------------------------
# Workflow entry (section 6.2)
# --------------------------------------------------------------------------------------


class WorkflowEntry(BaseModel):
    """One registered workflow (BUILD_PLAN section 6.2).

    ``approval`` is ``None`` when the entry relies on ``defaults.approval`` — resolved
    by :func:`resolve_workflow_entry`. A *resolved* entry (the only kind that reaches
    storage or a response) always has a concrete ``approval`` value.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    n8n_workflow_id: str
    title: str
    description: str
    owner: str
    version: int
    definition_hash: str
    risk: Literal["low", "medium", "high"]
    side_effects: Literal["read_only", "external_write", "irreversible"]
    approval: Literal["none", "required"] | None = None
    trigger: Trigger
    input_schema: dict[str, Any]
    output: Output = Field(default_factory=Output)
    limits: Limits = Field(default_factory=Limits)
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True


class RegistryDocument(BaseModel):
    """The whole registry file (BUILD_PLAN section 6.1).

    ``api_version`` is aliased from the YAML key ``apiVersion`` — the one field in this
    schema that is not already snake_case in the source document.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    api_version: str = Field(alias="apiVersion")
    metadata: RegistryMetadata
    defaults: RegistryDefaults = Field(default_factory=RegistryDefaults)
    workflows: list[WorkflowEntry] = Field(default_factory=list)


def resolve_workflow_entry(entry: WorkflowEntry, defaults: RegistryDefaults) -> WorkflowEntry:
    """The entry with every ``defaults``-eligible field resolved to a concrete value.

    Returns a **new** :class:`WorkflowEntry`; ``entry`` itself is never mutated (every
    model in this module is frozen). Only ``approval`` and the three ``limits`` fields
    that have a ``defaults`` counterpart are touched — ``limits.max_concurrent``,
    ``limits.rate_limit_per_minute``, and ``limits.max_argument_bytes`` are untouched,
    since none of them has a ``defaults``-block equivalent to inherit from.
    """
    resolved_limits = entry.limits.model_copy(
        update={
            "timeout_seconds": entry.limits.timeout_seconds
            if entry.limits.timeout_seconds is not None
            else defaults.timeout_seconds,
            "approval_ttl_seconds": entry.limits.approval_ttl_seconds
            if entry.limits.approval_ttl_seconds is not None
            else defaults.approval_ttl_seconds,
            "execution_ttl_seconds": entry.limits.execution_ttl_seconds
            if entry.limits.execution_ttl_seconds is not None
            else defaults.execution_ttl_seconds,
        }
    )
    return entry.model_copy(
        update={
            "approval": entry.approval if entry.approval is not None else defaults.approval,
            "limits": resolved_limits,
        }
    )


# --------------------------------------------------------------------------------------
# Public response shaping (boundary B5) — the allowlist a future list_workflows /
# describe_workflow builds its result from. Neither model below has a field capable of
# carrying an n8n workflow ID, a secret reference, or a URL.
# --------------------------------------------------------------------------------------


class WorkflowSummary(BaseModel):
    """The ``list_workflows`` per-entry shape (MCP_TOOLS.md section 2.1)."""

    model_config = ConfigDict(frozen=True)

    workflow_id: str
    title: str
    description: str
    risk: Literal["low", "medium", "high"]
    side_effects: Literal["read_only", "external_write", "irreversible"]
    approval: Literal["none", "required"]
    tags: list[str]
    owner: str
    version: int

    @classmethod
    def from_entry(cls, entry: WorkflowEntry) -> WorkflowSummary:
        """``entry`` must already be resolved (:func:`resolve_workflow_entry`) — a
        summary always carries a concrete ``approval``, never ``None``."""
        if entry.approval is None:
            raise ValueError("WorkflowSummary requires a resolved entry (approval is None)")
        return cls(
            workflow_id=entry.id,
            title=entry.title,
            description=entry.description,
            risk=entry.risk,
            side_effects=entry.side_effects,
            approval=entry.approval,
            tags=list(entry.tags),
            owner=entry.owner,
            version=entry.version,
        )


class WorkflowDetailOutput(BaseModel):
    """``describe_workflow``'s ``output`` block. ``redacted_paths`` is a **count**, not
    the paths themselves — publishing them would tell an attacker exactly which fields
    are worth attacking (MCP_TOOLS.md section 2.2)."""

    model_config = ConfigDict(frozen=True)

    max_bytes: int
    include_node_trace: bool
    redacted_paths: int


class WorkflowDetailLimits(BaseModel):
    """``describe_workflow``'s ``limits`` block, fully resolved.

    Includes ``max_argument_bytes`` — added here as part of Phase 2, correcting a gap
    in MCP_TOOLS.md's own example (which predates ADR-011's addition of this field);
    see the phase-2 documentation update in ``docs/MCP_TOOLS.md`` section 2.2.
    """

    model_config = ConfigDict(frozen=True)

    timeout_seconds: int
    approval_ttl_seconds: int
    execution_ttl_seconds: int
    max_concurrent: int
    rate_limit_per_minute: int | None
    max_argument_bytes: int | None


class WorkflowDetail(BaseModel):
    """The ``describe_workflow`` shape (MCP_TOOLS.md section 2.2)."""

    model_config = ConfigDict(frozen=True)

    workflow_id: str
    title: str
    description: str
    owner: str
    version: int
    risk: Literal["low", "medium", "high"]
    side_effects: Literal["read_only", "external_write", "irreversible"]
    approval: Literal["none", "required"]
    tags: list[str]
    input_schema: dict[str, Any]
    output: WorkflowDetailOutput
    limits: WorkflowDetailLimits

    @classmethod
    def from_entry(cls, entry: WorkflowEntry) -> WorkflowDetail:
        """``entry`` must already be resolved (:func:`resolve_workflow_entry`)."""
        if entry.approval is None:
            raise ValueError("WorkflowDetail requires a resolved entry (approval is None)")
        limits = entry.limits
        if (
            limits.timeout_seconds is None
            or limits.approval_ttl_seconds is None
            or limits.execution_ttl_seconds is None
        ):
            raise ValueError("WorkflowDetail requires resolved limits")
        return cls(
            workflow_id=entry.id,
            title=entry.title,
            description=entry.description,
            owner=entry.owner,
            version=entry.version,
            risk=entry.risk,
            side_effects=entry.side_effects,
            approval=entry.approval,
            tags=list(entry.tags),
            input_schema=entry.input_schema,
            output=WorkflowDetailOutput(
                max_bytes=entry.output.max_bytes,
                include_node_trace=entry.output.include_node_trace,
                redacted_paths=len(entry.output.redact),
            ),
            limits=WorkflowDetailLimits(
                timeout_seconds=limits.timeout_seconds,
                approval_ttl_seconds=limits.approval_ttl_seconds,
                execution_ttl_seconds=limits.execution_ttl_seconds,
                max_concurrent=limits.max_concurrent,
                rate_limit_per_minute=limits.rate_limit_per_minute,
                max_argument_bytes=limits.max_argument_bytes,
            ),
        )


# --------------------------------------------------------------------------------------
# Environment overlays (stage 04, ADR-016). One overlay document per environment,
# authored the same way the base registry is (a YAML file, loaded through the same
# read -> parse -> validate -> hash shape as `load_registry`), applied against the
# *current* active base snapshot via `n8n-operator environment set-overlay`.
#
# An overlay may only adjust *how a workflow is reached and how strongly it is
# gated* — never what it promises to do or accept (BUILD_PLAN section 8.3, ADR-016
# section 1, rules R13/R14). There is deliberately no `input_schema`/`side_effects`/
# `risk`/`title`/`description`/`tags` field on `WorkflowOverlayEntry` at all — R13 is
# therefore enforced structurally by this model's own `extra="forbid"` shape for any
# field name, not only for those six specifically; `registry/loader.py`'s R13
# rule-check exists for the cross-entry checks a single model's shape can't express
# (e.g. an overlay naming a `workflow_id` absent from the base registry).
# --------------------------------------------------------------------------------------


class WorkflowOverlayEntry(BaseModel):
    """One workflow's environment-specific override (BUILD_PLAN section 8.3, ADR-016
    section 1). Every field is optional; an unset field means "inherit the base entry
    unchanged" — an environment with no overlay for a given workflow, or an overlay
    that only touches `trigger_path`, leaves everything else exactly as the base
    registry declares it.

    ``approval_override`` may only ever be ``"required"`` (never ``"none"``) — the
    Literal type itself makes "weaken toward `none`" a schema-validation failure, not
    a semantic rule this module has to check (rule R14's approval half is therefore
    structurally guaranteed here; only the `limits_override` numeric-direction half
    needs an explicit rule-check in `registry/loader.py`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_id: str
    n8n_workflow_id: str | None = None
    definition_hash: str | None = None
    trigger_path: str | None = None
    trigger_secret_ref: str | None = None
    approval_override: Literal["required"] | None = None
    limits_override: dict[str, int] | None = None


class EnvironmentOverlayMetadata(BaseModel):
    """``metadata`` block: which environment this overlay document targets, for logs
    and audit — the environment ID itself is supplied separately, by the CLI
    invocation (``--env``), never trusted from inside the file (the same "identity is
    never self-asserted" discipline applied to the file's own content)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str | None = None


class EnvironmentOverlayDocument(BaseModel):
    """The whole overlay file — one per environment, structurally parallel to
    :class:`RegistryDocument`."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    api_version: str = Field(alias="apiVersion")
    metadata: EnvironmentOverlayMetadata
    overlays: list[WorkflowOverlayEntry] = Field(default_factory=list)


def resolve_overlay(
    base_entry: WorkflowEntry, overlay: WorkflowOverlayEntry | None
) -> WorkflowEntry:
    """The environment-resolved entry: ``base_entry`` (already resolved via
    :func:`resolve_workflow_entry`) with ``overlay``'s fields applied on top,
    field-by-field, wherever the overlay sets one. ``overlay=None`` returns
    ``base_entry`` itself unchanged — "no overlay" and "an overlay that touches
    nothing" are the same outcome by construction.

    Applied straightforwardly (the overlay's value replaces the base's, never merged
    or re-validated here) — the "strengthen-only" direction is enforced once, at
    overlay *load* time (``registry/loader.py``'s R14 rule-check, against the base
    entry active at that moment), not re-checked on every resolution. A later edit to
    the *base* registry that would make an already-loaded overlay's value no longer a
    strict strengthening relative to the *new* base is a known, accepted edge case —
    see THREAT_MODEL.md's stage 04 delta.
    """
    if overlay is None:
        return base_entry

    resolved_trigger = base_entry.trigger
    trigger_updates: dict[str, Any] = {}
    if overlay.trigger_path is not None:
        trigger_updates["path"] = overlay.trigger_path
    if overlay.trigger_secret_ref is not None:
        trigger_updates["secret_ref"] = overlay.trigger_secret_ref
    if trigger_updates:
        resolved_trigger = resolved_trigger.model_copy(update=trigger_updates)

    resolved_limits = base_entry.limits
    if overlay.limits_override:
        resolved_limits = resolved_limits.model_copy(update=dict(overlay.limits_override))

    updates: dict[str, Any] = {"trigger": resolved_trigger, "limits": resolved_limits}
    if overlay.n8n_workflow_id is not None:
        updates["n8n_workflow_id"] = overlay.n8n_workflow_id
    if overlay.definition_hash is not None:
        updates["definition_hash"] = overlay.definition_hash
    if overlay.approval_override is not None:
        updates["approval"] = overlay.approval_override
    return base_entry.model_copy(update=updates)


__all__ = [
    "EnvironmentOverlayDocument",
    "EnvironmentOverlayMetadata",
    "Limits",
    "Output",
    "RegistryDefaults",
    "RegistryDocument",
    "RegistryMetadata",
    "Trigger",
    "WorkflowDetail",
    "WorkflowDetailLimits",
    "WorkflowDetailOutput",
    "WorkflowEntry",
    "WorkflowOverlayEntry",
    "WorkflowSummary",
    "resolve_overlay",
    "resolve_workflow_entry",
]
