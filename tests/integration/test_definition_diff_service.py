"""``core.service.diff_workflow_definition`` (stage 07, ADR-008, MCP_TOOLS.md section
5.6) against a real database: no snapshot → honest hash comparison with an empty,
clearly-flagged diff; a captured snapshot → a real itemized diff; redaction actually
masks values; a credential-id change is visible without ever echoing the raw id;
AC-44's bitwise-identical unauthorized-vs-nonexistent requirement; error mapping;
``viewer`` alone is sufficient in v2 mode.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.core import service
from n8n_operator.errors import (
    InstanceUnreachableError,
    WorkflowMissingOnInstanceError,
    WorkflowNotFoundError,
)
from n8n_operator.storage.repository import (
    EnvironmentRepository,
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
    WorkflowDefinitionSnapshotRepository,
)
from n8n_operator.storage.session import session_scope

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: definition-diff-test
workflows:
  - id: crm.sync_contact
    n8n_workflow_id: n8n-real-1
    title: Sync a contact into the CRM
    description: External write.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_a}
    risk: medium
    side_effects: external_write
    approval: required
    trigger:
      type: webhook
      method: POST
      path: /webhook/a
      auth: none
    input_schema:
      type: object
      properties: {{}}
      additionalProperties: false
    output:
      redact: ["$.secretField"]
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300
""".format(hash_a="a" * 64)

_REGISTERED_RAW: dict[str, Any] = {
    "id": "n8n-real-1",
    "name": "Sync a contact",
    "nodes": [
        {
            "id": "node-1",
            "name": "Webhook",
            "type": "n8n-nodes-base.webhook",
            "position": [0, 0],
            "parameters": {},
        },
        {
            "id": "node-2",
            "name": "Set",
            "type": "n8n-nodes-base.set",
            "position": [200, 0],
            "parameters": {"url": "https://old.example.com"},
        },
    ],
    "connections": {"Webhook": {"main": [[{"node": "Set", "type": "main", "index": 0}]]}},
    "settings": {},
    "pinData": {},
}


class FakeDefinitionPort:
    def __init__(self, raw: dict[str, Any] | None = None, *, unreachable: bool = False) -> None:
        self._raw = raw
        self._unreachable = unreachable

    def get_workflow(self, n8n_workflow_id: str) -> dict[str, Any]:
        if self._unreachable:
            raise InstanceUnreachableError()
        if self._raw is None:
            raise WorkflowMissingOnInstanceError()
        return self._raw


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    path = tmp_path / "workflows.yaml"
    path.write_text(REGISTRY_YAML)
    return path


@pytest.fixture
def loaded(session_factory: sessionmaker[Session], registry_path: Path) -> sessionmaker[Session]:
    with session_scope(session_factory) as session:
        PrincipalRepository(session).create(id="local", kind="local", display_name="local")
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
    return session_factory


def test_no_stored_snapshot_still_gives_an_honest_hash_comparison(
    loaded: sessionmaker[Session],
) -> None:
    live = {**_REGISTERED_RAW, "nodes": [*_REGISTERED_RAW["nodes"]]}
    live["nodes"][1] = {**live["nodes"][1], "parameters": {"url": "https://new.example.com"}}
    with session_scope(loaded) as session:
        result = service.diff_workflow_definition(
            session,
            workflow_id="crm.sync_contact",
            definition=FakeDefinitionPort(live),
            principal_id="local",
        )
    assert result.diff_available is False
    assert result.diff == []
    assert result.changed is True
    assert result.note is not None


def test_captured_snapshot_yields_a_real_itemized_diff(loaded: sessionmaker[Session]) -> None:
    with session_scope(loaded) as session:
        WorkflowDefinitionSnapshotRepository(session).create(
            workflow_id="crm.sync_contact",
            definition_hash="sha256:" + "a" * 64,
            canonical_definition={
                "nodes": [
                    {k: v for k, v in n.items() if k != "position"}
                    for n in _REGISTERED_RAW["nodes"]
                ],
                "connections": _REGISTERED_RAW["connections"],
                "settings": _REGISTERED_RAW["settings"],
            },
            captured_by="local",
        )

    live = {**_REGISTERED_RAW}
    live["nodes"] = [
        _REGISTERED_RAW["nodes"][0],
        {**_REGISTERED_RAW["nodes"][1], "parameters": {"url": "https://new.example.com"}},
    ]
    with session_scope(loaded) as session:
        result = service.diff_workflow_definition(
            session,
            workflow_id="crm.sync_contact",
            definition=FakeDefinitionPort(live),
            principal_id="local",
        )
    assert result.diff_available is True
    assert result.changed is True
    assert len(result.diff) == 1
    assert result.diff[0].path == "/nodes/1/parameters/url"
    assert result.diff[0].registered_value == "https://old.example.com"
    assert result.diff[0].live_value == "https://new.example.com"


def test_output_redact_masks_matching_diff_values(loaded: sessionmaker[Session]) -> None:
    with session_scope(loaded) as session:
        WorkflowDefinitionSnapshotRepository(session).create(
            workflow_id="crm.sync_contact",
            definition_hash="sha256:" + "a" * 64,
            canonical_definition={
                "nodes": [
                    {k: v for k, v in n.items() if k != "position"}
                    for n in _REGISTERED_RAW["nodes"]
                ],
                "connections": _REGISTERED_RAW["connections"],
                "settings": {"secretField": "old-secret"},
            },
            captured_by="local",
        )

    live = {**_REGISTERED_RAW, "settings": {"secretField": "new-secret"}}
    with session_scope(loaded) as session:
        result = service.diff_workflow_definition(
            session,
            workflow_id="crm.sync_contact",
            definition=FakeDefinitionPort(live),
            principal_id="local",
        )
    entry = next(e for e in result.diff if e.path == "/settings/secretField")
    assert entry.registered_value == "[REDACTED]"
    assert entry.live_value == "[REDACTED]"


