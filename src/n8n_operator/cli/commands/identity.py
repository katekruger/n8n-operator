"""``n8n-operator identity`` — organizations, memberships, and service principals
(ADR-013, ADR-014; stage 02).

Every command here resolves the database URL via
:func:`n8n_operator.config.resolve_database_url`, the same "schema/identity
administration is orthogonal to the rest of configuration" reasoning ``db.py`` and
``audit.py`` already document — none of these commands needs
``N8N_OPERATOR_N8N_BASE_URL``/``N8N_OPERATOR_N8N_API_KEY``.

There is no MCP tool that grants an organization, a membership, or a role
(ADR-013 section 1's "no global admin query in v2" extended to writes: nothing in the
*tool* surface can create authority, only inspect what already exists). Every
authority-creating action lives here, on the machine running Operator — the same trust
boundary v1's registry file and approval bootstrap already rest on. ``bootstrap``
specifically exists because RBAC has no way to grant its own first grant: before any
organization exists, there is no admin to ask.

**Never prints a secret.** ``create-service-principal``/``rotate-service-credential``
take a ``credential_ref`` the admin already set at its target (an environment variable
or a keyring entry) — this module validates the reference *resolves* (catching a typo
or a forgotten `export` immediately) but never echoes the resolved value, only the
reference name itself, which is not a secret (ADR-006).

Phase 10 (v2) stage 02.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import typer
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.config import resolve_database_url, resolve_secret_reference
from n8n_operator.core.identity import resolve_user_principal
from n8n_operator.logging_setup import register_secret
from n8n_operator.storage.repository import (
    EnvironmentRepository,
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)

app = typer.Typer(
    help="Organizations, memberships, and service principals (v2).", no_args_is_help=True
)

VALID_ROLES = frozenset({"viewer", "operator", "approver", "admin"})


@contextmanager
def _connected() -> Iterator[sessionmaker[Session]]:
    engine = create_engine_for_url(resolve_database_url())
    try:
        yield create_session_factory(engine)
    finally:
        engine.dispose()


def _parse_roles(raw: str) -> list[str]:
    roles = [r.strip() for r in raw.split(",") if r.strip()]
    if not roles:
        typer.secho("At least one role is required.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    invalid = sorted(set(roles) - VALID_ROLES)
    if invalid:
        typer.secho(
            f"Invalid role(s): {invalid}. Valid roles: {sorted(VALID_ROLES)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    return roles


def _validate_credential_ref_or_exit(credential_ref: str) -> None:
    """Resolves ``credential_ref`` once, purely to fail loudly now (a typo'd env var
    name, a keyring entry that was never actually set) rather than silently at the
    first real authentication attempt — the resolved value itself is discarded
    immediately, never printed, never stored anywhere but transiently in this
    process's memory."""
    try:
        resolved = resolve_secret_reference(credential_ref)
    except ValueError as exc:
        typer.secho(f"credential_ref does not resolve: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    register_secret(resolved)


@app.command("bootstrap")
def bootstrap(
    org_name: str = typer.Option(..., "--org-name", help="The new organization's name."),
    admin_issuer: str = typer.Option(
        ..., "--admin-issuer", help="OIDC issuer URL of the first admin's identity."
    ),
    admin_subject: str = typer.Option(
        ..., "--admin-subject", help="OIDC sub claim of the first admin's identity."
    ),
    admin_display_name: str | None = typer.Option(
        None, "--admin-display-name", help="Defaults to the subject if omitted."
    ),
) -> None:
    """Create an organization and grant its first ``admin`` — the one action that must
    happen before any RBAC-gated path can grant anything else. The admin's principal
    is provisioned now if they have never authenticated (the same JIT logic a real
    first login would trigger), so they can be granted access before their first
    connection, not only after it.
    """
    with _connected() as factory, session_scope(factory) as session:
        organization = OrganizationRepository(session).create(name=org_name)
        principal = resolve_user_principal(
            session,
            issuer=admin_issuer,
            subject=admin_subject,
            display_name_hint=admin_display_name,
        )
        # A principal freshly created (or found not-disabled) here is never None —
        # resolve_user_principal only returns None for an *existing, disabled* one,
        # and disabling requires a membership to have existed to disable in the first
        # place, which is exactly what this command is about to create.
        assert principal is not None
        OrganizationMembershipRepository(session).create(
            principal_id=principal.id, organization_id=organization.id, roles=["admin"]
        )
        organization_id, principal_id = organization.id, principal.id

    typer.secho(f"Organization created: {organization_id} ({org_name})", fg=typer.colors.GREEN)
    typer.echo(f"Admin principal:      {principal_id} ({admin_issuer} / {admin_subject})")


@app.command("create-org")
def create_org(name: str = typer.Option(..., "--name")) -> None:
    """Create an additional organization. Grant its first admin separately with
    ``add-membership`` — ``bootstrap`` is for the very first organization only, where
    no admin yet exists to make that second call."""
    with _connected() as factory, session_scope(factory) as session:
        organization = OrganizationRepository(session).create(name=name)
        organization_id = organization.id
    typer.secho(f"Organization created: {organization_id} ({name})", fg=typer.colors.GREEN)


@app.command("list-orgs")
def list_orgs() -> None:
    with _connected() as factory, factory() as session:
        organizations = OrganizationRepository(session).list()
    if not organizations:
        typer.echo("No organizations.")
        return
    for organization in organizations:
        typer.echo(f"{organization.id}  {organization.name}")


@app.command("add-membership")
def add_membership(
    organization_id: str = typer.Option(..., "--org"),
    issuer: str = typer.Option(..., "--issuer"),
    subject: str = typer.Option(..., "--subject"),
    roles: str = typer.Option(
        ..., "--roles", help="Comma-separated: viewer,operator,approver,admin"
    ),
    display_name: str | None = typer.Option(None, "--display-name"),
    workflow_scope: str = typer.Option("*", "--workflow-scope"),
) -> None:
    """Grant a role set to a principal within one organization — provisioning the
    principal now (JIT) if this is the first time Operator has seen this identity."""
    parsed_roles = _parse_roles(roles)
    with _connected() as factory, session_scope(factory) as session:
        organization = OrganizationRepository(session).get(organization_id)
        if organization is None:
            typer.secho(f"No such organization: {organization_id}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        principal = resolve_user_principal(
            session, issuer=issuer, subject=subject, display_name_hint=display_name
        )
        if principal is None:
            typer.secho(
                "That principal is disabled; enable it first (identity enable-principal).",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        existing = OrganizationMembershipRepository(session).get_active(
            principal_id=principal.id, organization_id=organization.id
        )
        if existing is not None:
            typer.secho(
                "This principal already has an active membership in this organization; "
                "remove it first (identity remove-membership) to change its roles.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        OrganizationMembershipRepository(session).create(
            principal_id=principal.id,
            organization_id=organization.id,
            roles=parsed_roles,
            workflow_scope=workflow_scope,
        )
        principal_id = principal.id
    typer.secho(
        f"Granted {parsed_roles} to {principal_id} in {organization_id}", fg=typer.colors.GREEN
    )


@app.command("remove-membership")
def remove_membership(
    organization_id: str = typer.Option(..., "--org"),
    principal_id: str = typer.Option(..., "--principal"),
) -> None:
    """Revoke a principal's membership in one organization. Takes effect on that
    principal's *next* call — nothing here needs a cache to invalidate (ADR-014
    section 4)."""
    with _connected() as factory, session_scope(factory) as session:
        membership = OrganizationMembershipRepository(session).get_active(
            principal_id=principal_id, organization_id=organization_id
        )
        if membership is None:
            typer.secho("No active membership found for that pair.", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        OrganizationMembershipRepository(session).remove(membership.id)
    typer.secho(f"Removed {principal_id}'s membership in {organization_id}", fg=typer.colors.GREEN)


@app.command("list-memberships")
def list_memberships(
    organization_id: str = typer.Option(..., "--org"),
    include_removed: bool = typer.Option(False, "--include-removed"),
) -> None:
    with _connected() as factory, factory() as session:
        organization = OrganizationRepository(session).get(organization_id)
        if organization is None:
            typer.secho(f"No such organization: {organization_id}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        memberships = OrganizationMembershipRepository(session).list_for_organization(
            organization_id, include_removed=include_removed
        )
        environments = {
            env.id: env.name
            for env in EnvironmentRepository(session).list_for_organization(
                organization_id, include_archived=True
            )
        }
        rows = []
        for membership in memberships:
            principal = PrincipalRepository(session).get(membership.principal_id)
            display = principal.display_name if principal is not None else "(unknown)"
            status = "removed" if membership.removed_at is not None else "active"
            scope = (
                "*"
                if membership.environment_scope == ["*"]
                else ", ".join(environments.get(e, e) for e in membership.environment_scope)
            )
            rows.append(
                f"{membership.principal_id}  {display:<30}  {membership.roles}  "
                f"workflow_scope={membership.workflow_scope}  environments={scope}  [{status}]"
            )
    if not rows:
        typer.echo("No memberships.")
        return
    for row in rows:
        typer.echo(row)


@app.command("disable-principal")
def disable_principal(principal_id: str) -> None:
    """Disable a principal — checked live on every subsequent request, never cached
    (ADR-014 section 4). Works for a ``user`` or a ``service`` principal identically."""
    with _connected() as factory, session_scope(factory) as session:
        try:
            PrincipalRepository(session).disable(principal_id)
        except LookupError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
    typer.secho(f"Disabled: {principal_id}", fg=typer.colors.GREEN)


@app.command("enable-principal")
def enable_principal(principal_id: str) -> None:
    with _connected() as factory, session_scope(factory) as session:
        try:
            PrincipalRepository(session).enable(principal_id)
        except LookupError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
    typer.secho(f"Enabled: {principal_id}", fg=typer.colors.GREEN)


@app.command("create-service-principal")
def create_service_principal(
    display_name: str = typer.Option(..., "--name"),
    credential_ref: str = typer.Option(
        ...,
        "--credential-ref",
        help="env:NAME or keyring:SERVICE/ACCOUNT — set at that location BEFORE running this.",
    ),
) -> None:
    """Register a service principal. The credential itself is never generated,
    stored, or displayed by Operator — set it at ``credential_ref``'s target first
    (an environment variable, or a keyring entry), then point this command at it."""
    _validate_credential_ref_or_exit(credential_ref)
    with _connected() as factory, session_scope(factory) as session:
        principal = PrincipalRepository(session).create(
            kind="service", display_name=display_name, credential_ref=credential_ref
        )
        principal_id = principal.id
    typer.secho(
        f"Service principal created: {principal_id} ({display_name})", fg=typer.colors.GREEN
    )
    typer.echo(f"Credential reference: {credential_ref}")


@app.command("rotate-service-credential")
def rotate_service_credential(
    principal_id: str,
    credential_ref: str = typer.Option(
        ...,
        "--credential-ref",
        help="The new env:NAME or keyring:SERVICE/ACCOUNT — set at that location first.",
    ),
) -> None:
    """Repoint a service principal's credential reference. "Rotation" here means the
    new secret value already exists at the new reference before this command runs —
    Operator never issues or transports the credential itself, so there is nothing to
    print and nothing that could leak by printing it."""
    _validate_credential_ref_or_exit(credential_ref)
    with _connected() as factory, session_scope(factory) as session:
        try:
            PrincipalRepository(session).set_credential_ref(principal_id, credential_ref)
        except LookupError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
    typer.secho(f"Credential rotated for {principal_id}", fg=typer.colors.GREEN)
    typer.echo(f"New credential reference: {credential_ref}")


@app.command("list-service-principals")
def list_service_principals(
    include_disabled: bool = typer.Option(True, "--include-disabled/--exclude-disabled"),
) -> None:
    with _connected() as factory, factory() as session:
        principals = PrincipalRepository(session).list_service_principals(
            include_disabled=include_disabled
        )
    if not principals:
        typer.echo("No service principals.")
        return
    for principal in principals:
        status = "disabled" if principal.disabled_at is not None else "enabled"
        typer.echo(
            f"{principal.id}  {principal.display_name}  [{status}]  ref={principal.credential_ref}"
        )


__all__ = ["app"]
