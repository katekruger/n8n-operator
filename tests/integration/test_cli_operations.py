"""``n8n-operator operations`` end to end, through the real Typer CLI (BUILD_PLAN
section 12, phases 6 and 8; ADR-010).

Demonstrates the CLI-only stdio flow ADR-010 makes canonical: init the database, load
the registry, prepare an operation (standing in for the MCP `prepare_operation` tool,
which needs a running MCP session this test doesn't stand up), and approve or reject it
entirely from the command line — no browser, no listener, ever reachable. Phase 8 adds
`list`/`show`/`cancel` — history, detail, and withdrawal — including their `--json`
output shapes and determinism.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from n8n_operator.cli.main import app
from n8n_operator.core import service
from n8n_operator.core.models import PreflightResult
from n8n_operator.storage.repository import PrincipalRepository
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)

runner = CliRunner()

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: phase6-cli-test
workflows:
  - id: wf.approval
    n8n_workflow_id: n8n-1
    title: Needs approval
    description: Writes to an external system.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_a}
    risk: high
    side_effects: irreversible
    approval: required
    trigger:
      type: webhook
      method: POST
      path: /webhook/a
      auth: none
    input_schema:
      type: object
      properties:
        email: {{type: string}}
      required: [email]
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
def cli_env(cli_db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("N8N_OPERATOR_DATABASE_URL", cli_db_url)
    # `operations` and `serve approval` must never require these (see cli/commands/
    # operations.py and serve.py's own docstrings) — deliberately left unset to prove it.
    monkeypatch.delenv("N8N_OPERATOR_N8N_BASE_URL", raising=False)
    monkeypatch.delenv("N8N_OPERATOR_N8N_API_KEY", raising=False)


@pytest.fixture
def prepared(cli_env: None, cli_db_url: str, registry_path: Path) -> str:
    """Init the database, load the registry, and prepare one PENDING_APPROVAL
    operation — through ``db init``/``registry reload`` (the real CLI) for the first
    two, and directly through ``core.service`` for the third, since preparing an
    operation is normally an MCP tool call this test suite has no MCP session for.
    Returns the operation ID.
    """
    init_result = runner.invoke(app, ["db", "init"])
    assert init_result.exit_code == 0, init_result.output
    reload_result = runner.invoke(app, ["registry", "reload", "--path", str(registry_path)])
    assert reload_result.exit_code == 0, reload_result.output

    engine = create_engine_for_url(cli_db_url)
    session_factory = create_session_factory(engine)
    try:
        with session_scope(session_factory) as session:
            PrincipalRepository(session).create(id="local", kind="local", display_name="local")
        with session_scope(session_factory) as session:
            operation, _replay, _token = service.prepare_operation(
                session,
                principal_id="local",
                environment="default",
                workflow_id="wf.approval",
                arguments={"email": "a@b.com"},
                preflight=FakePreflight(),
                server_max_argument_bytes=262_144,
            )
            return operation.id
    finally:
        engine.dispose()


@pytest.mark.integration
def test_approval_status_renders_the_decision_surface(prepared: str, cli_env: None) -> None:
    result = runner.invoke(app, ["operations", "approval-status", prepared])
    assert result.exit_code == 0
    assert "title:               Needs approval" in result.output
    assert "risk:                high" in result.output
    assert "side_effects:        irreversible" in result.output
    assert '"email": "a@b.com"' in result.output
    assert "approval:            pending" in result.output


@pytest.mark.integration
def test_approve_with_yes_flag_skips_confirmation(prepared: str, cli_env: None) -> None:
    result = runner.invoke(app, ["operations", "approve", prepared, "--yes"])
    assert result.exit_code == 0
    assert "Approved. state=APPROVED" in result.output


@pytest.mark.integration
def test_approve_interactive_confirmation_accepted(prepared: str, cli_env: None) -> None:
    result = runner.invoke(app, ["operations", "approve", prepared], input="y\n")
    assert result.exit_code == 0
    assert "Approved. state=APPROVED" in result.output


@pytest.mark.integration
def test_approve_interactive_confirmation_declined_does_not_approve(
    prepared: str, cli_env: None
) -> None:
    result = runner.invoke(app, ["operations", "approve", prepared], input="n\n")
    assert result.exit_code == 1
    assert "Not approved." in result.output

    status = runner.invoke(app, ["operations", "approval-status", prepared])
    assert "approval:            pending" in status.output


@pytest.mark.integration
def test_reject_with_yes_flag(prepared: str, cli_env: None) -> None:
    result = runner.invoke(app, ["operations", "reject", prepared, "--yes"])
    assert result.exit_code == 0
    assert "Rejected. state=REJECTED" in result.output


@pytest.mark.integration
def test_approving_an_already_approved_operation_gives_a_clean_error(
    prepared: str, cli_env: None
) -> None:
    first = runner.invoke(app, ["operations", "approve", prepared, "--yes"])
    assert first.exit_code == 0

    second = runner.invoke(app, ["operations", "approve", prepared, "--yes"])
    assert second.exit_code == 1
    assert "not PENDING_APPROVAL; nothing to approve" in second.output


@pytest.mark.integration
def test_approve_unknown_operation_id(cli_env: None, cli_db_url: str) -> None:
    init_result = runner.invoke(app, ["db", "init"])
    assert init_result.exit_code == 0
    result = runner.invoke(app, ["operations", "approve", "op_does_not_exist", "--yes"])
    assert result.exit_code == 1
    assert "No such operation: op_does_not_exist" in result.output


@pytest.mark.integration
def test_expire_reports_a_count_and_is_idempotent(prepared: str, cli_env: None) -> None:
    first = runner.invoke(app, ["operations", "expire"])
    assert first.exit_code == 0
    assert "Expired 0 operation(s)." in first.output  # nothing overdue yet

    second = runner.invoke(app, ["operations", "expire"])
    assert second.exit_code == 0
    assert "Expired 0 operation(s)." in second.output


@pytest.mark.integration
def test_list_shows_a_prepared_operation(prepared: str, cli_env: None) -> None:
    result = runner.invoke(app, ["operations", "list"])
    assert result.exit_code == 0
    assert prepared[:20] in result.output
    assert "wf.approval" in result.output
    assert "PENDING_APPROVAL" in result.output


@pytest.mark.integration
def test_list_json_output_shape(prepared: str, cli_env: None) -> None:
    result = runner.invoke(app, ["operations", "list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert len(payload) == 1
    assert payload[0]["operation_id"] == prepared
    assert payload[0]["workflow_id"] == "wf.approval"
    assert payload[0]["state"] == "PENDING_APPROVAL"
    assert set(payload[0]) == {"operation_id", "workflow_id", "state", "created_at", "updated_at"}


@pytest.mark.integration
def test_list_json_output_is_deterministic_across_repeated_calls(
    prepared: str, cli_env: None
) -> None:
    first = runner.invoke(app, ["operations", "list", "--json"])
    second = runner.invoke(app, ["operations", "list", "--json"])
    assert first.exit_code == 0
    assert second.exit_code == 0
    assert first.output == second.output


@pytest.mark.integration
def test_list_filters_by_state(prepared: str, cli_env: None) -> None:
    approved_state = runner.invoke(app, ["operations", "list", "--state", "APPROVED", "--json"])
    assert approved_state.exit_code == 0
    assert json.loads(approved_state.output) == []

    pending_state = runner.invoke(
        app, ["operations", "list", "--state", "PENDING_APPROVAL", "--json"]
    )
    assert pending_state.exit_code == 0
    assert len(json.loads(pending_state.output)) == 1


@pytest.mark.integration
def test_list_with_no_operations_is_empty(cli_env: None, cli_db_url: str) -> None:
    init_result = runner.invoke(app, ["db", "init"])
    assert init_result.exit_code == 0
    result = runner.invoke(app, ["operations", "list"])
    assert result.exit_code == 0
    assert "No operations." in result.output
    json_result = runner.invoke(app, ["operations", "list", "--json"])
    assert json.loads(json_result.output) == []


@pytest.mark.integration
def test_show_renders_state_and_redacted_arguments(prepared: str, cli_env: None) -> None:
    result = runner.invoke(app, ["operations", "show", prepared])
    assert result.exit_code == 0
    assert f"operation_id:        {prepared}" in result.output
    assert "workflow_id:         wf.approval" in result.output
    assert "state:               PENDING_APPROVAL" in result.output
    assert '"email": "a@b.com"' in result.output


@pytest.mark.integration
def test_show_json_shape(prepared: str, cli_env: None) -> None:
    result = runner.invoke(app, ["operations", "show", prepared, "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["operation_id"] == prepared
    assert payload["state"] == "PENDING_APPROVAL"
    assert payload["arguments"] == {"email": "a@b.com"}
    assert payload["handle_used"] is False


@pytest.mark.integration
def test_show_unknown_operation_id(cli_env: None, cli_db_url: str) -> None:
    init_result = runner.invoke(app, ["db", "init"])
    assert init_result.exit_code == 0
    result = runner.invoke(app, ["operations", "show", "op_does_not_exist"])
    assert result.exit_code == 1
    assert "No such operation: op_does_not_exist" in result.output


@pytest.mark.integration
def test_cancel_with_yes_flag(prepared: str, cli_env: None) -> None:
    result = runner.invoke(app, ["operations", "cancel", prepared, "--yes"])
    assert result.exit_code == 0
    assert "Canceled. state=CANCELED" in result.output

    status = runner.invoke(app, ["operations", "show", prepared, "--json"])
    assert json.loads(status.output)["state"] == "CANCELED"


@pytest.mark.integration
def test_cancel_interactive_confirmation_declined_does_not_cancel(
    prepared: str, cli_env: None
) -> None:
    result = runner.invoke(app, ["operations", "cancel", prepared], input="n\n")
    assert result.exit_code == 1
    assert "Not canceled." in result.output

    status = runner.invoke(app, ["operations", "show", prepared, "--json"])
    assert json.loads(status.output)["state"] == "PENDING_APPROVAL"


@pytest.mark.integration
def test_cancel_an_already_terminal_operation_gives_a_clean_error(
    prepared: str, cli_env: None
) -> None:
    first = runner.invoke(app, ["operations", "cancel", prepared, "--yes"])
    assert first.exit_code == 0

    second = runner.invoke(app, ["operations", "cancel", prepared, "--yes"])
    assert second.exit_code == 1
    assert "only PENDING_APPROVAL or APPROVED operations can be canceled" in second.output


@pytest.mark.integration
def test_cancel_unknown_operation_id(cli_env: None, cli_db_url: str) -> None:
    init_result = runner.invoke(app, ["db", "init"])
    assert init_result.exit_code == 0
    result = runner.invoke(app, ["operations", "cancel", "op_does_not_exist", "--yes"])
    assert result.exit_code == 1
    assert "No such operation: op_does_not_exist" in result.output


@pytest.mark.integration
def test_full_cli_only_stdio_flow_prepare_to_approve_to_execute(
    prepared: str, cli_env: None, cli_db_url: str
) -> None:
    """The flow ADR-010 makes canonical, entirely from the command line: an operation
    is prepared (standing in for the MCP tool), approved via the CLI with no browser
    or listener ever involved, and then executable."""
    approve_result = runner.invoke(app, ["operations", "approve", prepared, "--yes"])
    assert approve_result.exit_code == 0

    engine = create_engine_for_url(cli_db_url)
    session_factory = create_session_factory(engine)
    try:
        with session_scope(session_factory) as session:
            operation = service.execute_operation(
                session,
                operation_id=prepared,
                handle=prepared,
                principal_id="local",
                preflight=FakePreflight(),
            )
        assert operation.state == "EXECUTING"
    finally:
        engine.dispose()
