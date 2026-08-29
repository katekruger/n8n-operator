"""``n8n-operator environment`` end to end, through the real Typer CLI (stage 04)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from n8n_operator.cli.main import app

runner = CliRunner()

_SECRET_ENV_VALUE = "https://staging.n8n.internal.example.com/should-never-print"
_SECRET_API_KEY_VALUE = "sk-should-never-print-either"


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("N8N_OPERATOR_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'cli.db'}")
    monkeypatch.delenv("N8N_OPERATOR_N8N_BASE_URL", raising=False)
    monkeypatch.delenv("N8N_OPERATOR_N8N_API_KEY", raising=False)
    monkeypatch.setenv("N8N_OPERATOR_TEST_STAGING_URL", _SECRET_ENV_VALUE)
    monkeypatch.setenv("N8N_OPERATOR_TEST_STAGING_KEY", _SECRET_API_KEY_VALUE)


def _init(cli_env: None) -> None:
    result = runner.invoke(app, ["db", "init"])
    assert result.exit_code == 0, result.output


_REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: environment-cli-test
workflows:
  - id: crm.sync_contact
    n8n_workflow_id: n8n-1
    title: Sync contact
    description: Read-only sync.
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
      auth: none
    input_schema:
      type: object
      properties: {{}}
      additionalProperties: false
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300
""".format(hash="a" * 64)

_OVERLAY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: staging-overlay
overlays:
  - workflow_id: crm.sync_contact
    approval_override: required
    limits_override:
      execution_ttl_seconds: 100
