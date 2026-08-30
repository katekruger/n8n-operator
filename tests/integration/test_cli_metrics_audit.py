"""``n8n-operator metrics show``, ``audit list``, and ``notifications check-alerts``
end to end, through the real Typer CLI (stage 08, BUILD_PLAN section 8).
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
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)

runner = CliRunner()

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: metrics-audit-cli-test
workflows:
  - id: crm.sync_contact
    n8n_workflow_id: n8n-1
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
    monkeypatch.delenv("N8N_OPERATOR_N8N_BASE_URL", raising=False)
    monkeypatch.delenv("N8N_OPERATOR_N8N_API_KEY", raising=False)


@pytest.fixture
def prepared(cli_env: None, cli_db_url: str, registry_path: Path) -> str:
    init_result = runner.invoke(app, ["db", "init"])
    assert init_result.exit_code == 0, init_result.output
    reload_result = runner.invoke(app, ["registry", "reload", "--path", str(registry_path)])
    assert reload_result.exit_code == 0, reload_result.output

    engine = create_engine_for_url(cli_db_url)
    session_factory = create_session_factory(engine)
    try:
        with session_scope(session_factory) as session:
            operation, _replay, _token = service.prepare_operation(
                session,
                principal_id="local",
                environment="default",
                workflow_id="crm.sync_contact",
                arguments={},
                preflight=FakePreflight(),
                server_max_argument_bytes=262_144,
            )
            return operation.id
    finally:
        engine.dispose()


@pytest.mark.integration
def test_metrics_show_human_output(prepared: str) -> None:
    result = runner.invoke(app, ["metrics", "show"])
    assert result.exit_code == 0, result.output
    assert "window:" in result.stdout
    assert "total:" in result.stdout


@pytest.mark.integration
def test_metrics_show_json_output(prepared: str) -> None:
    result = runner.invoke(app, ["metrics", "show", "--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["window"] == "24h"
    assert body["totals"]["count"] == 1


@pytest.mark.integration
def test_metrics_show_rejects_a_bad_window(prepared: str) -> None:
    result = runner.invoke(app, ["metrics", "show", "--window", "90d"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output


@pytest.mark.integration
def test_audit_list_human_output(prepared: str) -> None:
    result = runner.invoke(app, ["audit", "list"])
    assert result.exit_code == 0, result.output
    assert "seq=" in result.stdout


@pytest.mark.integration
def test_audit_list_json_output(prepared: str) -> None:
    result = runner.invoke(app, ["audit", "list", "--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["events"]


@pytest.mark.integration
def test_audit_list_workflow_id_filter(prepared: str) -> None:
    result = runner.invoke(app, ["audit", "list", "--workflow-id", "crm.sync_contact", "--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["events"]


@pytest.mark.integration
def test_audit_list_limit_paginates(prepared: str) -> None:
    result = runner.invoke(app, ["audit", "list", "--limit", "1", "--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert len(body["events"]) == 1


@pytest.mark.integration
def test_notifications_check_alerts_runs_cleanly_with_nothing_to_alert(
    prepared: str,
) -> None:
    result = runner.invoke(app, ["notifications", "check-alerts"])
    assert result.exit_code == 0, result.output
    assert "Delivered 0 alert(s)." in result.stdout


@pytest.mark.integration
def test_notifications_check_alerts_fires_for_a_stuck_executing_operation(
    cli_env: None, cli_db_url: str, registry_path: Path
) -> None:
    init_result = runner.invoke(app, ["db", "init"])
    assert init_result.exit_code == 0
    reload_result = runner.invoke(app, ["registry", "reload", "--path", str(registry_path)])
    assert reload_result.exit_code == 0

    engine = create_engine_for_url(cli_db_url)
    session_factory = create_session_factory(engine)
    try:
        with session_scope(session_factory) as session:
            from n8n_operator.storage.repository import OperationRepository

            OperationRepository(session).create(
                id="op_stuck",
                principal_id="local",
                environment="default",
                snapshot_id=service.get_active_snapshot(session).id,  # type: ignore[union-attr]
                workflow_id="crm.sync_contact",
                definition_hash="sha256:" + "a" * 64,
                state="EXECUTING",
                arguments={},
                argument_fingerprint="fp-stuck",
                argument_bytes=2,
            )
    finally:
        engine.dispose()

    result = runner.invoke(
        app, ["notifications", "check-alerts", "--executing-stuck-threshold-seconds", "0"]
    )
    assert result.exit_code == 0, result.output
    assert "Delivered 1 alert(s)." in result.stdout
