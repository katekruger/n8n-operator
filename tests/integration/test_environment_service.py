"""Stage 04 (ADR-016): environment resolution, overlays, and archival — against a real
database. Overlay parse/rule tests (R13/R14) live in
``tests/property/test_overlay_properties.py``; this file covers the scenarios that need
a real principal/organization/environment graph.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.core import service
from n8n_operator.core.models import PreflightResult
from n8n_operator.errors import (
    EnvironmentArchivedError,
    EnvironmentNotFoundError,
    EnvironmentRequiredError,
    OperationNotFoundError,
)
from n8n_operator.registry.loader import RegistryValidationError
from n8n_operator.storage.models import Organization
from n8n_operator.storage.repository import (
    EnvironmentRepository,
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import session_scope

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: env-test
workflows:
  - id: crm.sync_contact
    n8n_workflow_id: n8n-1
    title: Sync contact
    description: Read-only sync.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_a}
    risk: low
    side_effects: read_only
    approval: none
    trigger:
      type: webhook
      method: POST
      path: /webhook/a
      auth: none
    input_schema:
      type: object
      properties: {{}}
      additionalProperties: false
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300
""".format(hash_a="a" * 64)


class FakePreflight:
    def check(self, workflow: Any) -> PreflightResult:
        return PreflightResult(ready=True, checks=[], checked_at=datetime.now(UTC))


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    path = tmp_path / "workflows.yaml"
    path.write_text(REGISTRY_YAML)
    return path


@pytest.fixture
def loaded(session_factory: sessionmaker[Session], registry_path: Path) -> sessionmaker[Session]:
    with session_scope(session_factory) as session:
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
    return session_factory


def _make_org(session: Session, *, name: str) -> Organization:
    return OrganizationRepository(session).create(name=name)


def _make_env(session: Session, *, org_id: str, name: str, is_production: bool = False) -> str:
    return (
        EnvironmentRepository(session)
        .create(
            organization_id=org_id,
            name=name,
            n8n_base_url_ref="env:N8N_TEST_BASE_URL",
            n8n_api_key_ref="env:N8N_TEST_API_KEY",
            is_production=is_production,
        )
        .id
    )


def _make_member(session: Session, *, org_id: str, roles: list[str]) -> str:
    principal = PrincipalRepository(session).create(kind="user", display_name="P")
    OrganizationMembershipRepository(session).create(
        principal_id=principal.id, organization_id=org_id, roles=roles, workflow_scope="*"
    )
    return principal.id


# ----------------------------------------------------------------------------------
# Default-environment resolution (AC-37).
# ----------------------------------------------------------------------------------


@pytest.mark.integration
def test_single_environment_resolves_implicitly(loaded: sessionmaker[Session]) -> None:
    with session_scope(loaded) as session:
        org = _make_org(session, name="Acme")
        _make_env(session, org_id=org.id, name="only")
        principal_id = _make_member(session, org_id=org.id, roles=["viewer"])

    with session_scope(loaded) as session:
        detail = service.describe_workflow(
            session, workflow_id="crm.sync_contact", principal_id=principal_id, enable_v2=True
        )
        assert detail.workflow_id == "crm.sync_contact"


@pytest.mark.integration
def test_two_environments_require_explicit_naming_even_when_one_is_production(
    loaded: sessionmaker[Session],
) -> None:
    """ADR-016 section 3's own explicit refusal: production is never an implicit
    default merely because it is the only *other* environment."""
    with session_scope(loaded) as session:
        org = _make_org(session, name="Acme")
        _make_env(session, org_id=org.id, name="staging")
        prod_id = _make_env(session, org_id=org.id, name="production", is_production=True)
        principal_id = _make_member(session, org_id=org.id, roles=["viewer"])

    with session_scope(loaded) as session, pytest.raises(EnvironmentRequiredError):
        service.describe_workflow(
            session, workflow_id="crm.sync_contact", principal_id=principal_id, enable_v2=True
        )

    with session_scope(loaded) as session:
        detail = service.describe_workflow(
            session,
            workflow_id="crm.sync_contact",
            principal_id=principal_id,
            enable_v2=True,
            environment=prod_id,
        )
        assert detail.workflow_id == "crm.sync_contact"


@pytest.mark.integration
def test_no_visible_environment_is_environment_required(loaded: sessionmaker[Session]) -> None:
    with session_scope(loaded) as session:
        org = _make_org(session, name="Acme")
        principal_id = _make_member(session, org_id=org.id, roles=["viewer"])

    with session_scope(loaded) as session, pytest.raises(EnvironmentRequiredError):
        service.describe_workflow(
            session, workflow_id="crm.sync_contact", principal_id=principal_id, enable_v2=True
        )