"""


def _load_registry(cli_env: None, tmp_path: Path) -> None:
    registry_path = tmp_path / "workflows.yaml"
    registry_path.write_text(_REGISTRY_YAML)
    result = runner.invoke(app, ["registry", "reload", "--path", str(registry_path)])
    assert result.exit_code == 0, result.output


def _create_org(cli_env: None) -> str:
    result = runner.invoke(app, ["identity", "create-org", "--name", "Acme"])
    assert result.exit_code == 0, result.output
    match = re.search(r"Organization created: (\S+)", result.output)
    assert match is not None
    return match.group(1)


def _create_environment(cli_env: None, org_id: str, *, name: str = "staging") -> str:
    result = runner.invoke(
        app,
        [
            "environment",
            "create",
            "--org",
            org_id,
            "--name",
            name,
            "--n8n-base-url-ref",
            "env:N8N_OPERATOR_TEST_STAGING_URL",
            "--n8n-api-key-ref",
            "env:N8N_OPERATOR_TEST_STAGING_KEY",
        ],
    )
    assert result.exit_code == 0, result.output
    match = re.search(r"Environment created: (\S+)", result.output)
    assert match is not None
    return match.group(1)


@pytest.mark.integration
def test_create_list_and_show_safe(cli_env: None) -> None:
    _init(cli_env)
    org_id = _create_org(cli_env)
    env_id = _create_environment(cli_env, org_id)

    list_result = runner.invoke(app, ["environment", "list"])
    assert list_result.exit_code == 0, list_result.output
    assert env_id in list_result.output
    assert "staging" in list_result.output

    show_result = runner.invoke(app, ["environment", "show-safe", env_id])
    assert show_result.exit_code == 0, show_result.output
    assert env_id in show_result.output
    assert "env:N8N_OPERATOR_TEST_STAGING_URL" in show_result.output
    assert "env:N8N_OPERATOR_TEST_STAGING_KEY" in show_result.output


@pytest.mark.integration
def test_archive_marks_archived_and_is_reflected_in_list(cli_env: None) -> None:
    _init(cli_env)
    org_id = _create_org(cli_env)
    env_id = _create_environment(cli_env, org_id)

    archive_result = runner.invoke(app, ["environment", "archive", env_id])
    assert archive_result.exit_code == 0, archive_result.output

    list_result = runner.invoke(app, ["environment", "list"])
    assert "[archived]" in list_result.output


@pytest.mark.integration
def test_archive_unknown_environment_fails_loudly(cli_env: None) -> None:
    _init(cli_env)
    result = runner.invoke(app, ["environment", "archive", "does-not-exist"])
    assert result.exit_code != 0


@pytest.mark.integration
def test_validate_overlay_dry_run_does_not_persist(cli_env: None, tmp_path: Path) -> None:
    _init(cli_env)
    _load_registry(cli_env, tmp_path)
    org_id = _create_org(cli_env)
    env_id = _create_environment(cli_env, org_id)

    overlay_path = tmp_path / "overlay.yaml"
    overlay_path.write_text(_OVERLAY_YAML)

    validate_result = runner.invoke(
        app, ["environment", "validate-overlay", env_id, "--path", str(overlay_path)]
    )
    assert validate_result.exit_code == 0, validate_result.output
    assert "valid" in validate_result.output.lower()

    diff_result = runner.invoke(
        app,
        ["environment", "registry-diff", env_id, "--path", str(tmp_path / "workflows.yaml")],
    )
    assert diff_result.exit_code == 0, diff_result.output
    assert "no differences" in diff_result.output.lower()


@pytest.mark.integration
def test_reload_overlay_persists_and_shows_in_registry_diff(cli_env: None, tmp_path: Path) -> None:
    _init(cli_env)
    _load_registry(cli_env, tmp_path)
    org_id = _create_org(cli_env)
    env_id = _create_environment(cli_env, org_id)

    overlay_path = tmp_path / "overlay.yaml"
    overlay_path.write_text(_OVERLAY_YAML)

    reload_result = runner.invoke(
        app, ["environment", "reload-overlay", env_id, "--path", str(overlay_path)]
    )
    assert reload_result.exit_code == 0, reload_result.output
    assert "reloaded" in reload_result.output.lower()

    diff_result = runner.invoke(
        app,
        ["environment", "registry-diff", env_id, "--path", str(tmp_path / "workflows.yaml")],
    )
    assert diff_result.exit_code == 0, diff_result.output
    assert "crm.sync_contact" in diff_result.output
    assert "approval: none -> required" in diff_result.output


@pytest.mark.integration
def test_reload_overlay_naming_an_unknown_workflow_fails_loudly(
    cli_env: None, tmp_path: Path
) -> None:
    _init(cli_env)
    _load_registry(cli_env, tmp_path)
    org_id = _create_org(cli_env)
    env_id = _create_environment(cli_env, org_id)

    bad_overlay_path = tmp_path / "bad-overlay.yaml"
    bad_overlay_path.write_text(
        "apiVersion: n8n-operator/v1\n"
        "metadata:\n"
        "  name: bad\n"
        "overlays:\n"
        "  - workflow_id: does.not.exist\n"
        "    approval_override: required\n"
    )

    result = runner.invoke(
        app, ["environment", "reload-overlay", env_id, "--path", str(bad_overlay_path)]
    )
    assert result.exit_code != 0
    assert "R13" in result.output or "invalid" in result.output.lower()


@pytest.mark.integration
def test_no_command_ever_prints_the_resolved_secret_value(cli_env: None, tmp_path: Path) -> None:
    """The no-secrets artifact inspection the stage 04 completion gate names: every
    command this test suite exercises against a real, resolvable secret reference,
    checked for the literal resolved value never appearing in stdout."""
    _init(cli_env)
    _load_registry(cli_env, tmp_path)
    org_id = _create_org(cli_env)
    env_id = _create_environment(cli_env, org_id)

    overlay_path = tmp_path / "overlay.yaml"
    overlay_path.write_text(_OVERLAY_YAML)

    outputs = [
        runner.invoke(app, ["environment", "list"]).output,
        runner.invoke(app, ["environment", "show-safe", env_id]).output,
        runner.invoke(
            app, ["environment", "validate-overlay", env_id, "--path", str(overlay_path)]
        ).output,
        runner.invoke(
            app, ["environment", "reload-overlay", env_id, "--path", str(overlay_path)]
        ).output,
        runner.invoke(
            app,
            ["environment", "registry-diff", env_id, "--path", str(tmp_path / "workflows.yaml")],
        ).output,
    ]
    combined = "\n".join(outputs)
    assert _SECRET_ENV_VALUE not in combined
    assert _SECRET_API_KEY_VALUE not in combined
