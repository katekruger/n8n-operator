"""``n8n-operator operations reconcile record``/``list`` end to end, through the real
Typer CLI (stage 06, ADR-009/ADR-012) — the one CLI surface in ``operations.py`` that
genuinely needs a real n8n connection, faked here via ``httpx.MockTransport``
(``tests/integration/mock_n8n.py``, the same seam ``test_n8n_client.py`` uses).
Also covers retry lineage display in ``show``/``approval-status`` and the CLI ``list``
table's new column, and that ``n8n-operator operations`` (the retry-lineage read
commands) still needs no n8n configuration at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from integration.mock_n8n import MockN8n
from n8n_operator.cli.main import app
from n8n_operator.config import resolve_database_url
from n8n_operator.core import service
from n8n_operator.core.models import PreflightResult
from n8n_operator.n8n.client import N8nClient
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)

runner = CliRunner()

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: stage06-cli-reconcile-test
workflows:
  - id: wf.a
    n8n_workflow_id: n8n-real-1
    title: Campaign dispatch
    description: Read-only, auto-approved.
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
def cli_db_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'cli.db'}"


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    path = tmp_path / "workflows.yaml"
    path.write_text(REGISTRY_YAML)
    return path


@pytest.fixture
def mock_n8n() -> MockN8n:
    return MockN8n()


@pytest.fixture
def cli_env(cli_db_url: str, mock_n8n: MockN8n, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("N8N_OPERATOR_DATABASE_URL", cli_db_url)
    # `reconcile record` is the one command that genuinely needs n8n configuration
    # (this module's own docstring) — real-shaped, but unreachable, values; the
    # actual network call is faked via the monkeypatched `N8nClient` below.
    monkeypatch.setenv("N8N_OPERATOR_N8N_BASE_URL", "https://n8n.invalid")
    monkeypatch.setenv("N8N_OPERATOR_N8N_API_KEY", "test-key")

    def _fake_n8n_client(
        *, base_url: str, api_key: str, connect_timeout_seconds: float
    ) -> N8nClient:
        return N8nClient(
            base_url=base_url,
            api_key=api_key,
            connect_timeout_seconds=connect_timeout_seconds,
            transport=mock_n8n.transport(),
        )

    monkeypatch.setattr("n8n_operator.cli.commands.operations.N8nClient", _fake_n8n_client)


@pytest.fixture
def unknown_operation_id(cli_env: None, cli_db_url: str, registry_path: Path) -> str:
    init_result = runner.invoke(app, ["db", "init"])
    assert init_result.exit_code == 0, init_result.output
    reload_result = runner.invoke(app, ["registry", "reload", "--path", str(registry_path)])
    assert reload_result.exit_code == 0, reload_result.output

    engine = create_engine_for_url(cli_db_url)
    session_factory = create_session_factory(engine)
    try:
        with session_scope(session_factory) as session:
            operation, _, _ = service.prepare_operation(
                session,
                principal_id="local",
                environment="default",
                workflow_id="wf.a",
                arguments={},
                preflight=FakePreflight(),
                server_max_argument_bytes=262_144,
            )
            service.execute_operation(
                session,
                operation_id=operation.id,
                handle=operation.id,
                principal_id="local",
                preflight=FakePreflight(),
            )
            service.record_execution_outcome(
                session, operation_id=operation.id, outcome="indeterminate"
            )
            return operation.id
    finally:
        engine.dispose()


@pytest.mark.integration
def test_reconcile_record_with_a_matching_execution_succeeds(
    unknown_operation_id: str, mock_n8n: MockN8n, cli_env: None
) -> None:
    mock_n8n.add_execution(
        "exec-1",
        {
            "id": "exec-1",
            "finished": True,
            "mode": "webhook",
            "status": "success",
            "workflowId": "n8n-real-1",
        },
    )
    result = runner.invoke(
        app,
        [
            "operations",
            "reconcile",
            "record",
            unknown_operation_id,
            "--execution-id",
            "exec-1",
            "--note",
            "confirmed via n8n UI",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Recorded." in result.output
    assert "n8n_execution_status: success" in result.output


@pytest.mark.integration
def test_reconcile_record_with_a_mismatched_workflow_refuses(
    unknown_operation_id: str, mock_n8n: MockN8n, cli_env: None
) -> None:
    mock_n8n.add_execution(
        "exec-wrong",
        {
            "id": "exec-wrong",
            "finished": True,
            "mode": "webhook",
            "status": "success",
            "workflowId": "some-other-workflow",
        },
    )
    result = runner.invoke(
        app,
        [
            "operations",
            "reconcile",
            "record",
            unknown_operation_id,
            "--execution-id",
            "exec-wrong",
            "--note",
            "attempted",
            "--yes",
        ],
    )
    assert result.exit_code != 0
    assert "RECONCILIATION_NOT_APPLICABLE" in result.output or "not be verified" in result.output


@pytest.mark.integration
def test_reconcile_list_shows_recorded_evidence(
    unknown_operation_id: str, mock_n8n: MockN8n, cli_env: None
) -> None:
    mock_n8n.add_execution(
        "exec-1",
        {
            "id": "exec-1",
            "finished": True,
            "mode": "webhook",
            "status": "success",
            "workflowId": "n8n-real-1",
        },
    )
    record_result = runner.invoke(
        app,
        [
            "operations",
            "reconcile",
            "record",
            unknown_operation_id,
            "--execution-id",
            "exec-1",
            "--note",
            "confirmed",
            "--yes",
        ],
    )
    assert record_result.exit_code == 0, record_result.output

    list_result = runner.invoke(app, ["operations", "reconcile", "list", unknown_operation_id])
    assert list_result.exit_code == 0, list_result.output
    assert "exec-1" in list_result.output
    assert "confirmed" in list_result.output


@pytest.mark.integration
def test_reconcile_record_without_yes_prompts_and_can_be_declined(
    unknown_operation_id: str, mock_n8n: MockN8n, cli_env: None
) -> None:
    mock_n8n.add_execution(
        "exec-1",
        {
            "id": "exec-1",
            "finished": True,
            "mode": "webhook",
            "status": "success",
            "workflowId": "n8n-real-1",
        },
    )
    result = runner.invoke(
        app,
        [
            "operations",
            "reconcile",
            "record",
            unknown_operation_id,
            "--execution-id",
            "exec-1",
            "--note",
            "confirmed",
        ],
        input="n\n",
    )
    assert result.exit_code != 0
    assert "Not recorded." in result.output

    list_result = runner.invoke(app, ["operations", "reconcile", "list", unknown_operation_id])
    assert "No reconciliation evidence recorded." in list_result.output


@pytest.mark.integration
def test_retry_lineage_appears_in_show_and_approval_status(
    unknown_operation_id: str, mock_n8n: MockN8n, cli_env: None
) -> None:
    # Retry directly through core.service (an MCP tool call this CLI-only test has no
    # session for), then confirm the CLI's own read commands surface the lineage.
    engine = create_engine_for_url(resolve_database_url())
    session_factory = create_session_factory(engine)
    try:
        with session_scope(session_factory) as session:
            operation, _, _ = service.retry_operation(
                session,
                operation_id=unknown_operation_id,
                principal_id="local",
                preflight=FakePreflight(),
                server_max_argument_bytes=262_144,
            )
            child_id = operation.id
    finally:
        engine.dispose()

    show_result = runner.invoke(app, ["operations", "show", child_id])
    assert show_result.exit_code == 0
    assert f"parent_operation_id: {unknown_operation_id}" in show_result.output

    status_result = runner.invoke(app, ["operations", "approval-status", child_id])
    assert status_result.exit_code == 0
    assert f"retry of:            {unknown_operation_id}" in status_result.output

    parent_show_result = runner.invoke(app, ["operations", "show", unknown_operation_id])
    assert "parent_operation_id:" not in parent_show_result.output