@pytest.mark.integration
def test_unknown_or_unauthorized_environment_id_is_environment_not_found(
    loaded: sessionmaker[Session],
) -> None:
    """No enumeration oracle: a nonexistent ID and one that belongs to an org this
    caller isn't a member of are indistinguishable (ADR-015 section 3's own rule,
    extended to environments)."""
    with session_scope(loaded) as session:
        org_a = _make_org(session, name="Org A")
        org_b = _make_org(session, name="Org B")
        other_env_id = _make_env(session, org_id=org_b.id, name="only")
        principal_id = _make_member(session, org_id=org_a.id, roles=["viewer"])

    with session_scope(loaded) as session, pytest.raises(EnvironmentNotFoundError):
        service.describe_workflow(
            session,
            workflow_id="crm.sync_contact",
            principal_id=principal_id,
            enable_v2=True,
            environment=other_env_id,
        )

    with session_scope(loaded) as session, pytest.raises(EnvironmentNotFoundError):
        service.describe_workflow(
            session,
            workflow_id="crm.sync_contact",
            principal_id=principal_id,
            enable_v2=True,
            environment="does-not-exist",
        )


# ----------------------------------------------------------------------------------
# Archival (AC-47).
# ----------------------------------------------------------------------------------


@pytest.mark.integration
def test_archived_environment_rejects_new_prepare_but_stays_readable(
    loaded: sessionmaker[Session],
) -> None:
    with session_scope(loaded) as session:
        org = _make_org(session, name="Acme")
        env_id = _make_env(session, org_id=org.id, name="staging")
        principal_id = _make_member(session, org_id=org.id, roles=["operator"])
        EnvironmentRepository(session).archive(env_id)

    with session_scope(loaded) as session, pytest.raises(EnvironmentArchivedError):
        service.prepare_operation(
            session,
            principal_id=principal_id,
            environment=env_id,
            workflow_id="crm.sync_contact",
            arguments={},
            preflight=FakePreflight(),
            server_max_argument_bytes=262_144,
            enable_v2=True,
        )

    with session_scope(loaded) as session:
        # Still resolvable and describable — archival is never erasure.
        detail = service.describe_workflow(
            session,
            workflow_id="crm.sync_contact",
            principal_id=principal_id,
            enable_v2=True,
            environment=env_id,
        )
        assert detail.workflow_id == "crm.sync_contact"


@pytest.mark.integration
def test_operation_prepared_before_archival_may_still_execute(
    loaded: sessionmaker[Session],
) -> None:
    with session_scope(loaded) as session:
        org = _make_org(session, name="Acme")
        env_id = _make_env(session, org_id=org.id, name="staging")
        principal_id = _make_member(session, org_id=org.id, roles=["operator"])

    with session_scope(loaded) as session:
        operation, _replay, _token = service.prepare_operation(
            session,
            principal_id=principal_id,
            environment=env_id,
            workflow_id="crm.sync_contact",
            arguments={},
            preflight=FakePreflight(),
            server_max_argument_bytes=262_144,
            enable_v2=True,
        )
        operation_id, handle = operation.id, operation.id
        assert operation.state == "APPROVED"  # approval: none

    with session_scope(loaded) as session:
        EnvironmentRepository(session).archive(env_id)

    with session_scope(loaded) as session:
        # execute_operation never forbids an archived environment — only *new*
        # prepare_operation calls are refused (ADR-016 section 4).
        executed = service.execute_operation(
            session,
            operation_id=operation_id,
            handle=handle,
            principal_id=principal_id,
            preflight=FakePreflight(),
            enable_v2=True,
        )
        assert executed.state == "EXECUTING"


# ----------------------------------------------------------------------------------
# Overlays — strengthening, environment-scoped visibility.
# ----------------------------------------------------------------------------------

OVERLAY_YAML_STRENGTHEN = """apiVersion: n8n-operator/v1
metadata:
  name: staging-overlay
overlays:
  - workflow_id: crm.sync_contact
    approval_override: required
"""

OVERLAY_YAML_UNKNOWN_WORKFLOW = """apiVersion: n8n-operator/v1
metadata:
  name: bad-overlay
overlays:
  - workflow_id: does.not.exist
    approval_override: required
"""


@pytest.mark.integration
def test_overlay_strengthens_approval_for_one_environment_only(
    loaded: sessionmaker[Session], tmp_path: Path
) -> None:
    overlay_path = tmp_path / "staging.yaml"
    overlay_path.write_text(OVERLAY_YAML_STRENGTHEN)

    with session_scope(loaded) as session:
        org = _make_org(session, name="Acme")
        staging_id = _make_env(session, org_id=org.id, name="staging")
        prod_id = _make_env(session, org_id=org.id, name="production", is_production=True)
        principal_id = _make_member(session, org_id=org.id, roles=["viewer"])

    with session_scope(loaded) as session:
        service.reload_overlay(session, overlay_path, environment_id=staging_id)

    with session_scope(loaded) as session:
        staging_detail = service.describe_workflow(
            session,
            workflow_id="crm.sync_contact",
            principal_id=principal_id,
            enable_v2=True,
            environment=staging_id,
        )
        prod_detail = service.describe_workflow(
            session,
            workflow_id="crm.sync_contact",
            principal_id=principal_id,
            enable_v2=True,
            environment=prod_id,
        )
        assert staging_detail.approval == "required"
        assert prod_detail.approval == "none"


