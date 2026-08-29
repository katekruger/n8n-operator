"""The error taxonomy.

One taxonomy, defined normatively in ``docs/MCP_TOOLS.md`` section 4 and implemented
once here. Adapters map these to MCP tool errors, CLI exit codes, or HTTP status
without inventing new codes (ARCHITECTURE section 9).

Every error carries a stable machine-readable ``code``, a human-readable ``message``,
optional structured ``details``, and an advisory ``retryable`` flag which is ``False``
for every side-effect-adjacent failure (ADR-005). ``remediation`` carries the "model's
correct next move" column from the MCP_TOOLS.md table verbatim, since it is the
actionable half of a model-facing error and costs nothing to keep alongside ``message``.

Five categories separate errors by where they originate, matching how callers actually
need to handle them — an adapter deciding whether to retry a provider outage behaves
differently from one deciding whether a caller's request was simply malformed:

* :class:`DomainError` — business-rule and state-machine violations. The request was
  understood; the answer is no, for a reason grounded in policy.
* :class:`AuthorizationError` — capability failures: the caller lacks a valid handle or
  approval for what it is asking to do (ADR-003).
* :class:`ProviderError` — the n8n instance's own state: unreachable, drifted, inactive,
  or an indeterminate dispatch outcome (ADR-009).
* :class:`ConfigurationError` — the operator's own configuration or registry is invalid.
  Never a caller's fault; never retryable by an MCP client.
* :class:`StorageError` — an unexpected failure in the persistence layer. Reserved for
  wrapping lower-level exceptions safely (see :meth:`OperatorError.from_exception`).

**Never serialize exception internals or secrets.** :meth:`OperatorError.to_dict` is the
one sanctioned serialization path, and it emits exactly the four fields above — never a
wrapped exception's own message, its ``args``, or a traceback. :func:`OperatorError.from_exception`
deliberately does *not* copy the wrapped exception's text into ``message`` or ``details``;
a caller must supply safe text explicitly, because a raw driver or library exception can
itself contain a connection string, a header, or other sensitive material (ADR-006). Any
value in ``details`` that exposes a masked secret (a Pydantic ``SecretStr``/``SecretBytes``,
duck-typed via ``get_secret_value``) is replaced with a fixed placeholder before it is
returned by :meth:`to_dict`, so an accidental inclusion fails safe rather than leaking.

Phase 1 (BUILD_PLAN section 12).
"""

from __future__ import annotations

from typing import Any, ClassVar

_SECRET_PLACEHOLDER = "[REDACTED]"  # noqa: S105 - a redaction marker, not a credential


def _scrub(value: object) -> object:
    """Recursively replace anything that looks like a masked secret with a placeholder.

    Duck-typed on ``get_secret_value`` (Pydantic's ``SecretStr``/``SecretBytes`` shape)
    rather than importing pydantic here — ``errors.py`` has no reason to depend on it,
    and duck-typing keeps this useful against any future secret wrapper with the same
    shape. Everything else passes through unchanged; this is a safety net for accidental
    inclusion, not a substitute for callers choosing safe ``details`` in the first place.
    """
    if hasattr(value, "get_secret_value"):
        return _SECRET_PLACEHOLDER
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


