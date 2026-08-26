"""``n8n-operator registry`` end to end, through the real Typer CLI (BUILD_PLAN section
12, phase 2).

Mirrors ``tests/integration/test_cli_db.py``'s pattern: :class:`typer.testing.CliRunner`
against the real ``n8n_operator.cli.main.app``, with the registry path and database URL
set via environment variables the way an operator actually would. None of the four
read-only commands (``validate``/``list``/``show``/``hash``) require a database at all;
``reload`` requires a migrated one, exactly like the ``db`` commands.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from n8n_operator.cli.main import app

runner = CliRunner()

VALID_REGISTRY = """apiVersion: n8n-operator/v1
metadata:
  name: cli-test
workflows:
  - id: wf.a
    n8n_workflow_id: n8n-secret-id-999
    title: A workflow
    description: Does a thing.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash}
    risk: low
    side_effects: read_only
    approval: none
    trigger:
      type: webhook
      method: POST
      path: /webhook/a
      auth: header
      secret_ref: env:SOME_SECRET_NAME
    input_schema:
      type: object
      additionalProperties: false
  - id: wf.b
    n8n_workflow_id: n8n-2
    title: B workflow
    description: Does another thing.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash}
    risk: high
    side_effects: irreversible
    approval: required
    trigger:
      type: webhook
      method: POST
      path: /webhook/b
      auth: none
    input_schema:
      type: object
      additionalProperties: false
    enabled: false
