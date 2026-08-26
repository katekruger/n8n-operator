"""Preflight checks: reachable, exists, active, unchanged, credentialed.

Runs before an operation is offered for approval, and the definition-hash check runs
*again* at execute time — approval and execution are separated in time, so a workflow
modified in between cannot run under the old approval (boundary B8, AC-13).

Check codes are enumerated in ``docs/MCP_TOOLS.md`` section 2.5. Statuses are ``pass``,
``fail``, ``skipped``, and the two non-blocking statuses from ADR-009 — ``warn`` and
``unverifiable``. **Only ``fail`` produces BLOCKED.**

Two honesty constraints from ADR-009 apply here:

* credential checks report whether a credential is **bound**, never whether it is valid;
  validity is ``unverifiable`` absent a supported n8n mechanism that tests it — and this
  codebase tried the one n8n exposes (``POST /credentials/{id}/test``) and found it
  unreliable even for a common credential type (docs/N8N_COMPATIBILITY.md section 9),
  so this is not merely a cautious default, it is a tested one; and
* a workflow declaring ``correlation: none`` gets a ``warn`` so the reduced reconciliation
  capability is visible before an approver decides, not during an incident (threat T-40).

**Unavailable checks stay ``unverifiable`` or ``skipped``, never ``pass``.** A check this
module cannot actually run — no supported version-detection endpoint
(docs/N8N_COMPATIBILITY.md section 10), a prior check's failure making a later one
meaningless — is never silently reported as passing.

Definition canonicalization is conservative: inclusion by default, exclusion only on
harness evidence (CAN-01 through CAN-07, ADR-008, ``n8n/canonicalization.py``).

Phase 4 (BUILD_PLAN section 12).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from n8n_operator.errors import (
    DefinitionDriftError,
    InstanceUnreachableError,
    MissingNodeCredentialsError,
    ProviderError,
    WorkflowInactiveError,
    WorkflowMissingOnInstanceError,
)
from n8n_operator.n8n.canonicalization import compute_definition_hash
from n8n_operator.n8n.client import N8nClient

__all__ = ["N8nPreflight", "PreflightCheck", "PreflightResult", "WorkflowLike"]

_SKIPPED_DETAIL = "Not evaluated after a prior failure."
_CREDENTIAL_VALIDITY_DETAIL = "Operator verifies that credentials are bound, not that they work."
_WEBHOOK_NODE_TYPE = "n8n-nodes-base.webhook"


class _TriggerLike(Protocol):
    @property
    def path(self) -> str: ...
    @property
    def method(self) -> str: ...
    @property
    def correlation(self) -> str: ...


class WorkflowLike(Protocol):
    """The exact shape this module needs from a registry workflow entry —
    ``registry.schema.WorkflowEntry`` satisfies this structurally. Defined locally,
    rather than importing ``WorkflowEntry`` itself, because capability packages must
    not depend on each other (ARCHITECTURE.md section 2.1): ``n8n/`` and ``registry/``
    are both capability packages, so this is duck typing by construction, not by
    convention — there is no import here for a future refactor to accidentally lean on.

    Every member is a read-only ``@property``, not a plain attribute: mypy checks a
    Protocol's plain attributes *invariantly* (a match must be exactly the same type,
    since a caller could in principle reassign it), which would reject
    ``WorkflowEntry.approval``'s narrower ``Literal["none", "required"] | None`` against
    this Protocol's ``str | None`` even though the real type is a valid ``str`` at
    runtime. A read-only property is checked *covariantly* instead — the real, narrower
    type is exactly what a read-only view is supposed to accept.
    """

    @property
    def n8n_workflow_id(self) -> str: ...
    @property
    def definition_hash(self) -> str: ...
    @property
    def approval(self) -> str | None: ...
    @property
    def side_effects(self) -> str: ...
    @property
    def trigger(self) -> _TriggerLike: ...


@dataclass(frozen=True)
class PreflightCheck:
    """Structurally identical to ``core.models.PreflightCheck`` (same field names and
    types), deliberately not imported from there for the same reason as
    :class:`WorkflowLike` above. ``core.service.PreflightPort`` only ever accesses
    these fields by attribute, never by ``isinstance`` — Python's duck typing means a
    value shaped like this satisfies that protocol without inheriting from anything
    ``core/`` defines.
    """

    check: str
    status: str
    code: str | None = None
    detail: Any | None = None


@dataclass(frozen=True)
class PreflightResult:
    """Structurally identical to ``core.models.PreflightResult`` — see
    :class:`PreflightCheck`'s docstring for why this is a parallel definition rather
    than an import."""

    ready: bool
    checks: list[PreflightCheck] = field(default_factory=list)
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class N8nPreflight:
    """The real preflight adapter — satisfies ``core.service.PreflightPort``
    structurally (a ``check(workflow) -> PreflightResult``-shaped method), without
    ``core/`` ever importing this module or ``n8n/`` ever importing ``core/``
    (ARCHITECTURE.md section 2.1: ``core`` may depend on ``n8n/``, the reverse
    direction is what's forbidden). Phase 3 exercised the protocol against a fake; this
    is the implementation Phase 4 was building toward.
    """

    def __init__(
        self,
        client: N8nClient,
        *,
        supported_api_versions: frozenset[str] | None = None,
    ) -> None:
        self._client = client
        self._supported_api_versions = supported_api_versions

    def check(self, workflow: WorkflowLike) -> PreflightResult:
        checks: list[PreflightCheck] = []

        if not self._instance_reachable(checks):
            checks.append(self._skip("compatible_version"))
            checks.append(self._skip("workflow_exists"))
            checks.append(self._skip("workflow_active"))
            checks.append(self._skip("trigger_compatibility"))
            checks.append(self._skip("definition_unchanged"))
            checks.append(self._skip("credential_bindings"))
            checks.append(self._credential_validity_check())
            checks.extend(self._registry_only_checks(workflow))
            return self._result(checks)

        checks.append(self._check_compatible_version())

        live_definition = self._fetch_live_definition(workflow, checks)
        if live_definition is None:
            checks.append(self._skip("workflow_active"))
            checks.append(self._skip("trigger_compatibility"))
            checks.append(self._skip("definition_unchanged"))
            checks.append(self._skip("credential_bindings"))
        else:
            checks.append(self._check_active(live_definition))
            checks.append(self._check_trigger_compatibility(workflow, live_definition))
            checks.append(self._check_definition_unchanged(workflow, live_definition))
            checks.append(self._check_credential_bindings(live_definition))

        checks.append(self._credential_validity_check())
        checks.extend(self._registry_only_checks(workflow))
        return self._result(checks)

    # ------------------------------------------------------------------------------

    def _result(self, checks: list[PreflightCheck]) -> PreflightResult:
        ready = not any(c.status == "fail" for c in checks)
        return PreflightResult(ready=ready, checks=checks, checked_at=datetime.now(UTC))

    def _skip(self, name: str) -> PreflightCheck:
        return PreflightCheck(check=name, status="skipped", detail=_SKIPPED_DETAIL)

    def _instance_reachable(self, checks: list[PreflightCheck]) -> bool:
        try:
            self._client.health_check()
        except ProviderError:
            checks.append(
                PreflightCheck(
                    check="instance_reachable", status="fail", code=InstanceUnreachableError.code
                )
            )
            return False
        checks.append(PreflightCheck(check="instance_reachable", status="pass"))
        return True

    def _check_compatible_version(self) -> PreflightCheck:
        if self._supported_api_versions is None:
            return PreflightCheck(
                check="compatible_version",
                status="unverifiable",
                code="API_VERSION_UNVERIFIED",
                detail="No supported API version set is configured.",
            )
        version = self._client.get_api_version_info()
        if version is None:
            return PreflightCheck(
                check="compatible_version",
                status="unverifiable",
                code="API_VERSION_UNVERIFIED",
                detail="The instance's API version could not be determined.",
            )
        if version in self._supported_api_versions:
            return PreflightCheck(check="compatible_version", status="pass")
        return PreflightCheck(
            check="compatible_version",
            status="warn",
            code="API_VERSION_UNVERIFIED",
            detail={"api_version": version, "supported": sorted(self._supported_api_versions)},
        )

    def _fetch_live_definition(
        self, workflow: WorkflowLike, checks: list[PreflightCheck]
    ) -> dict[str, Any] | None:
        try:
            live_definition = self._client.get_workflow(workflow.n8n_workflow_id)
        except WorkflowMissingOnInstanceError:
            checks.append(
                PreflightCheck(
                    check="workflow_exists", status="fail", code=WorkflowMissingOnInstanceError.code
                )
            )
            return None
        except ProviderError:
            checks.append(
                PreflightCheck(
                    check="workflow_exists", status="fail", code=InstanceUnreachableError.code
                )
            )
            return None
        checks.append(PreflightCheck(check="workflow_exists", status="pass"))
        return live_definition

    def _check_active(self, live_definition: dict[str, Any]) -> PreflightCheck:
        if live_definition.get("active") is True:
            return PreflightCheck(check="workflow_active", status="pass")
        return PreflightCheck(
            check="workflow_active", status="fail", code=WorkflowInactiveError.code
        )

    def _check_trigger_compatibility(
        self, workflow: WorkflowLike, live_definition: dict[str, Any]
    ) -> PreflightCheck:
        """The registry's ``trigger.path`` is the full path including n8n's own
        ``/webhook/`` prefix (BUILD_PLAN section 6.3); the live webhook node's own
        ``parameters.path`` is the bare suffix after that prefix
        (docs/N8N_COMPATIBILITY.md section 2) — confirmed empirically, not assumed.
        """
        for node in live_definition.get("nodes", []):
            if node.get("type") != _WEBHOOK_NODE_TYPE:
                continue
            params = node.get("parameters", {})
            live_path = f"/webhook/{params.get('path', '')}"
            live_method = params.get("httpMethod")
            if live_path == workflow.trigger.path and live_method == workflow.trigger.method:
                return PreflightCheck(check="trigger_compatibility", status="pass")
            return PreflightCheck(
                check="trigger_compatibility",
                status="fail",
                code="TRIGGER_INCOMPATIBLE",
                detail={
                    "registered_path": workflow.trigger.path,
                    "live_path": live_path,
                    "registered_method": workflow.trigger.method,
                    "live_method": live_method,
                },
            )
        return PreflightCheck(
            check="trigger_compatibility",
            status="fail",
            code="TRIGGER_INCOMPATIBLE",
            detail="No webhook trigger node found on the live workflow.",
        )

    def _check_definition_unchanged(
        self, workflow: WorkflowLike, live_definition: dict[str, Any]
    ) -> PreflightCheck:
        live_hash = compute_definition_hash(live_definition)
        if live_hash == workflow.definition_hash:
            return PreflightCheck(check="definition_unchanged", status="pass")
        return PreflightCheck(
            check="definition_unchanged",
            status="fail",
            code=DefinitionDriftError.code,
            detail={"registered": workflow.definition_hash, "live": live_hash},
        )

    def _check_credential_bindings(self, live_definition: dict[str, Any]) -> PreflightCheck:
        """Reports binding **presence**, never validity (see the module docstring).
        A node is treated as declaring a credential requirement when its parameters
        name a ``nodeCredentialType`` (the shape every credentialed core node uses);
        such a node without a matching ``credentials`` entry is reported missing.
        """
        missing: list[dict[str, str]] = []
        for node in live_definition.get("nodes", []):
            params = node.get("parameters", {})
            required_type = params.get("nodeCredentialType")
            if not required_type:
                continue
            bound = required_type in (node.get("credentials") or {})
            if not bound:
                missing.append({"node": node.get("name", "?"), "credential_type": required_type})
        if missing:
            return PreflightCheck(
                check="credential_bindings",
                status="fail",
                code=MissingNodeCredentialsError.code,
                detail={"missing": missing},
            )
        return PreflightCheck(check="credential_bindings", status="pass")

    def _credential_validity_check(self) -> PreflightCheck:
        return PreflightCheck(
            check="credential_validity",
            status="unverifiable",
            code="CREDENTIAL_VALIDITY_UNVERIFIED",
            detail=_CREDENTIAL_VALIDITY_DETAIL,
        )

    def _registry_only_checks(self, workflow: WorkflowLike) -> list[PreflightCheck]:
        """Two checks evaluable from the registry entry alone, needing no live n8n call."""
        checks: list[PreflightCheck] = []
        if workflow.trigger.correlation == "none":
            checks.append(
                PreflightCheck(
                    check="correlation",
                    status="warn",
                    code="NO_EXECUTION_CORRELATION",
                    detail=(
                        "This workflow returns no execution ID. Reconciliation after an "
                        "indeterminate dispatch will be manual."
                    ),
                )
            )
        else:
            checks.append(PreflightCheck(check="correlation", status="pass"))

        if workflow.approval == "none" and workflow.side_effects == "read_only":
            checks.append(
                PreflightCheck(
                    check="unattended_execution",
                    status="warn",
                    code="UNATTENDED_EXECUTION",
                    detail=(
                        "This workflow runs with no human in the loop, on the strength "
                        "of its own approval: none + side_effects: read_only classification."
                    ),
                )
            )
        return checks