class OperatorError(Exception):
    """Base for every error this codebase raises, in any layer.

    Subclasses set ``code``, ``retryable``, and ``remediation`` as class-level constants
    — they are properties of *what kind of failure this is*, not of any one occurrence.
    ``message`` and ``details`` are supplied per-instance, since the specifics (which
    operation, which workflow) vary by call site.
    """

    code: ClassVar[str] = "INTERNAL_ERROR"
    retryable: ClassVar[bool] = False
    remediation: ClassVar[str] = "Report; do not retry blindly."
    default_message: ClassVar[str] = "An unexpected server fault occurred."

    def __init__(
        self, message: str | None = None, *, details: dict[str, Any] | None = None
    ) -> None:
        self.message = message if message is not None else self.default_message
        self.details: dict[str, Any] = dict(details) if details else {}
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        """The wire shape from MCP_TOOLS.md section 4.1 — nothing more, nothing less."""
        return {
            "code": self.code,
            "message": self.message,
            "details": _scrub(self.details),
            "retryable": self.retryable,
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"

    @classmethod
    def from_exception(
        cls, exc: BaseException, *, message: str, details: dict[str, Any] | None = None
    ) -> OperatorError:
        """Wrap a lower-level exception without copying its text into the safe error.

        ``exc`` is attached as ``__cause__`` (via the ``raise ... from`` the caller
        performs with the result) purely so a server-side traceback remains useful to
        the operator reading logs locally; it is never read back out of the returned
        error, and nothing here inspects ``str(exc)`` or ``exc.args``. The caller must
        supply ``message`` and, if any, ``details`` explicitly — that is what keeps a
        driver exception's own text (which can carry a connection string, a header, or
        other sensitive material) from ever reaching :meth:`to_dict`.
        """
        error = cls(message, details=details)
        error.__cause__ = exc
        return error


# --------------------------------------------------------------------------------------
# Domain — business-rule and state-machine violations.
# --------------------------------------------------------------------------------------


class DomainError(OperatorError):
    """The request was understood; policy says no."""


class WorkflowNotFoundError(DomainError):
    code = "WORKFLOW_NOT_FOUND"
    retryable = False
    remediation = "Call list_workflows."
    default_message = (
        "No such registered workflow. This is returned identically whether the ID was "
        "never registered or does not exist at all (boundary defense against enumeration)."
    )


class WorkflowDisabledError(DomainError):
    code = "WORKFLOW_DISABLED"
    retryable = False
    remediation = "Ask the operator; do not retry."
    default_message = "This workflow is registered but disabled (enabled: false)."


class EnvironmentNotFoundError(DomainError):
    """Stage 04 (ADR-016). Identical whether ``environment`` names a nonexistent ID or
    one the caller is not authorized to see — no enumeration oracle across the
    organization boundary (ADR-015 section 3), the same discipline
    ``WorkflowNotFoundError`` already applies to workflow IDs."""

    code = "ENVIRONMENT_NOT_FOUND"
    retryable = False
    remediation = "Call list_environments."
    default_message = (
        "No such environment visible to this caller. This is returned identically "
        "whether the ID does not exist or the caller is not authorized to see it."
    )


class EnvironmentRequiredError(DomainError):
    """Stage 04 (ADR-016 section 3). Raised the instant a caller's resolved
    organization has more than one non-archived environment and ``environment`` was
    omitted — never a silent default, even when only one environment is
    ``is_production``."""

    code = "ENVIRONMENT_REQUIRED"
    retryable = False
    remediation = "Call list_environments; name one explicitly."
    default_message = (
        "More than one environment is visible; `environment` must be named explicitly."
    )


class EnvironmentArchivedError(DomainError):
    """Stage 04 (ADR-016 section 4). Raised only by a state-changing call
    (``prepare_operation``, ``execute_operation``) against an archived environment —
    every read tool still resolves an archived environment normally, since historical
    operations must stay readable."""

    code = "ENVIRONMENT_ARCHIVED"
    retryable = False
    remediation = "Ask the operator; use a live environment for new work."
    default_message = "This environment is archived; no new work may target it."


class ApproverNotInPolicyError(DomainError):
    """Stage 05 (ADR-017 section 1): ``request_approval``'s ``approvers`` argument
    named a principal who is not in the operation's own approval-policy snapshot —
    never a way to route to (or imply the existence of) someone outside it."""

    code = "APPROVER_NOT_IN_POLICY"
    retryable = False
    remediation = "Omit approvers, or check get_approval_status for the real snapshot."
    default_message = "One or more named approvers are not in this operation's approval policy."


class InvalidArgumentsError(DomainError):
    code = "INVALID_ARGUMENTS"
    retryable = False
    remediation = "Fix the call shape."
    default_message = "The tool arguments failed the tool's own schema."


class IdempotencyConflictError(DomainError):
    code = "IDEMPOTENCY_CONFLICT"
    retryable = False
    remediation = "Use a new key, or reuse the original arguments."
    default_message = (
        "This idempotency key was already used, within the same namespace "
        "(principal, environment, workflow), with different arguments (ADR-011)."
    )


class ArgumentsTooLargeError(DomainError):
    code = "ARGUMENTS_TOO_LARGE"
    retryable = False
    remediation = (
        "Send less data; the limit is reported in details. "
        "Do not split a side-effecting call to evade it."
    )
    default_message = "The canonical argument size exceeds the effective limit."


class OperationNotFoundError(DomainError):
    code = "OPERATION_NOT_FOUND"
    retryable = False
    remediation = "Call list_operations."
    default_message = "No such operation."


class RetryNotApplicableError(DomainError):
    """Stage 06 (ADR-012 section 1): ``retry_operation``'s parent is not in a state
    representing "did not run as intended" — or, distinctly (``details={"reason":
    "chain_depth_exceeded"}``), the retry chain already reaches ``MAX_RETRY_CHAIN_
    DEPTH``. Both are still "you may not retry this operation"; MCP_TOOLS.md's frozen
    error contract for this tool names no separate code for the chain-depth case."""

    code = "RETRY_NOT_APPLICABLE"
    retryable = False
    remediation = "Do not retry; if a genuinely new run is wanted, call prepare_operation."
    default_message = (
        'This operation is not in a state representing "did not run as intended" '
        "(SUCCEEDED, CANCELED, INVALID, EXECUTING, PENDING_APPROVAL, or APPROVED)."
    )


class OperationExpiredError(DomainError):
    code = "OPERATION_EXPIRED"
    retryable = False
    remediation = "Prepare a new operation."
    default_message = "The approval or execution window elapsed."


class OperationCanceledError(DomainError):
    code = "OPERATION_CANCELED"
    retryable = False
    remediation = "Prepare a new one if still wanted."
    default_message = "This operation was canceled."


class InvalidStateTransitionError(DomainError):
    code = "INVALID_STATE_TRANSITION"
    retryable = False
    remediation = "Read get_operation and act on actual state."
    default_message = "The requested move is not an edge in BUILD_PLAN section 5.2."


class ArgumentMismatchError(DomainError):
    code = "ARGUMENT_MISMATCH"
    retryable = False
    remediation = "Re-prepare with the intended arguments."
    default_message = "The argument fingerprint at execute differs from the one at prepare."


class ResultNotAvailableError(DomainError):
    code = "RESULT_NOT_AVAILABLE"
    retryable = False
    remediation = "Check state first."
    default_message = "This operation never executed; there is no result to return."


class RateLimitedError(DomainError):
    code = "RATE_LIMITED"
    retryable = True
    remediation = "Back off; the limit is per-workflow."
    default_message = "The registry rate limit for this workflow was exceeded."


class ConcurrencyLimitReachedError(DomainError):
    code = "CONCURRENCY_LIMIT_REACHED"
    retryable = True
    remediation = "Wait for the in-flight operation."
    default_message = "max_concurrent was reached for this workflow."


# --------------------------------------------------------------------------------------
# Authorization — capability failures (ADR-003).
# --------------------------------------------------------------------------------------


class AuthorizationError(OperatorError):
    """The caller lacks a valid handle or approval for what it is asking to do."""


class ApprovalRequiredError(AuthorizationError):
    code = "APPROVAL_REQUIRED"
    retryable = False
    remediation = "Wait; poll get_operation. Do not retry in a tight loop."
    default_message = "This operation is still PENDING_APPROVAL."


class HandleInvalidError(AuthorizationError):
    code = "HANDLE_INVALID"
    retryable = False
    remediation = "Re-prepare."
    default_message = "The handle does not match this operation or principal."


class HandleAlreadyUsedError(AuthorizationError):
    code = "HANDLE_ALREADY_USED"
    retryable = False
    remediation = "Do not retry. Check get_operation; the run may have happened."
    default_message = "This operation handle has already been used."


class InsufficientRoleError(AuthorizationError):
    """Stage 03 (ADR-015): raised only by CLI-only, system-wide administrative
    commands (``audit verify``/``audit export``) that have no single workflow or
    operation to intersect a scope against, so denial cannot be shaped as
    ``WORKFLOW_NOT_FOUND``/``OPERATION_NOT_FOUND`` the way every workflow- or
    operation-scoped v2 check is (invariant I14 governs *those* — enumeration of a
    specific object — not "you lack administrative capability to run this command at
    all", which is not an enumeration oracle since there is no object being
    enumerated). Never raised by an MCP tool and deliberately not part of
    MCP_TOOLS.md's tool-facing taxonomy or the ``TAXONOMY`` registry below, the same
    carve-out the approval-channel errors above already use."""

    code = "INSUFFICIENT_ROLE"
    retryable = False
    remediation = "Ask an organization admin to grant the admin role, or run this as one."
    default_message = "This command requires the admin role."


# --------------------------------------------------------------------------------------
# Approval-channel errors (phase 6, ADR-010; stage 05, ADR-017) — raised only by the
# loopback approval web page's own token-verification path
# (``core.service.resolve_approval_token``) and by the shared
# ``approve_operation``/``reject_operation`` use case both the CLI and the web page
# call to actually decide. The CLI approval channel identifies an operation by ID
# directly and has no token to verify, so it never raises the token-specific ones;
# both channels apply the identical PENDING_APPROVAL and one-decision-per-principal
# checks. Never returned by an MCP tool (there is no tool that approves, boundary
# B4) — deliberately not part of MCP_TOOLS.md's tool-facing taxonomy or the
# ``TAXONOMY`` registry below.
# --------------------------------------------------------------------------------------


class ApprovalTokenInvalidError(AuthorizationError):
    code = "APPROVAL_TOKEN_INVALID"
    retryable = False
    remediation = "Ask the operator to re-run n8n-operator operations approve/reject."
    default_message = "This approval link is not valid."


class ApprovalTokenAlreadyUsedError(AuthorizationError):
    code = "APPROVAL_TOKEN_ALREADY_USED"
    retryable = False
    remediation = "Check n8n-operator operations approval-status; a decision was already recorded."
    default_message = "This approval link has already been used."


class ApprovalNotPendingError(AuthorizationError):
    code = "APPROVAL_NOT_PENDING"
    retryable = False
    remediation = "Check n8n-operator operations approval-status for the operation's current state."
    default_message = "This operation is no longer awaiting approval."


class ApprovalAlreadyDecidedError(AuthorizationError):
    """Stage 05 (ADR-017 section 3): a principal in the operation's approval-policy
    snapshot who has already cast a decision (either direction) tries to decide
    again. Deliberately an explicit error, not a silently-ignored no-op — a second
    click should never be indistinguishable from "my first click didn't register.\""""

    code = "APPROVAL_ALREADY_DECIDED"
    retryable = False
    remediation = "Check n8n-operator operations approval-status; you already decided this one."
    default_message = "You have already decided this operation."


class ReconciliationNotApplicableError(DomainError):
    """Stage 06 (ADR-009/ADR-012): ``operations reconcile record`` refused — the
    operation is not ``UNKNOWN``, the named execution ID does not exist or is
    unreachable, or its own ``workflowId`` does not match this operation's
    registered ``n8n_workflow_id``. CLI-only, like ``ApprovalAlreadyDecidedError`` —
    no MCP tool ever reaches reconciliation (boundary B4's spirit), so this carries no
    taxonomy row."""

    code = "RECONCILIATION_NOT_APPLICABLE"
    retryable = False
    remediation = "Check the operation's state and the execution ID; nothing was recorded."
    default_message = "This reconciliation attempt could not be verified and was not recorded."


# --------------------------------------------------------------------------------------
# Provider — the n8n instance's own state (ADR-009).
# --------------------------------------------------------------------------------------


class ProviderError(OperatorError):
    """The n8n instance is unreachable, drifted, inactive, or gave an ambiguous answer."""


class DefinitionDriftError(ProviderError):
    code = "DEFINITION_DRIFT"
    retryable = False
    remediation = "Stop. This needs an operator, not a retry."
    default_message = "The live workflow definition differs from the registered hash."


class WorkflowInactiveError(ProviderError):
    code = "WORKFLOW_INACTIVE"
    retryable = False
    remediation = "Ask the operator."
    default_message = "The workflow is deactivated in n8n."


class WorkflowMissingOnInstanceError(ProviderError):
    code = "WORKFLOW_MISSING_ON_INSTANCE"
    retryable = False
    remediation = "Ask the operator."
    default_message = "This workflow is registered but absent from the n8n instance."


class MissingNodeCredentialsError(ProviderError):
    code = "MISSING_NODE_CREDENTIALS"
    retryable = False
    remediation = "Ask the operator."
    default_message = (
        "A node has no credential bound on the instance. This says nothing about "
        "whether a bound credential is valid (ADR-009)."
    )


class InstanceUnreachableError(ProviderError):
    code = "INSTANCE_UNREACHABLE"
    retryable = True
    remediation = "Retry later at human pace; do not loop."
    default_message = "The n8n instance did not respond."


class DispatchIndeterminateError(ProviderError):
    code = "DISPATCH_INDETERMINATE"
    retryable = False
    remediation = "Never retry. Verify downstream, then decide."
    default_message = (
        "The request was sent but the outcome could not be confirmed. It may or may "
        "not have taken effect. Do not retry: verify the downstream system, then "
        "prepare a new operation if needed."
    )


# --------------------------------------------------------------------------------------
# Configuration — the operator's own configuration or registry is invalid.
# --------------------------------------------------------------------------------------


class ConfigurationError(OperatorError):
    """Startup configuration or registry-load failure. Never the caller's fault."""

    code = "CONFIGURATION_INVALID"
    retryable = False
    remediation = "Operator action required; the server should not be serving."
    default_message = "The server configuration is invalid."


class RegistryUnavailableError(ConfigurationError):
    code = "REGISTRY_UNAVAILABLE"
    retryable = False
    remediation = "Operator action required; the server should not be serving."
    default_message = "The registry failed to load."


# --------------------------------------------------------------------------------------
# Storage — unexpected persistence-layer failures.
# --------------------------------------------------------------------------------------


class StorageError(OperatorError):
    """An unexpected failure in the persistence layer.

    Reserved for wrapping SQLAlchemy or driver exceptions via
    :meth:`OperatorError.from_exception`. Defaults to ``INTERNAL_ERROR`` — the taxonomy
    has no dedicated storage-facing wire code, and this class exists to give the
    persistence layer (``storage/``) something distinct to raise and callers something
    distinct to catch, independent of which wire code the failure ultimately carries.
    """

    code = "INTERNAL_ERROR"
    retryable = False
    remediation = "Report; do not retry blindly."
    default_message = "An unexpected storage failure occurred."


class OptimisticLockError(StorageError):
    """A compare-and-set write found the row already at a different version or state.

    Raised by the CAS primitives in ``storage/repository.py`` — the mechanical
    enforcement invariants I4 and I6-adjacent guarantees rest on (BUILD_PLAN section
    5.4). This is a storage-layer signal, not a business decision: it says nothing about
    which state the row is actually in, only that the expected precondition did not hold.
    """

    default_message = "The expected row version or precondition did not hold."


# --------------------------------------------------------------------------------------
# Internal — the generic catch-all (MCP_TOOLS.md: "Unexpected server fault").
# --------------------------------------------------------------------------------------


class InternalError(OperatorError):
    code = "INTERNAL_ERROR"
    retryable = False
    remediation = "Report; do not retry blindly."
    default_message = "An unexpected server fault occurred."


# --------------------------------------------------------------------------------------
# The taxonomy registry — every MCP_TOOLS.md section 4 code, mapped to its class.
# --------------------------------------------------------------------------------------

TAXONOMY: dict[str, type[OperatorError]] = {
    cls.code: cls
    for cls in (
        WorkflowNotFoundError,
        WorkflowDisabledError,
        EnvironmentNotFoundError,
        EnvironmentRequiredError,
        EnvironmentArchivedError,
        ApproverNotInPolicyError,
        InvalidArgumentsError,
        IdempotencyConflictError,
        ArgumentsTooLargeError,
        OperationNotFoundError,
        RetryNotApplicableError,
        ApprovalRequiredError,
        OperationExpiredError,
        OperationCanceledError,
        InvalidStateTransitionError,
        HandleInvalidError,
        HandleAlreadyUsedError,
        ArgumentMismatchError,
        DefinitionDriftError,
        WorkflowInactiveError,
        WorkflowMissingOnInstanceError,
        MissingNodeCredentialsError,
        InstanceUnreachableError,
        DispatchIndeterminateError,
        RateLimitedError,
        ConcurrencyLimitReachedError,
        ResultNotAvailableError,
        RegistryUnavailableError,
        InternalError,
    )
}
"""24 original v1 codes, the 3 environment codes stage 04 adds, the 1 approval code
(``APPROVER_NOT_IN_POLICY``) stage 05 adds, and the 1 retry code (``RETRY_NOT_
APPLICABLE``) stage 06 adds — verified by a contract test that parses that table and
asserts this dict's keys are a subset of it, and that each implemented class's
``remediation`` matches the table's "Model's correct next move" column verbatim."""

__all__ = [
    "TAXONOMY",
    "ApprovalAlreadyDecidedError",
    "ApprovalNotPendingError",
    "ApprovalRequiredError",
    "ApprovalTokenAlreadyUsedError",
    "ApprovalTokenInvalidError",
    "ApproverNotInPolicyError",
    "ArgumentMismatchError",
    "ArgumentsTooLargeError",
    "AuthorizationError",
    "ConcurrencyLimitReachedError",
    "ConfigurationError",
    "DefinitionDriftError",
    "DispatchIndeterminateError",
    "DomainError",
    "EnvironmentArchivedError",
    "EnvironmentNotFoundError",
    "EnvironmentRequiredError",
    "HandleAlreadyUsedError",
    "HandleInvalidError",
    "IdempotencyConflictError",
    "InstanceUnreachableError",
    "InsufficientRoleError",
    "InternalError",
    "InvalidArgumentsError",
    "InvalidStateTransitionError",
    "MissingNodeCredentialsError",
    "OperationCanceledError",
    "OperationExpiredError",
    "OperationNotFoundError",
    "OperatorError",
    "OptimisticLockError",
    "ProviderError",
    "RateLimitedError",
    "ReconciliationNotApplicableError",
    "RegistryUnavailableError",
    "ResultNotAvailableError",
    "RetryNotApplicableError",
    "StorageError",
    "WorkflowDisabledError",
    "WorkflowInactiveError",
    "WorkflowMissingOnInstanceError",
    "WorkflowNotFoundError",
]
