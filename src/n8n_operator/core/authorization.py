"""RBAC authorization evaluation (ADR-015). Pure domain logic — no database, no HTTP,
no vendor — consuming data ``core/service.py`` already fetches (a principal's active
memberships, a resolved workflow ID), the same shape ``core/state_machine.py`` has for
the same reason: this isn't an external concern, so it isn't a capability package
(ARCHITECTURE.md section 2.1).

**Four roles, each a strict superset of the read-only tools before it.** Authorization
is workflow-scope AND environment-scope AND role-capability — all three must pass, or
the caller-facing result is identical to the resource not existing at all (invariant
I14: no ``FORBIDDEN`` code anywhere, extending ADR-002's anti-enumeration guarantee
across the organization boundary). :func:`evaluate` never raises and never determines
the caller-facing error itself — it returns a typed :class:`AuthorizationDecision`
whose ``reason_code`` is for server-side logging only; the caller (``core/service.py``)
decides which existing not-found exception to raise, so a denial is indistinguishable
from absence by construction, not by care at each call site.

**Cross-organization semantics: union across memberships, intersection within one.** A
principal may hold active memberships in several organizations. Each membership's own
grant (role ∧ workflow-scope ∧ environment-scope) is evaluated independently; a call is
authorized if *any single* membership's grant, entirely on its own terms, authorizes
it. This is not the "union across grants" ADR-015 explicitly rejects — that alternative
was about mixing fields *across* grants (org A's workflow-scope with org B's
environment-scope) to synthesize a broader combined grant, which stays forbidden here
too: :func:`evaluate` never combines fields from two different memberships into one
check.

**Environment-scope today.** No v1 tool carries an ``environment`` argument yet (that
argument, its default-resolution, and ``ENVIRONMENT_REQUIRED``/``ENVIRONMENT_ARCHIVED``
are Stage 04's charter, MCP_TOOLS.md section 5.9) — so a real v1 tool call always
evaluates with ``environment_id=None``. A membership's ``environment_scope`` grant is
satisfied by ``environment_id=None`` only when it is exactly ``["*"]`` (the default);
a membership deliberately narrowed to specific environment IDs cannot be satisfied by a
call with no environment to check against, and fails closed rather than guessing. This
conjunct is nonetheless fully implemented and exhaustively property-tested against
synthetic environment IDs (AC-39) — it doesn't need a live tool argument to be correct
or tested, only to be *reachable* from every real call, which is Stage 04's job to
complete.

Phase 10 (v2) stage 03.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Literal, cast, get_args

from n8n_operator.storage.models import OrganizationMembership

__all__ = [
    "APPROVE_REJECT_CAPABILITY",
    "ROLE_CAPABILITIES",
    "VALID_ROLES",
    "AuthorizationDecision",
    "Role",
    "capabilities_for_role",
    "environment_scope_covers",
    "evaluate",
    "has_role",
    "match_workflow_scope",
    "workflow_scope_to_sql_like",
]

Role = Literal["viewer", "operator", "approver", "admin"]

VALID_ROLES: frozenset[str] = frozenset(get_args(Role))

# The ADR-015 section 1 role-capability matrix, verbatim. Every role includes every
# read-only v1 and v2 tool; `approver` deliberately excludes `prepare_operation`/
# `execute_operation` (separating requester from decider — ADR-015 section 1);
# `retry_operation` is `admin`-only (a fresh, policy-significant re-authorization,
# ADR-012). Keyed by role so `tests/property/test_rbac_matrix.py` can diff this table
# directly against every (role, tool) pair it names.
_READ_ONLY_TOOLS = frozenset(
    {
        "list_workflows",
        "describe_workflow",
        "get_instance_health",
        "validate_input",
        "preflight_workflow",
        "get_operation",
        "list_operations",
        "get_execution_result",
        "get_execution_log",
        "whoami",
        "list_environments",
        "diff_workflow_definition",
        "get_metrics",
        "list_audit_events",
        "get_approval_status",
    }
)

# Not one of the 20 named MCP tools — there is no MCP tool that grants approval
# (boundary B4). This is ADR-015 section 1's separate matrix row, "out-of-band
# approve/reject decision (CLI or approval app)", modeled as a capability like any
# other so the CLI's approve/reject path can reuse `evaluate()` unchanged rather than
# a second, parallel check function. Internal-only name, never a tool a client can call.
APPROVE_REJECT_CAPABILITY = "approve_reject_operation"

ROLE_CAPABILITIES: dict[Role, frozenset[str]] = {
    "viewer": frozenset(_READ_ONLY_TOOLS),
    "operator": frozenset(
        _READ_ONLY_TOOLS
        | {"prepare_operation", "cancel_operation", "execute_operation", "request_approval"}
    ),
    "approver": frozenset(_READ_ONLY_TOOLS | {"request_approval", APPROVE_REJECT_CAPABILITY}),
    "admin": frozenset(
        _READ_ONLY_TOOLS
        | {
            "prepare_operation",
            "cancel_operation",
            "execute_operation",
            "request_approval",
            "retry_operation",
            APPROVE_REJECT_CAPABILITY,
        }
    ),
}


class _ReasonCode:
    """Internal-only decision reasons — logged for operators (server-side structured
    logs, ``core/service.py``'s ``_authorize`` helper), **never** serialized into any
    tool result, CLI output, or exception message a caller can read. Not an enum so a
    plain string comparison in a log filter needs no import; the closed set is
    documented here for anyone reading a log line."""

    ALLOWED = "ALLOWED"
    NO_ACTIVE_MEMBERSHIP = "NO_ACTIVE_MEMBERSHIP"
    ROLE_LACKS_CAPABILITY = "ROLE_LACKS_CAPABILITY"
    WORKFLOW_OUT_OF_SCOPE = "WORKFLOW_OUT_OF_SCOPE"
    ENVIRONMENT_OUT_OF_SCOPE = "ENVIRONMENT_OUT_OF_SCOPE"
    SELF_DECISION = "SELF_DECISION"


@dataclass(frozen=True)
class AuthorizationDecision:
    """The evaluator's one output shape. ``reason_code`` is one of :class:`_ReasonCode`'s
    values — internal-only, see that class's docstring."""

    allowed: bool
    reason_code: str


_ALLOWED = AuthorizationDecision(allowed=True, reason_code=_ReasonCode.ALLOWED)


def capabilities_for_role(role: str) -> frozenset[str]:
    """``ROLE_CAPABILITIES.get(role, frozenset())``, safe for a plain ``str`` — the
    one place this module bridges ``OrganizationMembership.roles`` (a stored,
    untyped-at-the-type-checker ``list[str]``, validated against :data:`VALID_ROLES`
    only at grant time, ``cli/commands/identity.py``'s own job) to
    :data:`ROLE_CAPABILITIES`'s precise ``Role``-keyed type. An unrecognized string
    (which should never reach here, given grant-time validation) is simply "no
    capabilities" rather than a lookup error."""
    if role not in VALID_ROLES:
        return frozenset()
    return ROLE_CAPABILITIES[cast(Role, role)]


def has_role(memberships: list[OrganizationMembership], role: Role) -> bool:
    """Whether any active membership grants ``role`` at all — ignoring workflow/
    environment scope entirely. For system-wide, not-workflow-scoped capabilities only
    (the CLI's ``audit verify``/``audit export``, which read across every principal and
    workflow at once, so there is no single workflow to intersect a scope against)."""
    return any(role in membership.roles for membership in memberships)


def match_workflow_scope(pattern: str, workflow_id: str) -> bool:
    """Whether ``workflow_id`` matches a membership's ``workflow_scope`` glob pattern
    (``*`` meaning all — ADR-015 section 2). ``fnmatch``-style: workflow IDs are
    already validated at registry-load time to be lowercase, dot/underscore/hyphen-
    segmented tokens (``registry/loader.py``'s ``ID_PATTERN``), so a plain glob against
    the literal ``id`` string is unambiguous."""
    return fnmatch.fnmatchcase(workflow_id, pattern)


def workflow_scope_to_sql_like(pattern: str) -> str:
    """Translate a ``workflow_scope`` glob pattern into a SQL ``LIKE`` pattern —
    mechanical and exact: ``fnmatch``'s ``*`` and ``?`` map 1:1 onto SQL's ``%`` and
    ``_``, so this is a straight character translation, not an approximation. Existing
    literal ``%``/``_``/``\\`` characters in the pattern are escaped first so they
    match themselves rather than acting as SQL wildcards — paired with an
    ``ESCAPE '\\\\'`` clause at the call site (``storage/repository.py``). Used only to
    push workflow-scope filtering into the database *before* a ``LIMIT`` is applied
    (``list_operations``'s pagination), so a cursor can never walk past a row that
    would have been hidden by a filter applied only after paging — the "pagination side
    channel" the Stage 03 completion gate names explicitly."""
    escaped = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped.replace("*", "%").replace("?", "_")


def _environment_scope_satisfied(
    membership: OrganizationMembership,
    environment_id: str | None,
    environment_organization_id: str | None,
) -> bool:
    """``membership.environment_scope`` never means "any environment ID anywhere" —
    even ``["*"]`` means "every environment *in this membership's own organization*"
    (ADR-016 section 2: an environment belongs to exactly one organization). A
    membership in an organization that does not own ``environment_id`` at all can
    never satisfy this conjunct, regardless of its own ``environment_scope`` value —
    checked first, before the scope pattern itself, so a caller with a wildcard grant
    in an unrelated organization is refused the same way a caller with no grant at all
    would be, not silently authorized by an org boundary neither field encodes on its
    own (Stage 04, closing RR-13's own "reachable but org-blind" gap)."""
    if (
        environment_id is not None
        and environment_organization_id is not None
        and membership.organization_id != environment_organization_id
    ):
        return False
    scope = membership.environment_scope
    if environment_id is None:
        # No v1 tool call carries an environment yet — only an unscoped ("all
        # environments") grant can be honored without an environment to check against
        # (module docstring, "Environment-scope today").
        return scope == ["*"]
    return "*" in scope or environment_id in scope


def environment_scope_covers(
    membership: OrganizationMembership,
    environment_id: str | None,
    environment_organization_id: str | None,
) -> bool:
    """Public wrapper over :func:`_environment_scope_satisfied` — a caller outside
    this module (stage 05's eligible-approver enumeration, ``core.service``) needs
    the identical org-boundary-then-scope-pattern check :func:`evaluate` already
    applies per membership, without duplicating it."""
    return _environment_scope_satisfied(membership, environment_id, environment_organization_id)


def evaluate(
    *,
    memberships: list[OrganizationMembership],
    tool_name: str,
    workflow_id: str | None,
    environment_id: str | None = None,
    environment_organization_id: str | None = None,
    requester_principal_id: str | None = None,
    decider_principal_id: str | None = None,
) -> AuthorizationDecision:
    """The one evaluation function every adapter calls (ADR-015). Pure: takes an
    already-fetched list of a principal's active memberships (typically
    ``OrganizationMembershipRepository.list_active_for_principal``) and returns a
    decision — no database access here, so a disabled principal or a removed
    membership never reaches this function at all (checked live, upstream, exactly as
    Stage 02 already does for identity — never cached, never re-derived here).

    ``environment_organization_id`` — the organization ``environment_id`` actually
    belongs to (resolved by the caller, e.g. ``core.identity.resolve_environment`` or
    an ``EnvironmentRepository`` lookup) — is what makes a membership's own
    ``environment_scope`` mean anything at all: without it, a membership in an
    unrelated organization whose grant happens to be ``["*"]`` would satisfy *any*
    ``environment_id`` from *any* organization (see ``_environment_scope_satisfied``).
    Every real call site that has a resolved ``environment_id`` also has this value
    available; omitting it (``None``) is only correct when ``environment_id`` is also
    ``None``, or in a synthetic property test that isn't modeling the organization
    boundary at all.

    ``requester_principal_id``/``decider_principal_id``, when both given (the
    approve/reject path only), implement the self-decision rule: a principal may never
    decide an operation they themselves requested, regardless of role — buildable now
    with data (``operations.principal_id``) that already exists, without waiting for
    ADR-017's full quorum/snapshot machinery (Stage 05).
    """
    if (
        requester_principal_id is not None
        and decider_principal_id is not None
        and requester_principal_id == decider_principal_id
    ):
        return AuthorizationDecision(allowed=False, reason_code=_ReasonCode.SELF_DECISION)

    if not memberships:
        return AuthorizationDecision(allowed=False, reason_code=_ReasonCode.NO_ACTIVE_MEMBERSHIP)

    saw_role_capability = False
    saw_workflow_scope = False
    for membership in memberships:
        capable_roles = [
            role for role in membership.roles if tool_name in capabilities_for_role(role)
        ]
        if not capable_roles:
            continue
        saw_role_capability = True

        if workflow_id is not None and not match_workflow_scope(
            membership.workflow_scope, workflow_id
        ):
            continue
        saw_workflow_scope = True

        if not _environment_scope_satisfied(
            membership, environment_id, environment_organization_id
        ):
            continue

        return _ALLOWED

    if not saw_role_capability:
        return AuthorizationDecision(allowed=False, reason_code=_ReasonCode.ROLE_LACKS_CAPABILITY)
    if not saw_workflow_scope:
        return AuthorizationDecision(allowed=False, reason_code=_ReasonCode.WORKFLOW_OUT_OF_SCOPE)
    return AuthorizationDecision(allowed=False, reason_code=_ReasonCode.ENVIRONMENT_OUT_OF_SCOPE)
