"""``n8n-operator db migrate-to-postgres`` and ``db status``, through the real Typer
CLI, against a real PostgreSQL destination with a real password — the one place a
password-redaction claim can actually be proven (a SQLite URL never carries one).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from n8n_operator.cli.main import app

runner = CliRunner()
pytestmark = pytest.mark.postgres


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite+pysqlite:///{tmp_path / 'source.db'}"
    monkeypatch.setenv("N8N_OPERATOR_DATABASE_URL", url)
    monkeypatch.delenv("N8N_OPERATOR_N8N_BASE_URL", raising=False)
    monkeypatch.delenv("N8N_OPERATOR_N8N_API_KEY", raising=False)
    return url


@pytest.mark.integration
def test_migrate_to_postgres_via_the_real_cli_succeeds_and_redacts_the_password(
    cli_env: str, postgres_test_db_url: str
) -> None:
    init_result = runner.invoke(app, ["db", "init"])
    assert init_result.exit_code == 0

    result = runner.invoke(app, ["db", "migrate-to-postgres", "--dest", postgres_test_db_url])
    assert result.exit_code == 0, result.output
    assert "Migration verified" in result.output
    assert "testpass" not in result.output
    assert "***" in result.output


@pytest.mark.integration
def test_migrate_to_postgres_dry_run_via_the_real_cli(
    cli_env: str, postgres_test_db_url: str
) -> None:
    init_result = runner.invoke(app, ["db", "init"])
    assert init_result.exit_code == 0

    result = runner.invoke(
        app, ["db", "migrate-to-postgres", "--dest", postgres_test_db_url, "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output


@pytest.mark.integration
def test_migrate_to_postgres_json_output(cli_env: str, postgres_test_db_url: str) -> None:
    import json

    init_result = runner.invoke(app, ["db", "init"])
    assert init_result.exit_code == 0

    result = runner.invoke(
        app, ["db", "migrate-to-postgres", "--dest", postgres_test_db_url, "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["audit_chain_ok"] is True


@pytest.mark.integration
def test_migrate_to_postgres_refuses_a_non_empty_destination_via_the_cli(
    cli_env: str, postgres_test_db_url: str
) -> None:
    init_result = runner.invoke(app, ["db", "init"])
    assert init_result.exit_code == 0

    first = runner.invoke(app, ["db", "migrate-to-postgres", "--dest", postgres_test_db_url])
    assert first.exit_code == 0, first.output

    second = runner.invoke(app, ["db", "migrate-to-postgres", "--dest", postgres_test_db_url])
    assert second.exit_code == 1
    assert "Migration refused" in second.output
