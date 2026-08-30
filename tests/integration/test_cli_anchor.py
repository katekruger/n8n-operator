"""``n8n-operator anchor init-key | publish | verify | status`` end to end, through
the real Typer CLI (stage 09, ADR-012 section 2).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from n8n_operator.cli.main import app

runner = CliRunner()

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: anchor-cli-test
workflows: []
"""


@pytest.fixture
def cli_db_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'cli.db'}"


@pytest.fixture
def cli_env(cli_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("N8N_OPERATOR_DATABASE_URL", cli_db_url)
    monkeypatch.setenv("N8N_OPERATOR_ANCHOR_SIGNING_KEY_PATH", str(tmp_path / "anchor_key"))
    monkeypatch.delenv("N8N_OPERATOR_N8N_BASE_URL", raising=False)
    monkeypatch.delenv("N8N_OPERATOR_N8N_API_KEY", raising=False)


@pytest.fixture
def db_ready(cli_env: None) -> None:
    result = runner.invoke(app, ["db", "init"])
    assert result.exit_code == 0, result.output


@pytest.mark.integration
def test_init_key_writes_a_key_with_0600_permissions(db_ready: None, tmp_path: Path) -> None:
    result = runner.invoke(app, ["anchor", "init-key"])
    assert result.exit_code == 0, result.output
    assert "public_key:" in result.stdout
    key_path = tmp_path / "anchor_key"
    assert key_path.exists()
    mode = key_path.stat().st_mode & 0o777
    assert mode == 0o600


@pytest.mark.integration
def test_init_key_refuses_to_overwrite(db_ready: None) -> None:
    first = runner.invoke(app, ["anchor", "init-key"])
    assert first.exit_code == 0
    second = runner.invoke(app, ["anchor", "init-key"])
    assert second.exit_code == 1
    assert "already exists" in second.output


@pytest.mark.integration
def test_publish_without_a_key_fails_cleanly(db_ready: None) -> None:
    result = runner.invoke(app, ["anchor", "publish"])
    assert result.exit_code == 1
    assert "init-key" in result.output
    assert "Traceback" not in result.output


@pytest.mark.integration
def test_publish_on_an_empty_chain_is_a_no_op(db_ready: None) -> None:
    runner.invoke(app, ["anchor", "init-key"])
    result = runner.invoke(app, ["anchor", "publish"])
    assert result.exit_code == 0, result.output
    assert "Nothing to anchor" in result.output


@pytest.mark.integration
def test_publish_then_status_then_verify(db_ready: None, tmp_path: Path) -> None:
    runner.invoke(app, ["anchor", "init-key"])
    registry_path = tmp_path / "workflows.yaml"
    registry_path.write_text(REGISTRY_YAML)
    reload_result = runner.invoke(app, ["registry", "reload", "--path", str(registry_path)])
    assert reload_result.exit_code == 0, reload_result.output

    publish_result = runner.invoke(app, ["anchor", "publish"])
    assert publish_result.exit_code == 0, publish_result.output
    assert "Anchored through seq=" in publish_result.output

    status_result = runner.invoke(app, ["anchor", "status"])
    assert status_result.exit_code == 0, status_result.output
    assert "local_file" in status_result.output

    status_json = runner.invoke(app, ["anchor", "status", "--json"])
    assert status_json.exit_code == 0
    body = json.loads(status_json.stdout)
    assert body[0]["implementation"] == "local_file"
    assert body[0]["last_publish_failed"] is False

    verify_result = runner.invoke(app, ["anchor", "verify"])
    assert verify_result.exit_code == 0, verify_result.output
    assert "OK" in verify_result.output


@pytest.mark.integration
def test_publish_twice_is_idempotent(db_ready: None, tmp_path: Path) -> None:
    runner.invoke(app, ["anchor", "init-key"])
    registry_path = tmp_path / "workflows.yaml"
    registry_path.write_text(REGISTRY_YAML)
    runner.invoke(app, ["registry", "reload", "--path", str(registry_path)])

    first = runner.invoke(app, ["anchor", "publish"])
    second = runner.invoke(app, ["anchor", "publish"])
    assert first.exit_code == 0
    assert second.exit_code == 0
    assert first.stdout == second.stdout


@pytest.mark.integration
def test_verify_with_no_anchors_published_fails_cleanly(db_ready: None) -> None:
    runner.invoke(app, ["anchor", "init-key"])
    result = runner.invoke(app, ["anchor", "verify"])
    assert result.exit_code == 1
    assert "No anchor" in result.output


@pytest.mark.integration
def test_verify_against_an_independent_database_copy(
    db_ready: None, tmp_path: Path, cli_db_url: str
) -> None:
    runner.invoke(app, ["anchor", "init-key"])
    registry_path = tmp_path / "workflows.yaml"
    registry_path.write_text(REGISTRY_YAML)
    runner.invoke(app, ["registry", "reload", "--path", str(registry_path)])
    runner.invoke(app, ["anchor", "publish"])

    import shutil

    db_file = cli_db_url.removeprefix("sqlite+pysqlite:///")
    copy_path = tmp_path / "copy.db"
    shutil.copyfile(db_file, copy_path)

    result = runner.invoke(
        app,
        ["anchor", "verify", "--database-url", f"sqlite+pysqlite:///{copy_path}"],
    )
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


@pytest.mark.integration
def test_status_with_nothing_published_reports_cleanly(db_ready: None) -> None:
    result = runner.invoke(app, ["anchor", "status"])
    assert result.exit_code == 0
    assert "no anchors published" in result.output.lower()


@pytest.mark.integration
def test_status_succeeds_under_v2_with_the_dev_principal(
    cli_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage 03: the CLI's own identity in v2 mode is always the fixed dev/service
    principal, which ``ensure_dev_principal`` idempotently grants a real ``admin``
    membership — exactly ``test_audit_verify_succeeds_under_v2_with_the_dev_principal``'s
    own established precedent, applied here. "Insufficient role" itself is proven at
    the ``core.service`` level instead
    (``tests/integration/test_audit_anchor_service.py::
    test_publish_anchor_v2_admin_gated_viewer_denied``), since the CLI's own identity
    can never be anything other than admin by this stage's design."""
    monkeypatch.setenv("N8N_OPERATOR_ENABLE_V2", "true")
    init_result = runner.invoke(app, ["db", "init"])
    assert init_result.exit_code == 0, init_result.output

    result = runner.invoke(app, ["anchor", "status"])
    assert result.exit_code == 0, result.output
