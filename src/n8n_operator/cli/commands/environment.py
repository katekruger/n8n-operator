"""``n8n-operator environment`` — named environments and per-environment registry
overlays (ADR-016; stage 04).

Like ``identity.py``, every command here resolves the database URL via
:func:`n8n_operator.config.resolve_database_url` — none of these commands needs the
rest of :class:`~n8n_operator.config.Settings`. ``health`` is the one exception: it
resolves *that one environment's own* ``n8n_base_url_ref``/``n8n_api_key_ref``
(never the process-wide ``N8N_OPERATOR_N8N_BASE_URL``/``N8N_OPERATOR_N8N_API_KEY``
``cli/commands/health.py`` uses) and builds a throwaway :class:`N8nClient` for that
one check — proving connectivity to a *specific* environment without starting the MCP
server, the same "runnable from the operator's own machine" property every other
command here has.

``show-safe`` and ``list`` project the same allowlist discipline
``registry://workflows`` already uses (WORKFLOW_REGISTRY.md): never
``n8n_base_url_ref``/``n8n_api_key_ref``'s *resolved* values, and never a raw
resolved URL — only the reference string itself (``env:NAME``), which is not a
secret (ADR-006), the same distinction ``identity.py``'s own docstring draws for
``credential_ref``.

Phase 10 (v2) stage 04.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.config import (
    resolve_database_url,
    resolve_registry_path,
    resolve_secret_reference,
)
from n8n_operator.core import service
from n8n_operator.core.models import HealthCheckResult
from n8n_operator.logging_setup import register_secret
from n8n_operator.n8n.client import N8nClient
from n8n_operator.n8n.health import N8nHealth
from n8n_operator.registry.loader import (
    RegistryParseError,
    RegistryValidationError,
    load_overlay,
    load_registry,
)
from n8n_operator.registry.schema import RegistryDocument, WorkflowOverlayEntry, resolve_overlay
from n8n_operator.storage.models import Environment, WorkflowEnvironmentOverlay
from n8n_operator.storage.repository import (
    EnvironmentRepository,
    OrganizationRepository,
    WorkflowEnvironmentOverlayRepository,
)
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)

app = typer.Typer(
    help="Named environments and per-environment overlays (v2).", no_args_is_help=True
)

_PATH_OPTION = typer.Option(None, "--path", help="Registry file path (default: configured path).")


def _connected() -> tuple[Engine, sessionmaker[Session]]:
    engine = create_engine_for_url(resolve_database_url())
    return engine, create_session_factory(engine)


def _find_environment_or_exit(session: Session, environment_id: str) -> Environment:
    environment = EnvironmentRepository(session).get(environment_id)
    if environment is None:
        typer.secho(f"No such environment: {environment_id!r}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    return environment


def _active_document_or_exit(session: Session) -> RegistryDocument:
    snapshot = service.get_active_snapshot(session)
    if snapshot is None:
        typer.secho(
            "No active registry snapshot — run `n8n-operator registry reload` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    return RegistryDocument.model_validate(snapshot.document)


def _overlay_entry_from_row(
    row: WorkflowEnvironmentOverlay, *, workflow_id: str
) -> WorkflowOverlayEntry:
    """As ``core.service``'s own private helper of the same name — duplicated here in
    miniature rather than imported, the same "this command is its own composition
    root" reasoning ``cli/commands/health.py``'s module docstring gives for
    ``_CliHealthAdapter``."""
    return WorkflowOverlayEntry(
        workflow_id=workflow_id,
        n8n_workflow_id=row.n8n_workflow_id,
        definition_hash=row.definition_hash,
        trigger_path=row.trigger_path,
        trigger_secret_ref=row.trigger_secret_ref,
        approval_override=row.approval_override,  # type: ignore[arg-type]
        limits_override=row.limits_override,
    )


@app.command("create")
def create(
    organization_id: str = typer.Option(..., "--org"),
    name: str = typer.Option(..., "--name"),
    n8n_base_url_ref: str = typer.Option(
        ..., "--n8n-base-url-ref", help="e.g. env:STAGING_N8N_BASE_URL or keyring:service/account."
    ),
    n8n_api_key_ref: str = typer.Option(
        ..., "--n8n-api-key-ref", help="e.g. env:STAGING_N8N_API_KEY or keyring:service/account."
    ),
    is_production: bool = typer.Option(
        False, "--production", help="Marks this environment production (ADR-016 section 3)."
    ),
) -> None:
    """Register a new environment in an organization. Server-owned, indirected
    connection configuration only (ADR-016 section 2) — a literal secret or a raw
    URL is never accepted here; ``n8n_base_url_ref``/``n8n_api_key_ref`` are
    references, resolved fresh at the point of use, never persisted resolved."""
    engine, factory = _connected()
    try:
        with session_scope(factory) as session:
            if OrganizationRepository(session).get(organization_id) is None:
                typer.secho(
                    f"No such organization: {organization_id!r}", fg=typer.colors.RED, err=True
                )
                raise typer.Exit(code=1)
            environment = EnvironmentRepository(session).create(
                organization_id=organization_id,
                name=name,
                n8n_base_url_ref=n8n_base_url_ref,
                n8n_api_key_ref=n8n_api_key_ref,
                is_production=is_production,
            )
            environment_id = environment.id
    finally:
        engine.dispose()
    typer.secho(f"Environment created: {environment_id} ({name})", fg=typer.colors.GREEN)


@app.command("archive")
def archive(environment_id: str = typer.Argument(...)) -> None:
    """Archive an environment (ADR-016 section 4) — never a delete. An archived
    environment can no longer be targeted by a new ``prepare_operation``
    (``ENVIRONMENT_ARCHIVED``), but stays resolvable by every read tool, and by
    ``execute_operation`` for work already prepared against it, forever."""
    engine, factory = _connected()
    try:
        with session_scope(factory) as session:
            _find_environment_or_exit(session, environment_id)
            try:
                EnvironmentRepository(session).archive(environment_id)
            except LookupError as exc:
                typer.secho(str(exc), fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()
    typer.secho(f"Environment archived: {environment_id}", fg=typer.colors.GREEN)


@app.command("list")
def list_environments() -> None:
    """Every environment in every organization — the operator's own view, unlike the
    MCP ``list_environments`` tool, which is scoped to one caller's memberships and
    hides archived environments from a non-admin (ADR-016 section 4)."""
    engine, factory = _connected()
    try:
        with session_scope(factory) as session:
            organizations = OrganizationRepository(session).list()
            rows = [
                (org.id, org.name, env)
                for org in organizations
                for env in EnvironmentRepository(session).list_for_organization(
                    org.id, include_archived=True
                )
            ]
    finally:
        engine.dispose()
    if not rows:
        typer.echo("(no environments)")
        return
    for org_id, org_name, env in rows:
        marker = " [archived]" if env.archived_at is not None else ""
        prod = " [production]" if env.is_production else ""
        typer.echo(f'{env.id}{marker}{prod}  org={org_id} ({org_name})  "{env.name}"')


@app.command("show-safe")
def show_safe(environment_id: str = typer.Argument(...)) -> None:
    """One environment's safe fields only — never a resolved URL, a resolved
    credential, or (per this command's own name) anything unsafe to print to a
    terminal that might be screen-shared or logged."""
    engine, factory = _connected()
    try:
        with session_scope(factory) as session:
            environment = _find_environment_or_exit(session, environment_id)
            typer.echo(f"id:                 {environment.id}")
            typer.echo(f"name:               {environment.name}")
            typer.echo(f"organization_id:    {environment.organization_id}")
            typer.echo(f"is_production:      {environment.is_production}")
            typer.echo(f"archived:           {environment.archived_at is not None}")
            typer.echo(f"n8n_base_url_ref:   {environment.n8n_base_url_ref}")
            typer.echo(f"n8n_api_key_ref:    {environment.n8n_api_key_ref}")
    finally:
        engine.dispose()


@app.command("health")
def health(
    environment_id: str = typer.Argument(...),
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Check whether *this one environment's own* configured n8n instance is
    reachable — resolving its ``n8n_base_url_ref``/``n8n_api_key_ref`` fresh, never
    the process-wide n8n configuration ``n8n-operator health`` (v1) checks."""
    engine, factory = _connected()
    try:
        with session_scope(factory) as session:
            environment = _find_environment_or_exit(session, environment_id)
            base_url_ref, api_key_ref = environment.n8n_base_url_ref, environment.n8n_api_key_ref
    finally:
        engine.dispose()

    try:
        base_url = resolve_secret_reference(base_url_ref)
        api_key = resolve_secret_reference(api_key_ref)
    except ValueError as exc:
        typer.secho(f"Configuration does not resolve: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    register_secret(api_key)

    client = N8nClient(base_url=base_url, api_key=api_key, connect_timeout_seconds=60.0)
    raw = N8nHealth(client).check()
    result = HealthCheckResult(
        reachable=raw.reachable,
        n8n_version=raw.n8n_version,
        latency_ms=raw.latency_ms,
        reason=raw.reason,
        checked_at=raw.checked_at,
    )

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "environment_id": environment_id,
                    "reachable": result.reachable,
                    "n8n_version": result.n8n_version,
                    "latency_ms": result.latency_ms,
                    "reason": result.reason,
                    "checked_at": result.checked_at.isoformat(),
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        typer.echo(f"environment_id: {environment_id}")
        typer.echo(f"reachable:      {result.reachable}")
        if result.n8n_version is not None:
            typer.echo(f"n8n_version:    {result.n8n_version}")
        if result.latency_ms is not None:
            typer.echo(f"latency_ms:     {result.latency_ms}")
        if result.reason is not None:
            typer.echo(f"reason:         {result.reason}")
        typer.echo(f"checked_at:     {result.checked_at.isoformat()}")

    if not result.reachable:
        raise typer.Exit(code=1)


@app.command("registry-diff")
def registry_diff(
    environment_id: str = typer.Argument(...),
    path: str | None = _PATH_OPTION,
) -> None:
    """Show, per workflow, whether this environment's resolved (base + overlay)
    contract differs from the base registry — the fields an overlay may ever touch
    only (``n8n_workflow_id``, ``definition_hash``, ``trigger.path``/``secret_ref``,
    ``approval``, ``limits``; ADR-016), read live against the database's current
    overlay rows, not a frozen snapshot."""
    resolved_path = resolve_registry_path(path)
    try:
        loaded = load_registry(resolved_path, server_max_argument_bytes=2**31 - 1)
    except (RegistryValidationError, RegistryParseError) as exc:
        typer.secho(f"Registry could not be loaded: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    engine, factory = _connected()
    try:
        with session_scope(factory) as session:
            _find_environment_or_exit(session, environment_id)
            overlay_rows = {
                row.workflow_id: row
                for row in WorkflowEnvironmentOverlayRepository(session).list_for_environment(
                    environment_id
                )
            }
    finally:
        engine.dispose()

    any_diff = False
    for entry in loaded.entries:
        row = overlay_rows.get(entry.id)
        if row is None:
            continue
        overlay_entry = _overlay_entry_from_row(row, workflow_id=entry.id)
        merged = resolve_overlay(entry, overlay_entry)
        diffs = []
        if merged.n8n_workflow_id != entry.n8n_workflow_id:
            diffs.append(f"n8n_workflow_id: {entry.n8n_workflow_id} -> {merged.n8n_workflow_id}")
        if merged.definition_hash != entry.definition_hash:
            diffs.append(f"definition_hash: {entry.definition_hash} -> {merged.definition_hash}")
        if merged.trigger.path != entry.trigger.path:
            diffs.append(f"trigger.path: {entry.trigger.path} -> {merged.trigger.path}")
        if merged.approval != entry.approval:
            diffs.append(f"approval: {entry.approval} -> {merged.approval}")
        if merged.limits != entry.limits:
            diffs.append(f"limits: {entry.limits} -> {merged.limits}")
        if diffs:
            any_diff = True
            typer.echo(f"{entry.id}:")
            for line in diffs:
                typer.echo(f"  {line}")
    if not any_diff:
        typer.echo("(no differences from the base registry)")


@app.command("validate-overlay")
def validate_overlay(
    environment_id: str = typer.Argument(...),
    overlay_path: Path = typer.Option(..., "--path", help="Overlay YAML file to validate."),
) -> None:
    """Validate an overlay file (rules R13-R14, ADR-016) against the currently active
    base registry snapshot, without persisting anything — a dry run for
    ``reload-overlay``."""
    engine, factory = _connected()
    try:
        with session_scope(factory) as session:
            _find_environment_or_exit(session, environment_id)
            document = _active_document_or_exit(session)
            try:
                loaded = load_overlay(
                    overlay_path, base_document=document, base_resolved_entries=document.workflows
                )
            except RegistryValidationError as exc:
                typer.secho(
                    f"Overlay is invalid ({len(exc.violations)} problem(s)):",
                    fg=typer.colors.RED,
                    err=True,
                )
                for violation in exc.violations:
                    typer.echo(f"  {violation.format()}", err=True)
                raise typer.Exit(code=1) from exc
            except RegistryParseError as exc:
                typer.secho(
                    f"Overlay could not be parsed: {exc.message}", fg=typer.colors.RED, err=True
                )
                raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()
    typer.secho("Overlay is valid.", fg=typer.colors.GREEN)
    typer.echo(f"  workflows overlaid: {len(loaded.overlays)}")


@app.command("reload-overlay")
def reload_overlay_command(
    environment_id: str = typer.Argument(...),
    overlay_path: Path = typer.Option(..., "--path", help="Overlay YAML file to load."),
) -> None:
    """Validate and persist an environment's overlay (ADR-016) — the full set of
    overlays this environment has after this call is exactly what ``overlay_path``
    names; a workflow overlaid before but no longer named in the file is no longer
    overridden (``core.service.reload_overlay``'s own docstring)."""
    engine, factory = _connected()
    try:
        with session_scope(factory) as session:
            _find_environment_or_exit(session, environment_id)
            try:
                loaded = service.reload_overlay(
                    session, overlay_path, environment_id=environment_id
                )
            except RegistryValidationError as exc:
                typer.secho(
                    f"Overlay is invalid ({len(exc.violations)} problem(s)):",
                    fg=typer.colors.RED,
                    err=True,
                )
                for violation in exc.violations:
                    typer.echo(f"  {violation.format()}", err=True)
                raise typer.Exit(code=1) from exc
            except RegistryParseError as exc:
                typer.secho(
                    f"Overlay could not be parsed: {exc.message}", fg=typer.colors.RED, err=True
                )
                raise typer.Exit(code=1) from exc
            except OperationalError as exc:
                typer.secho(
                    "Database is not initialized — run `n8n-operator db init` first.",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()
    typer.secho("Overlay reloaded.", fg=typer.colors.GREEN)
    typer.echo(f"  environment_id:     {environment_id}")
    typer.echo(f"  workflows overlaid: {len(loaded.overlays)}")


__all__ = ["app"]
