"""``n8n-operator db`` end to end, through the real Typer CLI (BUILD_PLAN section 12,
phase 1).

Uses :class:`typer.testing.CliRunner` against ``n8n_operator.cli.main.app`` — the same
entry point ``pyproject.toml``'s ``[project.scripts]`` and ``__main__.py`` both invoke —
so these tests exercise the actual command surface an operator runs, not a hand-called
substitute for it. The database URL is set via ``N8N_OPERATOR_DATABASE_URL`` in the
environment for each invocation, matching how an operator would configure it; nothing
here requires the full ``Settings`` (no ``N8N_BASE_URL``/``N8N_API_KEY``), which is
itself the property ``resolve_database_url`` exists to guarantee (ADR-006-adjacent
design note in ``config.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from n8n_operator.cli.main import app

runner = CliRunner()


@pytest.fixture
def cli_db_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'cli.db'}"


@pytest.fixture
def cli_env(cli_db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("N8N_OPERATOR_DATABASE_URL", cli_db_url)
    # These two must NOT be required for any `db` subcommand (see config.py's
    # resolve_database_url docstring) — deliberately left unset here to prove it.
    monkeypatch.delenv("N8N_OPERATOR_N8N_BASE_URL", raising=False)
    monkeypatch.delenv("N8N_OPERATOR_N8N_API_KEY", raising=False)


@pytest.mark.integration
def test_db_status_before_init_is_not_initialized(cli_env: None) -> None:
    result = runner.invoke(app, ["db", "status"])
    assert result.exit_code == 1
    assert "not initialized" in result.stdout
    assert "n8n-operator db init" in result.stdout


@pytest.mark.integration
def test_db_status_on_a_missing_parent_directory_reports_not_initialized_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact regression this test guards: a SQLite URL whose parent directory has
    never been created must not crash `db status` with a raw OperationalError
    traceback — it is precisely what `db status` exists to report cleanly."""
    nested = tmp_path / "does" / "not" / "exist" / "n8n-operator.db"
    monkeypatch.setenv("N8N_OPERATOR_DATABASE_URL", f"sqlite+pysqlite:///{nested}")
    monkeypatch.delenv("N8N_OPERATOR_N8N_BASE_URL", raising=False)
    monkeypatch.delenv("N8N_OPERATOR_N8N_API_KEY", raising=False)

    result = runner.invoke(app, ["db", "status"])
    assert result.exit_code == 1
    assert "Traceback" not in result.stdout
    assert "not initialized" in result.stdout


@pytest.mark.integration
def test_db_init_brings_a_fresh_database_to_head(cli_env: None) -> None:
    result = runner.invoke(app, ["db", "init"])
    assert result.exit_code == 0
    assert "at head" in result.stdout


@pytest.mark.integration
def test_db_init_creates_the_parent_directory_for_a_nested_sqlite_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_path / "a" / "b" / "c" / "n8n-operator.db"
    monkeypatch.setenv("N8N_OPERATOR_DATABASE_URL", f"sqlite+pysqlite:///{nested}")
    monkeypatch.delenv("N8N_OPERATOR_N8N_BASE_URL", raising=False)
    monkeypatch.delenv("N8N_OPERATOR_N8N_API_KEY", raising=False)

    result = runner.invoke(app, ["db", "init"])
    assert result.exit_code == 0
    assert nested.is_file()


@pytest.mark.integration
def test_db_status_after_init_is_up_to_date(cli_env: None) -> None:
    init_result = runner.invoke(app, ["db", "init"])
    assert init_result.exit_code == 0

    result = runner.invoke(app, ["db", "status"])
    assert result.exit_code == 0
    assert "up to date" in result.stdout
    assert "current revision: 0002" in result.stdout
    assert "head revision:    0002" in result.stdout


@pytest.mark.integration
def test_db_migrate_from_empty_reaches_head(cli_env: None) -> None:
    """AC-24, driven through the actual CLI command rather than the Alembic API
    directly — this is the literal command AC-24 names."""
    result = runner.invoke(app, ["db", "migrate"])
    assert result.exit_code == 0
    assert "at head" in result.stdout

    status_result = runner.invoke(app, ["db", "status"])
    assert status_result.exit_code == 0
    assert "up to date" in status_result.stdout


@pytest.mark.integration
def test_db_migrate_is_idempotent(cli_env: None) -> None:
    first = runner.invoke(app, ["db", "migrate"])
    assert first.exit_code == 0
    second = runner.invoke(app, ["db", "migrate"])
    assert second.exit_code == 0


@pytest.mark.integration
def test_db_init_is_idempotent(cli_env: None) -> None:
    first = runner.invoke(app, ["db", "init"])
    assert first.exit_code == 0
    second = runner.invoke(app, ["db", "init"])
    assert second.exit_code == 0
    assert "at head" in second.stdout


@pytest.mark.integration
def test_no_secret_setting_names_appear_in_any_db_command_output(cli_env: None) -> None:
    """A blunt but meaningful guard: db command output should never resemble a
    credential dump, whatever the command's outcome (ADR-006)."""
    for args in (["db", "status"], ["db", "init"], ["db", "migrate"]):
        result = runner.invoke(app, args)
        assert "N8N_OPERATOR_N8N_API_KEY" not in result.stdout
        assert "n8n_api_key" not in result.stdout.lower()


@pytest.mark.integration
def test_db_help_lists_all_three_subcommands() -> None:
    result = runner.invoke(app, ["db", "--help"])
    assert result.exit_code == 0
    assert "init" in result.stdout
    assert "migrate" in result.stdout
    assert "status" in result.stdout


@pytest.mark.integration
def test_python_dash_m_entry_point_wires_the_same_app() -> None:
    """``__main__.py`` must invoke the identical Typer app the installed script uses —
    checked by identity, not merely "produces similar output" (BUILD_PLAN section 4)."""
    from n8n_operator.__main__ import app as main_module_app

    assert main_module_app is app