def test_credential_id_change_is_visible_without_echoing_raw_ids(
    loaded: sessionmaker[Session],
) -> None:
    node_with_cred = {
        "id": "node-3",
        "name": "Enrich",
        "type": "n8n-nodes-base.httpRequest",
        "position": [400, 0],
        "parameters": {},
        "credentials": {"httpBasicAuth": {"id": "cred-old", "name": "Old Cred"}},
    }
    with session_scope(loaded) as session:
        WorkflowDefinitionSnapshotRepository(session).create(
            workflow_id="crm.sync_contact",
            definition_hash="sha256:" + "a" * 64,
            canonical_definition={
                "nodes": [{k: v for k, v in node_with_cred.items() if k != "position"}],
                "connections": {},
                "settings": {},
            },
            captured_by="local",
        )

    live = {
        **_REGISTERED_RAW,
        "nodes": [
            {
                **node_with_cred,
                "credentials": {"httpBasicAuth": {"id": "cred-new", "name": "New Cred"}},
            }
        ],
        "connections": {},
    }
    with session_scope(loaded) as session:
        result = service.diff_workflow_definition(
            session,
            workflow_id="crm.sync_contact",
            definition=FakeDefinitionPort(live),
            principal_id="local",
        )
    entry = next(e for e in result.diff if e.path == "/nodes/0/credentials/httpBasicAuth/id")
    assert entry.change_type == "modified"
    assert "cred-old" not in str(entry.registered_value)
    assert "cred-new" not in str(entry.live_value)
    assert entry.registered_value != entry.live_value
    assert str(entry.registered_value).startswith("[REDACTED:")


def test_workflow_not_found_for_both_nonexistent_and_unauthorized_are_bitwise_identical(
    session_factory: sessionmaker[Session], registry_path: Path
) -> None:
    with session_scope(session_factory) as session:
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
        org = OrganizationRepository(session).create(name="Acme")
        env = EnvironmentRepository(session).create(
            organization_id=org.id,
            name="production",
            n8n_base_url_ref="env:X",
            n8n_api_key_ref="env:Y",
        )
        authorized = PrincipalRepository(session).create(kind="user", display_name="Authorized")
        unauthorized = PrincipalRepository(session).create(kind="user", display_name="Unauthorized")
        OrganizationMembershipRepository(session).create(
            principal_id=authorized.id,
            organization_id=org.id,
            roles=["viewer"],
            workflow_scope="crm.*",
        )
        OrganizationMembershipRepository(session).create(
            principal_id=unauthorized.id,
            organization_id=org.id,
            roles=["viewer"],
            workflow_scope="sales.*",
        )
        env_id, unauthorized_id = env.id, unauthorized.id

    with (
        session_scope(session_factory) as session,
        pytest.raises(WorkflowNotFoundError) as unauthorized_exc,
    ):
        service.diff_workflow_definition(
            session,
            workflow_id="crm.sync_contact",
            definition=FakeDefinitionPort(_REGISTERED_RAW),
            principal_id=unauthorized_id,
            enable_v2=True,
            environment=env_id,
        )
    with (
        session_scope(session_factory) as session,
        pytest.raises(WorkflowNotFoundError) as nonexistent_exc,
    ):
        service.diff_workflow_definition(
            session,
            workflow_id="does.not.exist",
            definition=FakeDefinitionPort(_REGISTERED_RAW),
            principal_id=unauthorized_id,
            enable_v2=True,
            environment=env_id,
        )
    assert unauthorized_exc.value.to_dict() == nonexistent_exc.value.to_dict()


def test_viewer_role_alone_is_sufficient_in_v2_mode(
    session_factory: sessionmaker[Session], registry_path: Path
) -> None:
    with session_scope(session_factory) as session:
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
        org = OrganizationRepository(session).create(name="Acme")
        env = EnvironmentRepository(session).create(
            organization_id=org.id,
            name="production",
            n8n_base_url_ref="env:X",
            n8n_api_key_ref="env:Y",
        )
        viewer = PrincipalRepository(session).create(kind="user", display_name="Viewer")
        OrganizationMembershipRepository(session).create(
            principal_id=viewer.id, organization_id=org.id, roles=["viewer"]
        )
        env_id, viewer_id = env.id, viewer.id

    with session_scope(session_factory) as session:
        result = service.diff_workflow_definition(
            session,
            workflow_id="crm.sync_contact",
            definition=FakeDefinitionPort(_REGISTERED_RAW),
            principal_id=viewer_id,
            enable_v2=True,
            environment=env_id,
        )
    # This test's own point is that `viewer` alone is sufficient authorization in v2
    # mode — not any particular hash outcome, which depends on the registry's
    # placeholder hash never matching a real computed one.
    assert result.diff_available is False
    assert isinstance(result.changed, bool)


def test_workflow_missing_on_instance_propagates(loaded: sessionmaker[Session]) -> None:
    with session_scope(loaded) as session, pytest.raises(WorkflowMissingOnInstanceError):
        service.diff_workflow_definition(
            session,
            workflow_id="crm.sync_contact",
            definition=FakeDefinitionPort(None),
            principal_id="local",
        )


def test_instance_unreachable_propagates(loaded: sessionmaker[Session]) -> None:
    with session_scope(loaded) as session, pytest.raises(InstanceUnreachableError):
        service.diff_workflow_definition(
            session,
            workflow_id="crm.sync_contact",
            definition=FakeDefinitionPort(unreachable=True),
            principal_id="local",
        )
