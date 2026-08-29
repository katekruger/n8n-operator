"""``n8n-operator audit verify | export`` end to end, through the real Typer CLI
(BUILD_PLAN section 12, phase 8; section 9.4).

Covers AC-22 (`audit verify` passes on a clean database and identifies the exact
sequence number after a single row is mutated) and AC-25 (`audit export` produces a
complete, chain-verifiable record of every operation) — plus what the export must never
include (a credential, a webhook secret, raw unredacted arguments/results, an approval
token) and that the export can be independently re-verified in a genuinely separate
process, not just re-parsed in this one.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update
from typer.testing import CliRunner

from n8n_operator.cli.main import app
from n8n_operator.core import service
from n8n_operator.core.models import PreflightResult
from n8n_operator.storage.models import AuditLogEntry
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)

runner = CliRunner()

API_KEY_LOOKALIKE = "sk-live-should-never-appear-in-an-export-abc123"

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: phase8-audit-cli-test
workflows:
  - id: wf.secret
    n8n_workflow_id: n8n-1
    title: Handles a secret field
    description: Writes to an external system.
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
      properties:
        email: {{type: string}}
        api_key: {{type: string}}
      required: [email]
      additionalProperties: false
    output:
      redact: ["$.api_key"]
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
    monkeypatch.delenv("N8N_OPERATOR_N8N_BASE_URL", raising=False)
    monkeypatch.delenv("N8N_OPERATOR_N8N_API_KEY", raising=False)


@pytest.fixture
def prepared(cli_env: None, cli_db_url: str, registry_path: Path) -> str:
    """Init the database, load the registry, and prepare, approve, execute, and
    complete one operation whose ``api_key`` argument is what the export must redact —
    the same "real CLI for schema, core.service directly for what would otherwise need
    an MCP session" pattern ``test_cli_operations.py`` establishes. ``db init`` seeds
    the v1 default ``"local"`` principal itself — nothing else in the shipped product
    does."""
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
                workflow_id="wf.secret",
                arguments={"email": "a@b.com", "api_key": API_KEY_LOOKALIKE},
                preflight=FakePreflight(),
                server_max_argument_bytes=262_144,
            )
            op_id = operation.id
        with session_scope(session_factory) as session:
            service.approve_operation(session, operation_id=op_id, decided_by="local")
            service.execute_operation(
                session,
                operation_id=op_id,
                handle=op_id,
                principal_id="local",
                preflight=FakePreflight(),
            )
        with session_scope(session_factory) as session:
            service.record_execution_outcome(
                session,
                operation_id=op_id,
                outcome="success",
                result={"contact_id": "c_1", "api_key": API_KEY_LOOKALIKE},
            )
        return op_id
    finally:
        engine.dispose()


# --------------------------------------------------------------------------------------
# AC-22 — audit verify
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_verify_passes_on_a_clean_database(prepared: str, cli_env: None) -> None:
    result = runner.invoke(app, ["audit", "verify"])
    assert result.exit_code == 0
    assert "OK" in result.output


@pytest.mark.integration
def test_verify_json_shape_on_a_clean_database(prepared: str, cli_env: None) -> None:
    result = runner.invoke(app, ["audit", "verify", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload == {"ok": True, "first_break_seq": None, "reason": None}


@pytest.mark.integration
def test_verify_on_an_empty_but_initialized_database(cli_env: None, cli_db_url: str) -> None:
    init_result = runner.invoke(app, ["db", "init"])
    assert init_result.exit_code == 0
    result = runner.invoke(app, ["audit", "verify"])
    assert result.exit_code == 0


@pytest.mark.integration
def test_verify_before_db_init(cli_env: None) -> None:
    result = runner.invoke(app, ["audit", "verify"])
    assert result.exit_code == 1
    assert "db init" in result.output


@pytest.mark.integration
def test_verify_identifies_the_exact_sequence_number_after_one_row_is_tampered(
    prepared: str, cli_env: None, cli_db_url: str
) -> None:
    """AC-22: a single mutated row is caught, and the reported sequence number is
    exactly the mutated row's own — not the row before or after it."""
    engine = create_engine_for_url(cli_db_url)
    session_factory = create_session_factory(engine)
    try:
        with session_scope(session_factory) as session:
            seqs = list(session.scalars(select(AuditLogEntry.seq).order_by(AuditLogEntry.seq)))
        tampered_seq = seqs[1]  # the second entry — proves it's not always "the first"
        with session_scope(session_factory) as session:
            session.execute(
                update(AuditLogEntry)
                .where(AuditLogEntry.seq == tampered_seq)
                .values(action="tampered.action")
            )
    finally:
        engine.dispose()

    result = runner.invoke(app, ["audit", "verify"])
    assert result.exit_code == 2
    assert f"BROKEN at seq={tampered_seq}" in result.output

    json_result = runner.invoke(app, ["audit", "verify", "--json"])
    assert json_result.exit_code == 2
    payload = json.loads(json_result.output)
    assert payload["ok"] is False
    assert payload["first_break_seq"] == tampered_seq


