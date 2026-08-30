"""The six sanitized GTM-scenario fixture pairs (stage 07, ``tests/fixtures/diff/``)
demonstrated through the real ``core.service.diff_workflow_definition`` call — not just
the pure algorithm (already exhaustively covered by ``tests/unit/test_definition_diff.py``
and the Hypothesis properties). Each pair also exercises one named edge case from the
stage 07 prompt: node reordering, a renamed node, duplicate node names, a large
expression value, an unrecognized/future n8n field, and Unicode in a node name.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.core import service
from n8n_operator.n8n.canonicalization import canonical_form, compute_definition_hash
from n8n_operator.storage.repository import (
    PrincipalRepository,
    WorkflowDefinitionSnapshotRepository,
)
from n8n_operator.storage.session import session_scope

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "diff"

SCENARIOS = [
    "crm_field_mapping",
    "campaign_audience_filter",
    "enrichment_credential_binding",
    "webhook_response_correlation",
    "branching",
    "error_handling",
]


def _load(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    registered = json.loads((FIXTURES_DIR / f"{name}.registered.json").read_text())
    live = json.loads((FIXTURES_DIR / f"{name}.live.json").read_text())
    return registered, live


class FakeDefinitionPort:
    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = raw

    def get_workflow(self, n8n_workflow_id: str) -> dict[str, Any]:
        return self._raw


def _registry_yaml(*, n8n_workflow_id: str, definition_hash: str) -> str:
    return f"""apiVersion: n8n-operator/v1
metadata:
  name: diff-fixtures-test
workflows:
  - id: gtm.scenario
    n8n_workflow_id: {n8n_workflow_id}
    title: GTM scenario workflow
    description: Sanitized fixture-backed scenario.
    owner: carolyn
    version: 1
    definition_hash: {definition_hash}
    risk: medium
    side_effects: external_write
    approval: required
    trigger:
      type: webhook
      method: POST
      path: /webhook/gtm
      auth: none
    input_schema:
      type: object
      properties: {{}}
      additionalProperties: false
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300
"""


@pytest.mark.integration
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_gtm_fixture_pair_produces_a_real_diff_through_the_service(
    scenario: str, session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    registered_raw, live_raw = _load(scenario)
    registered_hash = compute_definition_hash(registered_raw)
    live_hash = compute_definition_hash(live_raw)
    assert registered_hash != live_hash, f"{scenario} fixture pair must actually differ"

    registry_path = tmp_path / "workflows.yaml"
    registry_path.write_text(
        _registry_yaml(n8n_workflow_id="n8n-gtm-1", definition_hash=registered_hash)
    )

    with session_scope(session_factory) as session:
        PrincipalRepository(session).create(id="local", kind="local", display_name="local")
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)

    with session_scope(session_factory) as session:
        WorkflowDefinitionSnapshotRepository(session).create(
            workflow_id="gtm.scenario",
            definition_hash=registered_hash,
            canonical_definition=canonical_form(registered_raw),
            captured_by="local",
        )

    with session_scope(session_factory) as session:
        result = service.diff_workflow_definition(
            session,
            workflow_id="gtm.scenario",
            definition=FakeDefinitionPort(live_raw),
            principal_id="local",
        )

    assert result.diff_available is True
    assert result.changed is True
    assert result.diff, f"{scenario} produced no diff entries"
    assert result.registered_hash == registered_hash
    assert result.live_hash == live_hash