""".format(hash="a" * 64)

INVALID_REGISTRY = "apiVersion: n8n-operator/v99\nmetadata:\n  name: bad\nworkflows: []\n"


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    path = tmp_path / "workflows.yaml"
    path.write_text(VALID_REGISTRY)
    return path


@pytest.fixture
def registry_env(registry_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("N8N_OPERATOR_REGISTRY_PATH", str(registry_path))
    monkeypatch.setenv(
        "N8N_OPERATOR_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'cli_registry.db'}"
    )
    monkeypatch.delenv("N8N_OPERATOR_N8N_BASE_URL", raising=False)
    monkeypatch.delenv("N8N_OPERATOR_N8N_API_KEY", raising=False)


# --------------------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_validate_succeeds_on_a_valid_registry(registry_env: None) -> None:
    result = runner.invoke(app, ["registry", "validate"])
    assert result.exit_code == 0
    assert "valid" in result.stdout.lower()
    assert "sha256:" in result.stdout


@pytest.mark.integration
def test_validate_requires_no_n8n_settings(registry_env: None) -> None:
    """The point of resolve_registry_path/resolve_max_argument_bytes: this command
    works with zero N8N_BASE_URL/N8N_API_KEY configuration."""
    result = runner.invoke(app, ["registry", "validate"])
    assert result.exit_code == 0


@pytest.mark.integration
def test_validate_fails_cleanly_on_an_invalid_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad_path = tmp_path / "bad.yaml"
    bad_path.write_text(INVALID_REGISTRY)
    monkeypatch.setenv("N8N_OPERATOR_REGISTRY_PATH", str(bad_path))
    result = runner.invoke(app, ["registry", "validate"])
    assert result.exit_code == 1
    assert "R1" in result.output
    assert "Traceback" not in result.output


@pytest.mark.integration
def test_validate_accepts_an_explicit_path_override(
    tmp_path: Path, registry_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("N8N_OPERATOR_REGISTRY_PATH", str(tmp_path / "does-not-exist.yaml"))
    result = runner.invoke(app, ["registry", "validate", "--path", str(registry_path)])
    assert result.exit_code == 0


# --------------------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_list_shows_both_workflows(registry_env: None) -> None:
    result = runner.invoke(app, ["registry", "list"])
    assert result.exit_code == 0
    assert "wf.a" in result.stdout
    assert "wf.b" in result.stdout


@pytest.mark.integration
def test_list_marks_the_disabled_entry(registry_env: None) -> None:
    result = runner.invoke(app, ["registry", "list"])
    assert "[disabled]" in result.stdout
    lines = result.stdout.splitlines()
    b_line = next(line for line in lines if line.startswith("wf.b"))
    a_line = next(line for line in lines if line.startswith("wf.a"))
    assert "[disabled]" in b_line
    assert "[disabled]" not in a_line


@pytest.mark.integration
def test_list_on_an_empty_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text("apiVersion: n8n-operator/v1\nmetadata:\n  name: empty\nworkflows: []\n")
    monkeypatch.setenv("N8N_OPERATOR_REGISTRY_PATH", str(path))
    result = runner.invoke(app, ["registry", "list"])
    assert result.exit_code == 0
    assert "no workflows" in result.stdout.lower()


# --------------------------------------------------------------------------------------
# show
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_show_prints_full_entry_including_n8n_id_and_secret_ref(registry_env: None) -> None:
    """This is CLI/operator-facing output, not what crosses the MCP boundary — the
    operator already has direct file access to this same data."""
    result = runner.invoke(app, ["registry", "show", "wf.a"])
    assert result.exit_code == 0
    assert "n8n-secret-id-999" in result.stdout
    assert "env:SOME_SECRET_NAME" in result.stdout


@pytest.mark.integration
def test_show_unknown_workflow_id_fails_cleanly(registry_env: None) -> None:
    result = runner.invoke(app, ["registry", "show", "wf.does-not-exist"])
    assert result.exit_code == 1
    assert "No such workflow" in result.output
    assert "Traceback" not in result.output


@pytest.mark.integration
def test_show_can_display_a_disabled_entry(registry_env: None) -> None:
    """Disabled workflows are excluded from MCP discovery but remain inspectable by the
    operator via the CLI (WORKFLOW_REGISTRY.md section 9.3)."""
    result = runner.invoke(app, ["registry", "show", "wf.b"])
    assert result.exit_code == 0
    assert "enabled:          False" in result.stdout


# --------------------------------------------------------------------------------------
# hash
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_hash_prints_a_sha256_prefixed_value(registry_env: None) -> None:
    result = runner.invoke(app, ["registry", "hash"])
    assert result.exit_code == 0
    assert result.stdout.strip().startswith("sha256:")


@pytest.mark.integration
def test_hash_matches_validate(registry_env: None) -> None:
    hash_result = runner.invoke(app, ["registry", "hash"])
    validate_result = runner.invoke(app, ["registry", "validate"])
    assert hash_result.stdout.strip() in validate_result.stdout


@pytest.mark.integration
def test_hash_with_n8n_workflow_id_reports_not_yet_implemented(registry_env: None) -> None:
    result = runner.invoke(app, ["registry", "hash", "--n8n-workflow-id", "abc"])
    assert result.exit_code == 1
    assert "phase 4" in result.stdout.lower() or "phase 4" in result.stderr


# --------------------------------------------------------------------------------------
# reload
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_reload_before_db_init_fails_cleanly(registry_env: None) -> None:
    result = runner.invoke(app, ["registry", "reload"])
    assert result.exit_code == 1
    assert "db init" in result.output
    assert "Traceback" not in result.output


@pytest.mark.integration
def test_reload_after_db_init_succeeds(registry_env: None) -> None:
    init_result = runner.invoke(app, ["db", "init"])
    assert init_result.exit_code == 0

    result = runner.invoke(app, ["registry", "reload"])
    assert result.exit_code == 0
    assert "new snapshot is now active" in result.stdout.lower()


@pytest.mark.integration
def test_reload_twice_reuses_the_snapshot(registry_env: None) -> None:
    runner.invoke(app, ["db", "init"])
    first = runner.invoke(app, ["registry", "reload"])
    second = runner.invoke(app, ["registry", "reload"])
    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "unchanged" in second.stdout.lower()


@pytest.mark.integration
def test_reload_rejects_an_invalid_registry_without_touching_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good_path = tmp_path / "good.yaml"
    good_path.write_text(VALID_REGISTRY)
    monkeypatch.setenv("N8N_OPERATOR_REGISTRY_PATH", str(good_path))
    monkeypatch.setenv("N8N_OPERATOR_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'db.db'}")
    runner.invoke(app, ["db", "init"])
    good_reload = runner.invoke(app, ["registry", "reload"])
    assert good_reload.exit_code == 0

    bad_path = tmp_path / "bad.yaml"
    bad_path.write_text(INVALID_REGISTRY)
    monkeypatch.setenv("N8N_OPERATOR_REGISTRY_PATH", str(bad_path))
    bad_reload = runner.invoke(app, ["registry", "reload"])
    assert bad_reload.exit_code == 1
    assert "R1" in bad_reload.output


@pytest.mark.integration
def test_no_secret_or_n8n_id_appears_in_reload_output(registry_env: None) -> None:
    runner.invoke(app, ["db", "init"])
    result = runner.invoke(app, ["registry", "reload"])
    assert "n8n-secret-id-999" not in result.stdout
    assert "SOME_SECRET_NAME" not in result.stdout
