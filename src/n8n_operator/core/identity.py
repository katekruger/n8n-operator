"""Identity resolution — the orchestration ``storage/`` and ``identity/`` are each
forbidden from doing themselves (ARCHITECTURE.md section 2.1: capability packages must
not import each other). ``identity/oidc.py`` proves a bearer token is a valid,
current assertion of ``(iss, sub)``; this module turns that pair into an Operator
:class:`~n8n_operator.storage.models.Principal` — creating one just-in-time for a new
``user``, never a membership (ADR-013 section 3) — and answers ``whoami`` from a fresh,
uncached read of that principal's active memberships (ADR-014 section 4).

Every function here takes an already-open ``Session``; nothing opens its own
transaction, the same convention ``core/service.py`` already follows.

Phase 10 (v2) stage 02.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from n8n_operator.storage.models import Principal
from n8n_operator.storage.repository import (
    EnvironmentRepository,
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
)

__all__ = [
    "DEV_PRINCIPAL_DISPLAY_NAME",
    "EnvironmentSummary",
    "OrganizationMembershipSummary",
    "WhoAmI",
    "build_whoami",
    "ensure_dev_principal",
    "resolve_cli_principal_id",
    "resolve_user_principal",
]

DEV_PRINCIPAL_DISPLAY_NAME = "local development (identity_mode=dev — never for production)"

# A fixed, well-known ID (not a random ULID) so `ensure_dev_principal` can check for
# this organization's existence with a plain get-by-id — no name-based lookup needed,
# and idempotent by construction the same way `ensure_dev_principal` itself already is.
DEV_ORGANIZATION_ID = "org_local_development"
DEV_ORGANIZATION_NAME = "Local development"


@dataclass(frozen=True)
class EnvironmentSummary:
    environment_id: str
    name: str
    is_production: bool


@dataclass(frozen=True)
class OrganizationMembershipSummary:
    organization_id: str
    name: str
    roles: list[str]
    environments: list[EnvironmentSummary] = field(default_factory=list)


@dataclass(frozen=True)
class WhoAmI:
    """Exactly MCP_TOOLS.md section 5.1's result shape."""

    principal_id: str
    kind: str
    display_name: str
    organizations: list[OrganizationMembershipSummary] = field(default_factory=list)


def resolve_user_principal(
    session: Session,
    *,
    issuer: str,
    subject: str,
    display_name_hint: str | None = None,
) -> Principal | None:
    """Look up the principal for ``(issuer, subject)``, provisioning one just-in-time
    if this is its first successful authentication — **never** with any organization
    membership (ADR-013 section 3): a fresh ``user`` principal is a normal, expected
    "member of nothing" state (ADR-013's own Consequences), not a partial failure.

    Returns ``None`` if the principal exists and is disabled — the caller (the
    composition-root token verifier, ``mcp/server.py``) treats this identically to an
    invalid token: authentication itself fails, uniformly, before any tool handler
    runs (ADR-014 section 4).
    """
    principals = PrincipalRepository(session)
    principal = principals.get_by_external_identity(issuer=issuer, subject=subject)
    if principal is None:
        principal = principals.create(
            kind="user",
            display_name=display_name_hint or subject,
            external_subject=subject,
            external_issuer=issuer,
        )
    if principal.disabled_at is not None:
        return None
    return principal