# --------------------------------------------------------------------------------------
# AC-25 — audit export
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_export_produces_a_complete_chain_verifiable_record(prepared: str, cli_env: None) -> None:
    result = runner.invoke(app, ["audit", "export"])
    assert result.exit_code == 0
    record = json.loads(result.output)

    assert record["chain"]["ok"] is True
    assert record["chain"]["first_break_seq"] is None
    assert record["chain"]["entry_count"] == len(record["audit_log"])
    assert record["chain"]["entry_count"] > 0

    assert len(record["operations"]) == 1
    operation = record["operations"][0]
    assert operation["id"] == prepared
    assert operation["state"] == "SUCCEEDED"
    transitions = [event["transition"] for event in operation["events"]]
    assert transitions == ["T01", "T04", "T06", "T10", "T13"]
    for event in operation["events"]:
        assert event["actor"]
        assert event["occurred_at"]
        assert event["from_state"] is None or isinstance(event["from_state"], str)
        assert isinstance(event["to_state"], str)

    assert operation["execution_result"]["status"] == "success"
    assert operation["definition_hash"] == "sha256:" + "a" * 64

    assert len(record["registry_snapshots"]) == 1
    snapshot = record["registry_snapshots"][0]
    assert snapshot["id"] == operation["snapshot_id"]
    assert snapshot["document"]["workflows"][0]["id"] == "wf.secret"


@pytest.mark.integration
def test_export_redacts_arguments_and_results_per_workflow_policy(
    prepared: str, cli_env: None
) -> None:
    result = runner.invoke(app, ["audit", "export"])
    record = json.loads(result.output)
    operation = record["operations"][0]

    assert operation["arguments"]["email"] == "a@b.com"
    assert operation["arguments"]["api_key"] == "[REDACTED]"
    assert API_KEY_LOOKALIKE not in result.output

    execution_result = operation["execution_result"]
    assert execution_result["redacted_payload"]["contact_id"] == "c_1"
    assert execution_result["redacted_payload"]["api_key"] == "[REDACTED]"


@pytest.mark.integration
def test_export_never_includes_an_approval_token(prepared: str, cli_env: None) -> None:
    """No ``approvals`` table content — including a token hash — appears anywhere in
    the export; ``operation_events`` alone carries the T06 decision, actor, and
    timestamp (this module's docstring; core.service.export_audit_record's own)."""
    result = runner.invoke(app, ["audit", "export"])
    record = json.loads(result.output)
    assert "approvals" not in record
    dumped = json.dumps(record)
    assert "token" not in dumped.lower()


@pytest.mark.integration
def test_export_to_a_file(prepared: str, cli_env: None, tmp_path: Path) -> None:
    output_path = tmp_path / "export.json"
    result = runner.invoke(app, ["audit", "export", "--output", str(output_path)])
    assert result.exit_code == 0
    assert output_path.exists()
    record = json.loads(output_path.read_text())
    assert record["chain"]["ok"] is True
    assert len(record["operations"]) == 1
    assert "Wrote 1 operation(s)" in result.output


@pytest.mark.integration
def test_export_before_db_init(cli_env: None) -> None:
    result = runner.invoke(app, ["audit", "export"])
    assert result.exit_code == 1
    assert "db init" in result.output


@pytest.mark.integration
def test_export_warns_and_exits_with_the_chain_broken_code_when_tampered(
    prepared: str, cli_env: None, cli_db_url: str
) -> None:
    engine = create_engine_for_url(cli_db_url)
    session_factory = create_session_factory(engine)
    try:
        with session_scope(session_factory) as session:
            session.execute(
                update(AuditLogEntry).where(AuditLogEntry.seq == 1).values(action="tampered")
            )
    finally:
        engine.dispose()

    result = runner.invoke(app, ["audit", "export"])
    assert result.exit_code == 2
    assert "WARNING" in result.stderr
    record = json.loads(result.stdout)
    assert record["chain"]["ok"] is False