@pytest.mark.integration
def test_overlay_naming_an_unknown_workflow_is_rejected(
    loaded: sessionmaker[Session], tmp_path: Path
) -> None:
    overlay_path = tmp_path / "bad.yaml"
    overlay_path.write_text(OVERLAY_YAML_UNKNOWN_WORKFLOW)

    with session_scope(loaded) as session:
        org = _make_org(session, name="Acme")
        env_id = _make_env(session, org_id=org.id, name="staging")

    with session_scope(loaded) as session, pytest.raises(RegistryValidationError) as excinfo:
        service.reload_overlay(session, overlay_path, environment_id=env_id)
    assert any(v.rule == "R13" for v in excinfo.value.violations)


@pytest.mark.integration
def test_reload_overlay_removes_overrides_no_longer_named_in_the_file(
    loaded: sessionmaker[Session], tmp_path: Path
) -> None:
    overlay_path = tmp_path / "staging.yaml"
    overlay_path.write_text(OVERLAY_YAML_STRENGTHEN)

    with session_scope(loaded) as session:
        org = _make_org(session, name="Acme")
        env_id = _make_env(session, org_id=org.id, name="staging")
        principal_id = _make_member(session, org_id=org.id, roles=["viewer"])

    with session_scope(loaded) as session:
        service.reload_overlay(session, overlay_path, environment_id=env_id)
    with session_scope(loaded) as session:
        detail = service.describe_workflow(
            session,
            workflow_id="crm.sync_contact",
            principal_id=principal_id,
            enable_v2=True,
            environment=env_id,
        )
        assert detail.approval == "required"

    empty_overlay_path = tmp_path / "empty.yaml"
    empty_overlay_path.write_text(
        "apiVersion: n8n-operator/v1\nmetadata:\n  name: empty\noverlays: []\n"
    )
    with session_scope(loaded) as session:
        service.reload_overlay(session, empty_overlay_path, environment_id=env_id)
    with session_scope(loaded) as session:
        detail = service.describe_workflow(
            session,
            workflow_id="crm.sync_contact",
            principal_id=principal_id,
            enable_v2=True,
            environment=env_id,
        )
        assert detail.approval == "none"


# ----------------------------------------------------------------------------------
# Idempotency keys across environments; cross-environment operation access.
# ----------------------------------------------------------------------------------


@pytest.mark.integration
def test_idempotency_key_reused_across_environments_does_not_collide(
    loaded: sessionmaker[Session],
) -> None:
    with session_scope(loaded) as session:
        org = _make_org(session, name="Acme")
        staging_id = _make_env(session, org_id=org.id, name="staging")
        prod_id = _make_env(session, org_id=org.id, name="production", is_production=True)
        principal_id = _make_member(session, org_id=org.id, roles=["operator"])

    with session_scope(loaded) as session:
        staging_op, staging_replay, _ = service.prepare_operation(
            session,
            principal_id=principal_id,
            environment=staging_id,
            workflow_id="crm.sync_contact",
            arguments={},
            preflight=FakePreflight(),
            server_max_argument_bytes=262_144,
            idempotency_key="same-key",
            enable_v2=True,
        )
        prod_op, prod_replay, _ = service.prepare_operation(
            session,
            principal_id=principal_id,
            environment=prod_id,
            workflow_id="crm.sync_contact",
            arguments={},
            preflight=FakePreflight(),
            server_max_argument_bytes=262_144,
            idempotency_key="same-key",
            enable_v2=True,
        )
    assert staging_op.id != prod_op.id
    assert staging_replay is False
    assert prod_replay is False


