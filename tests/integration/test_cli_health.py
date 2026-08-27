"""``n8n-operator health`` end to end, through the real Typer CLI (BUILD_PLAN section
12, phase 8) — ``get_instance_health``, from the command line.

No real n8n instance in the loop (this test suite's mock n8n transport is injected at
the ``N8nClient`` constructor, a seam the CLI itself does not expose — by design,
``cli/commands/health.py`` is its own small composition root, not a place to thread a
test-only parameter through): instead this points at a local port nothing is listening
on, so "unreachable" resolves immediately without a real network dependency.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from n8n_operator.cli.main import app

runner = CliRunner()


@pytest.fixture
def unreachable_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("N8N_OPERATOR_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'cli.db'}")
    # Port 1 on loopback: privileged and essentially never bound by anything in a test
    # sandbox, so the connection is refused immediately rather than timing out.
    monkeypatch.setenv("N8N_OPERATOR_N8N_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("N8N_OPERATOR_N8N_API_KEY", "test-key-not-a-real-secret")
    monkeypatch.setenv("N8N_OPERATOR_REQUEST_TIMEOUT_SECONDS", "2")


@pytest.mark.integration
def test_health_reports_unreachable_and_exits_1(unreachable_env: None) -> None:
    result = runner.invoke(app, ["health"])
    assert result.exit_code == 1
    assert "reachable:    False" in result.output
    assert "reason:" in result.output


@pytest.mark.integration
def test_health_json_shape_when_unreachable(unreachable_env: None) -> None:
    result = runner.invoke(app, ["health", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["reachable"] is False
    assert payload["n8n_version"] is None
    assert payload["reason"]
    assert "checked_at" in payload


@pytest.mark.integration
def test_health_output_never_contains_the_configured_url_or_api_key(
    unreachable_env: None,
) -> None:
    """Boundary B5: get_instance_health is a discovery tool, not a way to learn where
    the instance lives — nothing printed here should ever carry the base URL or the
    API key, reachable or not."""
    result = runner.invoke(app, ["health"])
    assert "127.0.0.1:1" not in result.output
    assert "test-key-not-a-real-secret" not in result.output

    json_result = runner.invoke(app, ["health", "--json"])
    assert "127.0.0.1:1" not in json_result.output
    assert "test-key-not-a-real-secret" not in json_result.output


@pytest.mark.integration
def test_health_missing_n8n_config_fails_with_a_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("N8N_OPERATOR_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'cli.db'}")
    monkeypatch.delenv("N8N_OPERATOR_N8N_BASE_URL", raising=False)
    monkeypatch.delenv("N8N_OPERATOR_N8N_API_KEY", raising=False)
    result = runner.invoke(app, ["health"])
    assert result.exit_code != 0