@pytest.mark.integration
def test_export_reverification_in_a_separate_process(
    prepared: str, cli_env: None, tmp_path: Path
) -> None:
    """Not just re-parsing the export in this test process: a genuinely separate
    Python interpreter reads the exported file, reconstructs entries satisfying
    ``audit.chain.AuditEntryLike`` purely from the JSON, and calls ``verify_chain``
    itself — proving the export alone (no live database) is enough to re-verify."""
    output_path = tmp_path / "export.json"
    export_result = runner.invoke(app, ["audit", "export", "--output", str(output_path)])
    assert export_result.exit_code == 0

    script = f"""
import json
from datetime import datetime
from types import SimpleNamespace

from n8n_operator.audit.chain import verify_chain

with open({str(output_path)!r}, "r", encoding="utf-8") as f:
    record = json.load(f)

entries = [
    SimpleNamespace(
        seq=e["seq"],
        prev_hash=e["prev_hash"],
        entry_hash=e["entry_hash"],
        occurred_at=datetime.fromisoformat(e["occurred_at"]),
        actor=e["actor"],
        action=e["action"],
        subject_type=e["subject_type"],
        subject_id=e["subject_id"],
        outcome=e["outcome"],
        detail=e["detail"],
    )
    for e in record["audit_log"]
]

result = verify_chain(entries)
print(json.dumps({{"ok": result.ok, "first_break_seq": result.first_break_seq}}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    reverified = json.loads(completed.stdout)
    assert reverified == {"ok": True, "first_break_seq": None}


@pytest.mark.integration
def test_export_reverification_in_a_separate_process_detects_tampering(
    prepared: str, cli_env: None, cli_db_url: str, tmp_path: Path
) -> None:
    engine = create_engine_for_url(cli_db_url)
    session_factory = create_session_factory(engine)
    try:
        with session_scope(session_factory) as session:
            session.execute(
                update(AuditLogEntry).where(AuditLogEntry.seq == 2).values(actor="someone-else")
            )
    finally:
        engine.dispose()

    output_path = tmp_path / "export.json"
    runner.invoke(app, ["audit", "export", "--output", str(output_path)])

    script = f"""
import json
from datetime import datetime
from types import SimpleNamespace

from n8n_operator.audit.chain import verify_chain

with open({str(output_path)!r}, "r", encoding="utf-8") as f:
    record = json.load(f)

entries = [
    SimpleNamespace(
        seq=e["seq"], prev_hash=e["prev_hash"], entry_hash=e["entry_hash"],
        occurred_at=datetime.fromisoformat(e["occurred_at"]), actor=e["actor"],
        action=e["action"], subject_type=e["subject_type"], subject_id=e["subject_id"],
        outcome=e["outcome"], detail=e["detail"],
    )
    for e in record["audit_log"]
]

result = verify_chain(entries)
print(json.dumps({{"ok": result.ok, "first_break_seq": result.first_break_seq}}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    reverified = json.loads(completed.stdout)
    assert reverified["ok"] is False
    assert reverified["first_break_seq"] == 2


@pytest.mark.integration
def test_audit_verify_succeeds_under_v2_with_the_dev_principal(
    cli_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage 03: ``audit verify``/``audit export`` now require the ``admin`` role in
    v2 mode (``core.service._require_admin``) — the CLI's own identity (Stage 03,
    ``core.identity.resolve_cli_principal_id``) is always the fixed dev/service
    principal, which ``ensure_dev_principal`` idempotently grants a real ``admin``
    membership (Stage 03's "dev principal needs a real membership to be usable"
    decision), so this command keeps working exactly as it always did under
    ``enable_v2=True`` — "insufficient role" itself is proven against a real,
    non-admin principal at the ``core.service`` level
    (``tests/integration/test_authorization_service.py``), since the CLI's own
    identity can never be anything other than admin by this stage's design."""
    monkeypatch.setenv("N8N_OPERATOR_ENABLE_V2", "true")
    init_result = runner.invoke(app, ["db", "init"])
    assert init_result.exit_code == 0, init_result.output

    result = runner.invoke(app, ["audit", "verify"])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output