@pytest.mark.integration
def test_cross_environment_operation_access_is_denied(loaded: sessionmaker[Session]) -> None:
    """A principal who only belongs to org B cannot reach an operation prepared in
    org A's environment — the same ownership/scope boundary Stage 03 already proved
    across organizations, now proved across environments."""
    with session_scope(loaded) as session:
        org_a = _make_org(session, name="Org A")
        org_b = _make_org(session, name="Org B")
        env_a_id = _make_env(session, org_id=org_a.id, name="only")
        _make_env(session, org_id=org_b.id, name="only")
        alice_id = _make_member(session, org_id=org_a.id, roles=["operator"])
        bob_id = _make_member(session, org_id=org_b.id, roles=["admin"])

    with session_scope(loaded) as session:
        operation, _replay, _token = service.prepare_operation(
            session,
            principal_id=alice_id,
            environment=env_a_id,
            workflow_id="crm.sync_contact",
            arguments={},
            preflight=FakePreflight(),
            server_max_argument_bytes=262_144,
            enable_v2=True,
        )
        operation_id = operation.id

    with session_scope(loaded) as session, pytest.raises(OperationNotFoundError):
        service.get_operation(
            session, operation_id=operation_id, principal_id=bob_id, enable_v2=True
        )


# ----------------------------------------------------------------------------------
# list_operations' own `environment` filter argument (MCP_TOOLS.md section 5.9).
# ----------------------------------------------------------------------------------


@pytest.mark.integration
def test_list_operations_environment_filter_scopes_to_one_environment(
    loaded: sessionmaker[Session],
) -> None:
    with session_scope(loaded) as session:
        org = _make_org(session, name="Acme")
        staging_id = _make_env(session, org_id=org.id, name="staging")
        prod_id = _make_env(session, org_id=org.id, name="production", is_production=True)
        principal_id = _make_member(session, org_id=org.id, roles=["operator"])

    with session_scope(loaded) as session:
        staging_op, _, _ = service.prepare_operation(
            session,
            principal_id=principal_id,
            environment=staging_id,
            workflow_id="crm.sync_contact",
            arguments={},
            preflight=FakePreflight(),
            server_max_argument_bytes=262_144,
            enable_v2=True,
        )
        prod_op, _, _ = service.prepare_operation(
            session,
            principal_id=principal_id,
            environment=prod_id,
            workflow_id="crm.sync_contact",
            arguments={},
            preflight=FakePreflight(),
            server_max_argument_bytes=262_144,
            enable_v2=True,
        )

    with session_scope(loaded) as session, pytest.raises(EnvironmentRequiredError):
        # Two environments visible, none named — the same standard-resolution rule
        # every other v2 tool follows (MCP_TOOLS.md section 5.9).
        service.list_operations(session, principal_id=principal_id, enable_v2=True)

    with session_scope(loaded) as session:
        staging_page = service.list_operations(
            session, principal_id=principal_id, environment=staging_id, enable_v2=True
        )
        prod_page = service.list_operations(
            session, principal_id=principal_id, environment=prod_id, enable_v2=True
        )
    assert {op.id for op in staging_page} == {staging_op.id}
    assert {op.id for op in prod_page} == {prod_op.id}


# ----------------------------------------------------------------------------------
# list_workflows' own v2 pagination (MCP_TOOLS.md section 5.9, "same shape as v1
# list_operations").
# ----------------------------------------------------------------------------------

_MULTI_WORKFLOW_REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: pagination-test
workflows:
{entries}
""".format(
    entries="\n".join(
        f"""  - id: wf.{n:02d}
    n8n_workflow_id: n8n-{n}
    title: Workflow {n}
    description: d
    owner: carolyn
    version: 1
    definition_hash: sha256:{n:064d}
    risk: low
    side_effects: read_only
    approval: none
    trigger:
      type: webhook
      method: POST
      path: /webhook/{n}
      auth: none
    input_schema:
      type: object
      properties: {{}}
      additionalProperties: false
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300"""
        for n in range(5)
    )
)


@pytest.mark.integration
def test_list_workflows_paginates_in_v2_mode_only(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    registry_path = tmp_path / "many.yaml"
    registry_path.write_text(_MULTI_WORKFLOW_REGISTRY_YAML)
    with session_scope(session_factory) as session:
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
        org = _make_org(session, name="Acme")
        _make_env(session, org_id=org.id, name="only")
        principal_id = _make_member(session, org_id=org.id, roles=["viewer"])

    with session_scope(session_factory) as session:
        # v1: unpaginated, every entry, regardless of `limit`.
        v1_summaries = service.list_workflows(session, limit=2)
        assert len(v1_summaries) == 5

    with session_scope(session_factory) as session:
        page1 = service.list_workflows(session, principal_id=principal_id, enable_v2=True, limit=2)
        assert [s.workflow_id for s in page1] == ["wf.00", "wf.01"]
        page2 = service.list_workflows(
            session,
            principal_id=principal_id,
            enable_v2=True,
            limit=2,
            cursor=page1[-1].workflow_id,
        )
        assert [s.workflow_id for s in page2] == ["wf.02", "wf.03"]
        page3 = service.list_workflows(
            session,
            principal_id=principal_id,
            enable_v2=True,
            limit=2,
            cursor=page2[-1].workflow_id,
        )
        assert [s.workflow_id for s in page3] == ["wf.04"]