def ensure_dev_principal(session: Session, *, principal_id: str) -> Principal:
    """Get-or-create the one fixed, visibly-labeled service principal
    ``identity_mode=dev`` attributes every caller to (ADR-014 section 5, extended from
    stdio to also cover a non-OIDC local HTTP deployment — both are "not real identity,
    kept easy for local development" in the same way).

    Also idempotently ensures this principal holds a real, ordinary ``admin`` membership
    in one canonical "Local development" organization (Stage 03) — under real RBAC
    enforcement, a bare principal with zero memberships authorizes for nothing, which
    would silently defeat "local dev stays easy" the moment authorization is enforced.
    This is a real grant through the real grant mechanism (``organization_memberships``,
    visible via ``whoami``, revocable via ``identity remove-membership`` like any other),
    not a bypass — the dev principal is simply, deliberately, always an admin of its own
    always-existing development organization.

    Idempotent — safe to call on every server startup, not just once at ``db init``:
    an operator flipping ``enable_v2``/``identity_mode`` on an existing database should
    not have to remember a separate seeding step first.
    """
    principals = PrincipalRepository(session)
    principal = principals.get(principal_id)
    if principal is None:
        principal = principals.create(
            id=principal_id, kind="service", display_name=DEV_PRINCIPAL_DISPLAY_NAME
        )

    organizations = OrganizationRepository(session)
    organization = organizations.get(DEV_ORGANIZATION_ID)
    if organization is None:
        organization = organizations.create(id=DEV_ORGANIZATION_ID, name=DEV_ORGANIZATION_NAME)

    memberships = OrganizationMembershipRepository(session)
    if memberships.get_active(principal_id=principal.id, organization_id=organization.id) is None:
        memberships.create(
            principal_id=principal.id,
            organization_id=organization.id,
            roles=["admin"],
            workflow_scope="*",
            environment_scope=["*"],
        )

    return principal


def build_whoami(session: Session, principal: Principal) -> WhoAmI:
    """Every organization ``principal`` currently belongs to, its role grants, and
    that organization's environments — queried fresh, never cached (ADR-014 section 4,
    extended from "is this principal disabled" to "what can whoami currently see").
    A principal in no organization gets ``organizations: []`` (ADR-013 section 3),
    which is exactly what an empty query result already produces — no special case."""
    memberships = OrganizationMembershipRepository(session).list_active_for_principal(principal.id)
    organizations: list[OrganizationMembershipSummary] = []
    for membership in memberships:
        organization = OrganizationRepository(session).get(membership.organization_id)
        if organization is None:
            continue  # pragma: no cover - defensive; a live FK makes this unreachable
        environments = [
            EnvironmentSummary(
                environment_id=env.id, name=env.name, is_production=env.is_production
            )
            for env in EnvironmentRepository(session).list_for_organization(organization.id)
        ]
        organizations.append(
            OrganizationMembershipSummary(
                organization_id=organization.id,
                name=organization.name,
                roles=list(membership.roles),
                environments=environments,
            )
        )
    return WhoAmI(
        principal_id=principal.id,
        kind=principal.kind,
        display_name=principal.display_name,
        organizations=organizations,
    )


def resolve_cli_principal_id(session: Session, *, enable_v2: bool, dev_principal_id: str) -> str:
    """The CLI's own identity resolution (Stage 03) — every command before this stage
    either hardcoded the fixed v1 identity ``"local"`` (``cli/commands/operations.py``)
    or resolved no principal at all (``cli/commands/audit.py``).

    Takes ``enable_v2``/``dev_principal_id`` as plain values, not a full ``Settings``
    (``config.resolve_v2_identity_flags()`` resolves them without requiring
    ``n8n_base_url``/``n8n_api_key`` — both otherwise-required ``Settings`` fields — to
    be present, the same "schema/identity management is orthogonal to n8n
    configuration" reasoning ``config.resolve_database_url`` already established) —
    this module stays independent of ``config.py``, matching every other module here.

    ``enable_v2=False`` (v1, the default): returns ``"local"`` unchanged — v1's CLI
    behavior is byte-identical to before this stage, the completion gate's explicit
    requirement.

    ``enable_v2=True``: the CLI, like stdio, is a local-machine trust boundary with no
    network listener — it always resolves to the same fixed dev/service principal
    :func:`ensure_dev_principal` provisions (and grants a real ``admin`` membership,
    per that function's own docstring), regardless of ``identity_mode``. This mirrors
    ADR-014 section 5's existing stdio rule exactly rather than inventing a second
    identity mechanism for a command-line invocation, which has no bearer token to
    resolve an OIDC identity from in the first place.
    """
    if not enable_v2:
        return "local"
    principal = ensure_dev_principal(session, principal_id=dev_principal_id)
    return principal.id
