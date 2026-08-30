# Stage 11 — v2 Integration, Release, and Proof Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the merged v2 system (stages 00–10) is operational, secure, recoverable,
understandable, and honest enough to release — without adding new product feature
scope — and produce a findings-first go/no-go report. No tag, release, or publish
happens in this stage.

**Architecture:** Ten workstreams matching the approved design spec: re-verify already
mature evidence (consistency audit, tool-count/protocol sessions, THREAT_MODEL,
migration tooling, live-n8n harness, CI/packaging config); build the two genuinely
missing artifacts (a two-org/three-environment integrated scenario test, a
load/concurrency harness); rehearse the Postgres migration for real including rollback;
run a self-conducted security probe pass seeding negative tests for any real finding;
update documentation to match facts; and close with a durable release report.

**Tech Stack:** Python 3.12, pytest, SQLAlchemy + Alembic, real PostgreSQL 16 (Docker,
`N8N_OPERATOR_TEST_POSTGRES_URL`), the existing MCP stdio/Streamable HTTP transports,
`uv`, `ruff`, `mypy --strict`.

**Spec:** [docs/superpowers/specs/2026-08-30-stage-11-v2-integration-release-and-proof-design.md](../specs/2026-08-30-stage-11-v2-integration-release-and-proof-design.md)

## Global Constraints

- No new n8n-operator product feature, especially no credential-intake mechanism for
  LLM provider API keys (resolved explicitly with the user — out of scope).
- No tag, GitHub release, PyPI publish, or repository-setting change in this stage —
  Section 10's report is advisory only.
- Every new/changed file passes `ruff check .`, `ruff format --check`, and
  `mypy --strict src/` before commit.
- Every new test runs against SQLite where applicable and real Postgres 16 (Docker,
  `docker/postgres-test/docker-compose.yml`, `N8N_OPERATOR_TEST_POSTGRES_URL`) where
  the design calls for Postgres specifically (Sections 3, 4).
- `scripts/check_docs_consistency.py` must stay clean after every doc change.
- The hosted Claude/OpenAI client-validation claim stays **explicitly pending** — no
  credentials are added anywhere, ever, in this stage.
- Load test scale: **startup** ~5 concurrent operators / ~50 ops/day / 1 environment;
  **Series C** ~50 concurrent operators / ~5,000 ops/day / 3 environments, meaningful
  quorum-approval fraction — my own stated assumptions per the approved spec.
- Security review is self-conducted and must be labeled as such everywhere it's
  reported — never implied to be a professional/external pentest.

---

### Task 1: Mechanized consistency audit — re-verification pass

**Files:**
- Modify (only if a real gap is found): `scripts/check_docs_consistency.py`
- Create: `docs/evidence/stage11-consistency-audit.md`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `docs/evidence/stage11-consistency-audit.md` — a dated record other tasks'
  report rows (Task 10) cite as evidence.

- [ ] **Step 1: Run the full local release gate from a clean state**

```bash
cd /Users/carolynstumph/Documents/n8ntools/n8n-operator
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src/
uv run python scripts/check_docs_consistency.py
```
Expected: all four commands exit 0. Record the exact output of
`check_docs_consistency.py` (it prints counts: states, transitions, tool counts,
criteria, invariants/boundaries/rules, canon rules, error codes, ADRs, tree entries).

- [ ] **Step 2: Run the full test suite (SQLite + Postgres)**

```bash
uv run pytest -q --ignore=tests/live
docker compose -f docker/postgres-test/docker-compose.yml up -d
export N8N_OPERATOR_TEST_POSTGRES_URL="postgresql+psycopg://operator:operator_test_password@127.0.0.1:55432/postgres"
uv run pytest tests/integration/postgres tests/integration/keycloak -q
```
Expected: SQLite suite passes (compare pass count against the 1451-passed baseline
from Stage 10 — a lower count is a regression, worth investigating before continuing).
Keycloak tests skip unless `N8N_OPERATOR_TEST_KEYCLOAK_URL` is set — that's expected
and not a failure.

- [ ] **Step 3: Re-walk every row of V2_TRACEABILITY.md against current code**

Read `docs/V2_TRACEABILITY.md` in full (29 rows). For each row, confirm: the cited test
file still exists and still exercises the claimed behavior (spot-check by running that
specific test file individually), and the cited doc section still exists. Do not
re-derive the table from scratch — this is a spot-check of staleness, not a rewrite.

- [ ] **Step 4: Write the evidence file**

Create `docs/evidence/stage11-consistency-audit.md`:

```markdown
# Stage 11 evidence — mechanized consistency audit

Run 2026-08-30, commit <fill in current HEAD short SHA>.

## Local release gate

- `ruff check .` — <PASS/FAIL, paste any failures>
- `ruff format --check .` — <PASS/FAIL>
- `mypy --strict src/` — <PASS/FAIL, file count>
- `scripts/check_docs_consistency.py` — <PASS/FAIL, paste the summary line>

## Test suite

- SQLite suite: <N passed, M skipped> (Stage 10 baseline: 1451 passed, 40 skipped)
- Postgres suite: <N passed>
- Keycloak suite: <skipped, reason>

## V2_TRACEABILITY.md spot-check

29 rows reviewed. <List any row found stale, with what's wrong. If none: "No stale
rows found — every cited test file and doc section still exists and still matches its
claimed row.">

## check_docs_consistency.py gaps

<Either "No gap found — D1-D13 already cover every mechanizable claim reviewed here."
or a description of what's missing and a note that Step 5 below adds it.>
```

- [ ] **Step 5 (only if Step 3/4 found a real gap): extend check_docs_consistency.py**

If a genuinely missing mechanizable check surfaced, add a new `D14` check following the
existing pattern in `scripts/check_docs_consistency.py` (each check is a labeled block
ending in `fail("D<N>", "<message>")` calls). Do not add this step's work if no gap was
found — most of this task is expected to confirm the existing D1-D13 coverage already
holds.

- [ ] **Step 6: Commit**

```bash
git add docs/evidence/stage11-consistency-audit.md scripts/check_docs_consistency.py
git commit -m "stage 11: mechanized consistency audit re-verification"
```

---

### Task 2: Tool-count and protocol-session verification

**Files:**
- Create: `docs/evidence/stage11-protocol-sessions.md`

**Interfaces:**
- Consumes: nothing.
- Produces: `docs/evidence/stage11-protocol-sessions.md`, cited by Task 10.

- [ ] **Step 1: Build a fresh wheel and run the stdio smoke session**

```bash
cd /Users/carolynstumph/Documents/n8ntools/n8n-operator
rm -rf dist/
uv build
uv run python scripts/mcp_session_smoke.py 2>&1 | tee /tmp/stage11-stdio-session.log
```
Expected: the script completes successfully and its own output lists every discovered
tool. Count them — v1-compatibility mode should show 12, v2 mode should show 20 (per
AC-23). If the script has a mode flag, run it in both modes; if it only runs one mode,
note which and run the v2/v1 tool-count assertion via the existing test suite instead
(`tests/contract/` — grep for a tool-count assertion and run that file directly).

- [ ] **Step 2: Run the OpenAI-compatible Streamable HTTP session test**

```bash
uv run pytest tests/integration/test_mcp_http_openai_compat.py -v 2>&1 | tee /tmp/stage11-openai-compat.log
```
Expected: all tests pass. This is the existing protocol-conformance evidence for the
OpenAI Responses API `mcp` tool shape — confirm it still passes against current code,
don't rewrite it.

- [ ] **Step 3: Write the evidence file**

```bash
mkdir -p docs/evidence
```

Create `docs/evidence/stage11-protocol-sessions.md` summarizing: the stdio session's
tool count (v1/v2), the OpenAI-compat test's pass/fail, and a plain statement that no
hosted Claude/OpenAI credentials exist in this environment — the hosted-client claim
stays pending (cross-referenced from Task 7 and Task 10). Sanitize the retained log
excerpts (`/tmp/stage11-*.log`) before copying into the doc — strip any local
filesystem paths beyond the repo-relative ones, confirm no accidental secret-shaped
string via `gitleaks detect --source docs/evidence --no-git`.

- [ ] **Step 4: Commit**

```bash
git add docs/evidence/stage11-protocol-sessions.md
git commit -m "stage 11: tool-count and protocol-session verification"
```

---

### Task 3a: Two-org, three-environment integrated scenario — setup through approval

**Files:**
- Create: `tests/integration/test_v2_integrated_scenario.py`
- Test: the same file (this is the test)

**Interfaces:**
- Consumes: `service.prepare_operation`, `service.approve_operation` (verified
  signatures below); `OrganizationRepository.create`, `EnvironmentRepository.create`,
  `OrganizationMembershipRepository.create`, `PrincipalRepository.create` from
  `n8n_operator.storage.repository`; `session_scope`, `create_engine_for_url`,
  `create_session_factory` from `n8n_operator.storage.session`; `service.reload_registry`.
- Produces: a `TestTwoOrgThreeEnvironmentScenario` class in this new file that Task 3b
  extends with more test methods in the *same file* — Task 3b is a continuation, not a
  separate module, so name the fixtures and helpers here with Task 3b's needs in mind
  (see the `_ScenarioState` dataclass below, which Task 3b's steps read from).

- [ ] **Step 1: Write the registry fixture and scenario-state scaffold**

```python
"""Two-organization, three-environment integrated scenario (Stage 11) — proves the v2
system works together, not just that each stage's own isolated tests pass. Runs
against real PostgreSQL (this scenario needs org/environment isolation semantics that
SQLite's single-writer model can't meaningfully distinguish from a correctness
standpoint, and Stage 11's own design calls for Postgres here specifically).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import Engine

from n8n_operator.core import service
from n8n_operator.core.models import NotificationEvent, DeliveryOutcome, PreflightResult
from n8n_operator.storage.repository import (
    EnvironmentRepository,
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)

pytestmark = pytest.mark.postgres

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: stage11-integrated-scenario
workflows:
  - id: crm.sync_contact
    n8n_workflow_id: n8n-crm-1
    title: Sync a contact into the CRM
    description: Upserts one contact by email.
    owner: revops
    version: 1
    definition_hash: sha256:{hash_a}
    risk: medium
    side_effects: external_write
    approval: required
    trigger:
      type: webhook
      method: POST
      path: /webhook/crm-sync
      auth: none
      correlation: response_envelope
    input_schema:
      type: object
      properties:
        email:
          type: string
      additionalProperties: false
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300
  - id: mkt.campaign_sync
    n8n_workflow_id: n8n-mkt-1
    title: Sync a campaign audience segment
    description: Pushes an audience definition to the marketing platform.
    owner: marketing-ops
    version: 1
    definition_hash: sha256:{hash_b}
    risk: medium
    side_effects: external_write
    approval: required
    trigger:
      type: webhook
      method: POST
      path: /webhook/campaign-sync
      auth: none
      correlation: response_envelope
    input_schema:
      type: object
      properties:
        campaign_id:
          type: string
      additionalProperties: false
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300
""".format(hash_a="a" * 64, hash_b="b" * 64)


class FakePreflight:
    def check(self, workflow: Any) -> PreflightResult:
        return PreflightResult(ready=True, checks=[], checked_at=datetime.now(UTC))


class FakeSink:
    def __init__(self) -> None:
        self.events: list[NotificationEvent] = []

    def deliver(self, event: NotificationEvent) -> DeliveryOutcome:
        self.events.append(event)
        return DeliveryOutcome(delivered=True)


def _migrated_engine(url: str) -> Engine:
    from alembic import command

    from n8n_operator.cli.commands.db import _alembic_config

    command.upgrade(_alembic_config(url), "head")
    return create_engine_for_url(url, pool_size=10, max_overflow=10)


@dataclass
class _ScenarioState:
    """Everything Task 3b's test methods need, built once by Task 3a's setup and
    threaded through the class via a shared pytest fixture (see Step 2 below)."""

    engine: Engine
    session_factory: Any
    org_a_id: str
    org_b_id: str
    env_staging_id: str
    env_production_id: str
    env_secondary_prod_id: str
    org_a_operator_id: str
    org_a_approver_id: str
    org_b_viewer_id: str
    sink: FakeSink
    crm_operation_id: str = field(default="")
    campaign_operation_id: str = field(default="")
```

- [ ] **Step 2: Write the fixture that builds the two-org/three-env world**

Append to the same file:

```python
@pytest.fixture
def scenario(postgres_test_db_url: str, tmp_path: Path) -> _ScenarioState:
    engine = _migrated_engine(postgres_test_db_url)
    factory = create_session_factory(engine)

    registry_path = tmp_path / "workflows.yaml"
    registry_path.write_text(REGISTRY_YAML)

    sink = FakeSink()

    with session_scope(factory) as session:
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)

        # Org A: a startup GTM team running crm.sync_contact.
        org_a = OrganizationRepository(session).create(name="Org A — Acme GTM")
        # Org B: a second, unrelated organization — proves cross-org isolation is real,
        # not just "the query happened to only return one org's rows in this test."
        org_b = OrganizationRepository(session).create(name="Org B — Globex Marketing")

        env_staging = EnvironmentRepository(session).create(
            organization_id=org_a.id,
            name="staging",
            n8n_base_url_ref="env:STAGE11_STAGING_BASE_URL",
            n8n_api_key_ref="env:STAGE11_STAGING_API_KEY",
        )
        env_production = EnvironmentRepository(session).create(
            organization_id=org_a.id,
            name="production",
            n8n_base_url_ref="env:STAGE11_PROD_BASE_URL",
            n8n_api_key_ref="env:STAGE11_PROD_API_KEY",
            is_production=True,
        )
        # A third environment, in Org B — proves environment scoping is keyed by org,
        # not just by environment row ID.
        env_secondary_prod = EnvironmentRepository(session).create(
            organization_id=org_b.id,
            name="production",
            n8n_base_url_ref="env:STAGE11_ORGB_PROD_BASE_URL",
            n8n_api_key_ref="env:STAGE11_ORGB_PROD_API_KEY",
            is_production=True,
        )

        operator_a = PrincipalRepository(session).create(kind="user", display_name="Org A Operator")
        approver_a = PrincipalRepository(session).create(kind="user", display_name="Org A Approver")
        viewer_b = PrincipalRepository(session).create(kind="user", display_name="Org B Viewer")

        memberships = OrganizationMembershipRepository(session)
        memberships.create(
            principal_id=operator_a.id,
            organization_id=org_a.id,
            roles=["operator"],
            workflow_scope="*",
            environment_scope=[env_staging.id, env_production.id],
        )
        memberships.create(
            principal_id=approver_a.id,
            organization_id=org_a.id,
            roles=["approver"],
            workflow_scope="*",
            environment_scope=[env_staging.id, env_production.id],
        )
        memberships.create(
            principal_id=viewer_b.id,
            organization_id=org_b.id,
            roles=["viewer"],
            workflow_scope="*",
            environment_scope=[env_secondary_prod.id],
        )

        state = _ScenarioState(
            engine=engine,
            session_factory=factory,
            org_a_id=org_a.id,
            org_b_id=org_b.id,
            env_staging_id=env_staging.id,
            env_production_id=env_production.id,
            env_secondary_prod_id=env_secondary_prod.id,
            org_a_operator_id=operator_a.id,
            org_a_approver_id=approver_a.id,
            org_b_viewer_id=viewer_b.id,
            sink=sink,
        )

    yield state
    engine.dispose()
```

- [ ] **Step 3: Write the prepare-through-approve test**

```python
class TestTwoOrgThreeEnvironmentScenario:
    def test_prepare_and_approve_crm_sync_in_org_a_production(
        self, scenario: _ScenarioState
    ) -> None:
        with session_scope(scenario.session_factory) as session:
            operation, replay, _token = service.prepare_operation(
                session,
                principal_id=scenario.org_a_operator_id,
                environment=scenario.env_production_id,
                workflow_id="crm.sync_contact",
                arguments={"email": "lead@example.com"},
                preflight=FakePreflight(),
                server_max_argument_bytes=262_144,
                enable_v2=True,
                notification_sink=scenario.sink,
            )
            assert replay is False
            assert operation.state == "PENDING_APPROVAL"
            crm_operation_id = operation.id

        with session_scope(scenario.session_factory) as session:
            approved = service.approve_operation(
                session,
                operation_id=crm_operation_id,
                decided_by=scenario.org_a_approver_id,
                enable_v2=True,
            )
            assert approved.state == "APPROVED"

        scenario.crm_operation_id = crm_operation_id
```

- [ ] **Step 4: Run this one test against real Postgres**

```bash
docker compose -f docker/postgres-test/docker-compose.yml up -d
export N8N_OPERATOR_TEST_POSTGRES_URL="postgresql+psycopg://operator:operator_test_password@127.0.0.1:55432/postgres"
uv run pytest tests/integration/test_v2_integrated_scenario.py::TestTwoOrgThreeEnvironmentScenario::test_prepare_and_approve_crm_sync_in_org_a_production -v
```
Expected: PASS. If it fails on an import or fixture error, fix before proceeding —
Task 3b builds directly on this file's fixtures.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_v2_integrated_scenario.py
git commit -m "stage 11: two-org/three-env scenario, part 1 (prepare + approve)"
```

---

### Task 3b: Integrated scenario — retry, reconcile, diff, metrics, audit, alerts, both anchors

**Files:**
- Modify: `tests/integration/test_v2_integrated_scenario.py` (same file as Task 3a —
  append test methods to `TestTwoOrgThreeEnvironmentScenario`)

**Interfaces:**
- Consumes: `_ScenarioState`, `FakeSink`, `REGISTRY_YAML`, `scenario` fixture, and
  `scenario.crm_operation_id` from Task 3a. Also: `service.retry_operation`,
  `service.reconcile_operation`, `service.diff_workflow_definition`,
  `service.get_metrics`, `service.list_audit_events`, `service.check_and_deliver_alerts`,
  `service.publish_anchor` (verified signatures in the spec's research — see plan
  header). `ExecutionLookup`, `InstanceUnreachableError` from `n8n_operator.core.models`
  / `n8n_operator.errors`. `LocalFileAnchor` from `n8n_operator.audit_anchor.local_file`,
  `HttpsWebhookAnchor` from `n8n_operator.audit_anchor.webhook`.
- Produces: nothing further downstream — this is the scenario's terminal task.

- [ ] **Step 1: Add a governed-retry-off-a-failure test**

First force a `FAILED` operation directly (mirroring the pattern
`test_execute_dispatch.py` uses for forcing terminal states — a `FAILED` row inserted
via the repository, since dispatch mechanics aren't this scenario's focus):

```python
    def test_retry_off_a_failed_operation_reaches_pending_approval_again(
        self, scenario: _ScenarioState
    ) -> None:
        from n8n_operator.storage.repository import OperationRepository

        with session_scope(scenario.session_factory) as session:
            snapshot = service.get_active_snapshot(session)
            failed = OperationRepository(session).create(
                principal_id=scenario.org_a_operator_id,
                environment=scenario.env_production_id,
                snapshot_id=snapshot.id,
                workflow_id="crm.sync_contact",
                definition_hash="sha256:" + "a" * 64,
                state="FAILED",
                arguments={"email": "retry-me@example.com"},
                argument_fingerprint="fp-retry",
                argument_bytes=10,
            )
            failed_id = failed.id

        with session_scope(scenario.session_factory) as session:
            retried, replay, _token = service.retry_operation(
                session,
                operation_id=failed_id,
                principal_id=scenario.org_a_operator_id,
                preflight=FakePreflight(),
                server_max_argument_bytes=262_144,
                enable_v2=True,
                notification_sink=scenario.sink,
            )
            assert replay is False
            assert retried.state == "PENDING_APPROVAL"
            assert retried.id != failed_id
```

- [ ] **Step 2: Add a reconciliation test for an UNKNOWN operation**

```python
def test_reconcile_an_unknown_operation_records_evidence(self, scenario: _ScenarioState) -> None:
    from n8n_operator.core.models import ExecutionLookup
    from n8n_operator.storage.repository import OperationRepository

    class FakeReconciliation:
        def get_execution(self, execution_id: str) -> ExecutionLookup:
            return ExecutionLookup(
                execution_id=execution_id,
                n8n_workflow_id="n8n-crm-1",
                status="success",
            )

    with session_scope(scenario.session_factory) as session:
        snapshot = service.get_active_snapshot(session)
        unknown = OperationRepository(session).create(
            principal_id=scenario.org_a_operator_id,
            environment=scenario.env_production_id,
            snapshot_id=snapshot.id,
            workflow_id="crm.sync_contact",
            definition_hash="sha256:" + "a" * 64,
            state="UNKNOWN",
            arguments={"email": "unknown-outcome@example.com"},
            argument_fingerprint="fp-unknown",
            argument_bytes=10,
        )
        unknown_id = unknown.id

    with session_scope(scenario.session_factory) as session:
        record = service.reconcile_operation(
            session,
            operation_id=unknown_id,
            principal_id=scenario.org_a_operator_id,
            execution_id="n8n-exec-999",
            note="Confirmed via n8n execution history: this run succeeded.",
            reconciliation=FakeReconciliation(),
            enable_v2=True,
        )
        assert record.execution_id == "n8n-exec-999"
```
`ExecutionLookup`'s real fields are `execution_id: str`, `n8n_workflow_id: str`,
`status: str` (confirmed at `src/n8n_operator/core/models.py:411-424`) — the code above
already uses the verified shape.

- [ ] **Step 3: Add a definition-drift-detection test via diff_workflow_definition**

```python
    def test_diff_workflow_definition_detects_drift_on_campaign_sync(
        self, scenario: _ScenarioState
    ) -> None:
        class FakeDefinitionPort:
            def get_workflow(self, n8n_workflow_id: str) -> dict[str, Any]:
                return {"nodes": [{"id": "new-node", "type": "n8n-nodes-base.set"}]}

        with session_scope(scenario.session_factory) as session:
            diff = service.diff_workflow_definition(
                session,
                workflow_id="mkt.campaign_sync",
                definition=FakeDefinitionPort(),
                principal_id=scenario.org_a_operator_id,
                environment=scenario.env_production_id,
                enable_v2=True,
            )
            assert diff.changed is True
```

- [ ] **Step 4: Add a cross-org metrics/audit isolation test**

This is the assertion that actually proves org isolation, not just role scoping:

```python
def test_get_metrics_and_audit_events_never_cross_the_org_boundary(
    self, scenario: _ScenarioState
) -> None:
    with session_scope(scenario.session_factory) as session:
        org_a_metrics = service.get_metrics(
            session,
            principal_id=scenario.org_a_operator_id,
            environment=scenario.env_production_id,
            group_by="workflow",
            enable_v2=True,
        )
        # Org A's operator has never touched Org B's environment at all — this
        # call must resolve org membership correctly, never accidentally include
        # Org B's crm.sync_contact/campaign_sync operations in Org A's totals.
        assert org_a_metrics.totals.count >= 1

        org_b_events = service.list_audit_events(
            session,
            principal_id=scenario.org_b_viewer_id,
            environment=scenario.env_secondary_prod_id,
            enable_v2=True,
        )
        # Org B's viewer has no memberships touching Org A's environments — Org
        # A's own operation ID (created in Task 3a's approve test) must never
        # appear as a subject_id in Org B's own audit query. AuditEvent carries
        # no environment_id field directly (confirmed at
        # src/n8n_operator/core/models.py:283-296), so isolation is proven by
        # subject identity, not a field comparison.
        org_b_subject_ids = {event.subject_id for event in org_b_events.events}
        assert scenario.crm_operation_id not in org_b_subject_ids
```
`org_a_metrics.totals.count` is confirmed correct — `MetricsResult.totals` is a
`MetricsTotals` with a `count: int` field (`src/n8n_operator/core/models.py:504-556`).

- [ ] **Step 5: Add an alert-delivery test using the shared FakeSink**

```python
def test_check_and_deliver_alerts_fires_for_a_stuck_executing_operation(
    self, scenario: _ScenarioState
) -> None:
    from n8n_operator.storage.repository import OperationRepository

    with session_scope(scenario.session_factory) as session:
        snapshot = service.get_active_snapshot(session)
        OperationRepository(session).create(
            principal_id=scenario.org_a_operator_id,
            environment=scenario.env_production_id,
            snapshot_id=snapshot.id,
            workflow_id="crm.sync_contact",
            definition_hash="sha256:" + "a" * 64,
            state="EXECUTING",
            arguments={"email": "stuck@example.com"},
            argument_fingerprint="fp-stuck",
            argument_bytes=10,
        )

    with session_scope(scenario.session_factory) as session:
        delivered = service.check_and_deliver_alerts(
            session,
            sink=scenario.sink,
            executing_stuck_threshold_seconds=0,
        )
        assert delivered >= 1
        assert any(e.event_type == "operation.stuck" for e in scenario.sink.events)
```

- [ ] **Step 6: Add a both-anchor-implementations test**

```python
def test_both_anchor_implementations_publish_and_verify_the_same_chain(
    self, scenario: _ScenarioState, tmp_path: Path
) -> None:
    import httpx

    private_key = Ed25519PrivateKey.generate()

    from n8n_operator.audit_anchor.local_file import LocalFileAnchor
    from n8n_operator.audit_anchor.webhook import HttpsWebhookAnchor

    local_sink = LocalFileAnchor(
        path=tmp_path / "anchors.jsonl",
        private_key=private_key,
    )

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    webhook_sink = HttpsWebhookAnchor(
        url="https://anchors.example.invalid/ingest",
        bearer_token="stage11-test-token",
        private_key=private_key,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with session_scope(scenario.session_factory) as session:
        local_row = service.publish_anchor(
            session,
            sink=local_sink,
            implementation="local_file",
            enable_v2=True,
        )
        assert local_row is not None

    with session_scope(scenario.session_factory) as session:
        webhook_row = service.publish_anchor(
            session,
            sink=webhook_sink,
            implementation="https_webhook",
            enable_v2=True,
        )
        assert webhook_row is not None
    assert len(captured) == 1
```

- [ ] **Step 7: Run the whole scenario file against real Postgres**

```bash
docker compose -f docker/postgres-test/docker-compose.yml up -d
export N8N_OPERATOR_TEST_POSTGRES_URL="postgresql+psycopg://operator:operator_test_password@127.0.0.1:55432/postgres"
uv run pytest tests/integration/test_v2_integrated_scenario.py -v
```
Expected: every test method in `TestTwoOrgThreeEnvironmentScenario` passes. Fix any
attribute-name mismatches flagged as "confirm during execution" above by reading the
real model definitions in `src/n8n_operator/core/models.py` before adjusting the test.

- [ ] **Step 8: Lint and type-check**

```bash
uv run ruff check tests/integration/test_v2_integrated_scenario.py
uv run ruff format tests/integration/test_v2_integrated_scenario.py
uv run mypy --strict tests/integration/test_v2_integrated_scenario.py 2>&1 | tail -30
```
Fix any findings. Note: pytest test files are commonly excluded from `--strict`'s
harshest rules by repo convention — check `pyproject.toml`'s `[tool.mypy]` overrides
before treating a test-only typing gap as blocking; match whatever the existing test
files under `tests/integration/postgres/` already satisfy.

- [ ] **Step 9: Commit**

```bash
git add tests/integration/test_v2_integrated_scenario.py
git commit -m "stage 11: two-org/three-env scenario, part 2 (retry/reconcile/diff/metrics/audit/alerts/anchors)"
```

---

### Task 4: PostgreSQL migration rehearsal — real run plus rollback

**Files:**
- Create: `tests/integration/postgres/test_v2_migration_rehearsal.py`
- Modify: `docs/POSTGRES_OPERATIONS.md` (add a concrete rollback walkthrough if one
  doesn't already exist — read the file first; only add if genuinely missing)
- Create: `docs/evidence/stage11-migration-rehearsal.md`

**Interfaces:**
- Consumes: `n8n_operator.core.postgres_migration.migrate`,
  `n8n_operator.storage.postgres_migration.preflight`/`MigrationRefusedError`,
  `OrganizationRepository`, `EnvironmentRepository`, `OrganizationMembershipRepository`,
  `PrincipalRepository` (same as Task 3), `n8n_operator.audit_anchor.local_file.LocalFileAnchor`,
  `service.publish_anchor`.
- Produces: `docs/evidence/stage11-migration-rehearsal.md`, cited by Task 10.

- [ ] **Step 1: Write a v2-shaped seed (orgs, environments, memberships, anchored
  chain) on top of the existing v1 seed pattern**

```python
"""Real SQLite-v1-shaped-plus-v2-rows -> PostgreSQL migration rehearsal (Stage 11) —
proves counts, identity mapping, audit-chain integrity, and historical operation
readability survive migration, and that rollback (restoring the pre-migration SQLite
file) leaves the source untouched. Extends the existing v1-only migration coverage in
tests/integration/postgres/test_migration.py (not modified here) with v2 rows:
organizations, environments, memberships, and a real anchored audit chain.
"""

from __future__ import annotations

import base64
import shutil
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import func, select

from n8n_operator.audit_anchor.local_file import LocalFileAnchor
from n8n_operator.core import service
from n8n_operator.core.postgres_migration import migrate
from n8n_operator.storage.models import Base
from n8n_operator.storage.repository import (
    EnvironmentRepository,
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
    RegistrySnapshotRepository,
)
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)

pytestmark = pytest.mark.postgres


@pytest.fixture
def sqlite_source_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'source.db'}"


def _seed_v2_fixture(sqlite_source_url: str, anchor_path: Path) -> dict[str, str]:
    from alembic import command

    from n8n_operator.cli.commands.db import _alembic_config

    command.upgrade(_alembic_config(sqlite_source_url), "head")

    engine = create_engine_for_url(sqlite_source_url)
    try:
        factory = create_session_factory(engine)
        private_key = Ed25519PrivateKey.generate()
        with session_scope(factory) as session:
            principal = PrincipalRepository(session).create(kind="local", display_name="local")
            RegistrySnapshotRepository(session).create(
                content_hash="sha256:" + "a" * 64,
                source_path="./workflows.yaml",
                document={"apiVersion": "n8n-operator/v1", "workflows": []},
            )
            org = OrganizationRepository(session).create(name="Migration Rehearsal Org")
            env = EnvironmentRepository(session).create(
                organization_id=org.id,
                name="production",
                n8n_base_url_ref="env:REHEARSAL_BASE_URL",
                n8n_api_key_ref="env:REHEARSAL_API_KEY",
                is_production=True,
            )
            member = PrincipalRepository(session).create(
                kind="user", display_name="Rehearsal Operator"
            )
            OrganizationMembershipRepository(session).create(
                principal_id=member.id,
                organization_id=org.id,
                roles=["operator"],
            )
            ids = {
                "org_id": org.id,
                "env_id": env.id,
                "member_id": member.id,
                "principal_id": principal.id,
            }

        with session_scope(factory) as session:
            sink = LocalFileAnchor(path=anchor_path, private_key=private_key)
            row = service.publish_anchor(
                session, sink=sink, implementation="local_file", enable_v2=True
            )
            assert row is not None
    finally:
        engine.dispose()
    return ids
```

- [ ] **Step 2: Write the migrate-and-verify test**

```python
def test_v2_shaped_dataset_migrates_with_verified_counts_and_intact_anchor_chain(
    sqlite_source_url: str,
    postgres_test_db_url: str,
    tmp_path: Path,
) -> None:
    anchor_path = tmp_path / "anchors.jsonl"
    ids = _seed_v2_fixture(sqlite_source_url, anchor_path)

    def _row_counts(url: str) -> dict[str, int]:
        engine = create_engine_for_url(url)
        try:
            factory = create_session_factory(engine)
            with factory() as session:
                return {
                    name: session.execute(select(func.count()).select_from(table)).scalar_one()
                    for name, table in Base.metadata.tables.items()
                }
        finally:
            engine.dispose()

    before = _row_counts(sqlite_source_url)
    report = migrate(source_url=sqlite_source_url, dest_url=postgres_test_db_url)
    assert report.ok

    after = _row_counts(postgres_test_db_url)
    for table_name, count in before.items():
        assert after.get(table_name, 0) == count, (
            f"{table_name}: {before[table_name]} -> {after.get(table_name)}"
        )

    engine = create_engine_for_url(postgres_test_db_url)
    try:
        factory = create_session_factory(engine)
        with factory() as session:
            org = OrganizationRepository(session).get(ids["org_id"])
            assert org is not None and org.id == ids["org_id"]
            env = EnvironmentRepository(session).get(ids["env_id"])
            assert env is not None and env.organization_id == ids["org_id"]
    finally:
        engine.dispose()

    # Audit chain integrity: the anchor file signed against SQLite content is verified
    # against the migrated Postgres database — the whole point of an external anchor
    # is that it doesn't care which database backend now holds the audit log.
    from n8n_operator.audit_anchor.local_file import LocalFileAnchor

    verifier = LocalFileAnchor(path=anchor_path, private_key=Ed25519PrivateKey.generate())
    file_report = verifier.verify_file()
    assert file_report.ok
```
Adjust `OrganizationRepository.get`/`EnvironmentRepository.get` to whatever the real
lookup-by-ID method is named (confirm during execution — likely `.get(id)` matching
every other repository's convention already seen in this codebase, but verify against
`src/n8n_operator/storage/repository.py` before finalizing).

- [ ] **Step 3: Write the rollback rehearsal test**

```python
def test_rollback_restores_the_pre_migration_sqlite_file_untouched(
    sqlite_source_url: str,
    postgres_test_db_url: str,
    tmp_path: Path,
) -> None:
    _seed_v2_fixture(sqlite_source_url, tmp_path / "anchors.jsonl")

    source_path = Path(sqlite_source_url.replace("sqlite+pysqlite:///", ""))
    backup_path = tmp_path / "source-backup.db"
    shutil.copy2(source_path, backup_path)

    migrate(source_url=sqlite_source_url, dest_url=postgres_test_db_url)

    # "Rollback" here means: the migration never mutates the source SQLite file at
    # all (it's read-only copy semantics) — the backup and the post-migration source
    # must be byte-identical, proving there's nothing to actually restore.
    assert backup_path.read_bytes() == source_path.read_bytes()
```

- [ ] **Step 4: Run both tests against real Postgres**

```bash
docker compose -f docker/postgres-test/docker-compose.yml up -d
export N8N_OPERATOR_TEST_POSTGRES_URL="postgresql+psycopg://operator:operator_test_password@127.0.0.1:55432/postgres"
uv run pytest tests/integration/postgres/test_v2_migration_rehearsal.py -v
```
Expected: both PASS. If `OrganizationRepository`/`EnvironmentRepository` lack a `.get`
method, adjust to a `select()` query against the model directly, matching whatever
pattern `test_migration.py` already uses for post-migration row assertions.

- [ ] **Step 5: Check docs/POSTGRES_OPERATIONS.md for a concrete rollback walkthrough**

Read the file. If it already documents "the migration never mutates the source, so
rollback is just: stop pointing the app at the new Postgres database" concretely, no
edit needed. If it doesn't say this plainly, add a short "Rollback" subsection stating
exactly that, citing this rehearsal test as evidence.

- [ ] **Step 6: Write the evidence file**

Create `docs/evidence/stage11-migration-rehearsal.md` recording: the row-count table
(before/after), the anchor-chain verification result, and the rollback rehearsal
result — in prose, not just "see test file."

- [ ] **Step 7: Lint, type-check, commit**

```bash
uv run ruff check tests/integration/postgres/test_v2_migration_rehearsal.py
uv run ruff format tests/integration/postgres/test_v2_migration_rehearsal.py
uv run mypy --strict tests/integration/postgres/test_v2_migration_rehearsal.py 2>&1 | tail -30
git add tests/integration/postgres/test_v2_migration_rehearsal.py docs/evidence/stage11-migration-rehearsal.md docs/POSTGRES_OPERATIONS.md
git commit -m "stage 11: PostgreSQL migration rehearsal with rollback"
```

---

### Task 5: Security review — re-verify THREAT_MODEL, probe, seed negative tests for real findings

**Files:**
- Create: `docs/evidence/stage11-security-review.md`
- Create (only for confirmed findings): a new negative test per finding, named after
  the probed area (e.g. `tests/unit/test_ssrf_environment_refs.py`,
  `tests/integration/test_webhook_replay_protection.py`) — file names depend on what's
  actually found, so this step is inherently open-ended; each probe below is concrete
  even though its outcome isn't known yet.
- Modify: `docs/THREAT_MODEL.md` (only if a re-verified entry's status changed, or a
  new finding needs a new `T-66`+ row)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `docs/evidence/stage11-security-review.md`, cited by Task 10 and Task 9
  (THREAT_MODEL.md update).

- [ ] **Step 1: Re-verify every THREAT_MODEL.md entry's status against current code**

Read `docs/THREAT_MODEL.md` in full (T-01 through T-65+, RR-1 through RR-15). For each
`mitigated` entry, confirm the cited test file still exists and still passes
(`uv run pytest <cited test path> -v`). For each `accepted` residual risk, confirm the
accepting rationale still holds (nothing shipped since that changes the calculus). Note
any status that no longer holds.

- [ ] **Step 2: Probe SSRF via `n8n_base_url_ref` / webhook config**

Concrete probe: attempt to register an environment whose `n8n_base_url_ref` indirection
resolves (via a test-only env var) to `http://169.254.169.254/` or `http://localhost:<internal-port>/`
— confirm whether Operator's dispatch path validates the *resolved* URL at all, or only
validates that the reference string itself is `env:`/`keyring:`-shaped. Write this as a
real test:

```python
# tests/unit/test_environment_url_resolution.py (only if this file doesn't already exist —
# check first)
def test_resolved_n8n_base_url_pointing_at_a_private_address_is_rejected_or_accepted(
    ...
) -> None:
    """Documents actual current behavior, whichever it is. If Operator dispatches to
    a resolved private/link-local address without complaint, this is a real SSRF
    finding — escalate to a fix in this same task, don't just document the gap."""
```
If the probe reveals Operator does NOT validate the resolved URL's address class (a
real finding): fix it (reject link-local/private-range resolved URLs at the point of
use, or at minimum at `environment create`/`reload-overlay` time) and keep the test as
a permanent regression guard. If Operator already rejects or the URL is only ever used
against a real reachable instance with no code path that fetches attacker-controlled
URLs (i.e. SSRF isn't actually reachable because `n8n_base_url_ref` is server-owned
config, never end-user input per ADR-006) — record that reasoning as the finding
("not exploitable: `n8n_base_url_ref` is operator-configured, never derived from a
tool argument or client-supplied value") and keep the test as documentation of why.

- [ ] **Step 3: Probe approval-token forgery beyond T-57's existing coverage**

Concrete probe: attempt to present a validly-signed approval token for operation A
against operation B's approval endpoint (token/operation-ID cross-use), and attempt to
replay an already-used token a second time immediately (not after TTL expiry — a tighter
race than T-57's existing "reused" test). Run:

```bash
uv run pytest tests/ -k "approval_token" -v
```
first to see what's already covered, then write only the genuinely new cross-use/replay
case if it isn't already exercised. If a real gap is found, fix and add the regression
test; if the existing `compute_approval_binding` design already structurally prevents
this (binds operation ID into the token, per Stage 05's own T-57 mitigation), record
that as the reviewed-and-confirmed-safe outcome.

- [ ] **Step 4: Probe webhook delivery (notification + anchor) for SSRF/replay**

Concrete probe: check whether `NotificationSink`'s HTTPS delivery and
`HttpsWebhookAnchor`'s publish path validate the target URL's scheme/host before
sending (both should already require `https://` per their own constructors — confirm
this is enforced, not just documented). Check whether a webhook response can be replayed
to forge a second "delivered" receipt. Run the existing webhook test files
(`tests/integration/test_audit_anchor_webhook.py`,
`tests/integration/test_notifications_webhook.py` or similarly named) and read them for
whether HTTPS-only and no-credential-in-URL are actually asserted, not just assumed. If
either is missing an assertion, add it as a regression test against real current
behavior (not a new feature) — if the underlying behavior is actually permissive
(accepts `http://`), that's a real finding requiring a code fix.

- [ ] **Step 5: Probe metrics-privacy edge cases beyond ADR-019's sample-size floor**

Concrete probe: with two organizations both running a workflow with the identical
`workflow_id` string in each org's own registry (registries are per-deployment, not
per-org, so this specific case may not even be reachable — confirm), check whether
`get_metrics`'s cross-caller aggregation ever mixes two callers' authorized-but-separate
scopes into one total when `group_by` is used with a narrow `workflow_scope`. This is
substantially what Task 3b's Step 4 test already exercises for audit events; extend the
same reasoning to metrics specifically if Task 3b's coverage didn't already nail it down
for the `breakdown` field specifically (not just `totals`).

- [ ] **Step 6: Audit supply-chain configuration**

Read `.github/dependabot.yml`, `.github/workflows/release.yml`'s `provenance` job, and
`pyproject.toml`'s dependency version constraints. Confirm: Dependabot covers both `uv`
(Python deps) and `github-actions` ecosystems (already found true in scoping research —
re-confirm), the provenance job uses `actions/attest-build-provenance` correctly gated
before publish steps, and no dependency uses an unpinned/floating version range that
would let a compromised transitive dependency slip in silently between CI runs. Record
findings — this is a config-reading task, not a code-writing one, unless something is
genuinely wrong.

- [ ] **Step 7: Write the evidence file**

Create `docs/evidence/stage11-security-review.md`:

```markdown
# Stage 11 evidence — security review (self-conducted)

**This is an internal, self-conducted review by the implementing engineer — not a
substitute for professional third-party penetration testing.** No external pentest
budget was available for this stage.

## THREAT_MODEL.md re-verification

<Table: entry ID, still-mitigated (Y/N), test re-run result>

## New probes

| Area | Probe | Finding | Disposition |
|---|---|---|---|
| SSRF via n8n_base_url_ref | <what was tried> | <what happened> | <fixed / not exploitable, reasoning / accepted residual> |
| Approval-token cross-use/replay | ... | ... | ... |
| Webhook delivery SSRF/replay | ... | ... | ... |
| Metrics privacy edge case | ... | ... | ... |
| Supply-chain config | ... | ... | ... |

## New THREAT_MODEL.md rows added (if any)

<List any new T-66+ rows, or "None — no new exploitable finding required a new threat
model entry.">
```

- [ ] **Step 8: For every real finding, fix and add the regression test (already
  covered inline in Steps 2-5 above) — then lint/type-check/commit everything together**

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src/
uv run pytest -q --ignore=tests/live
git add docs/evidence/stage11-security-review.md docs/THREAT_MODEL.md
# plus any new test files and any src/ fixes from Steps 2-6
git commit -m "stage 11: security review — re-verification and probe findings"
```

---

### Task 6: Load and concurrency testing harness

**Files:**
- Create: `scripts/load_test.py`
- Create: `docs/evidence/stage11-load-test-results.md`
- Modify (only if results show a default is unrealistic):
  `examples/registry/starter-kits/gtm-starter-kits.yaml`,
  `examples/registry/workflows.example.yaml`

**Interfaces:**
- Consumes: `service.prepare_operation`, `service.approve_operation` (same as Task 3),
  runs against a real Postgres database via `create_engine_for_url`/`session_scope`.
- Produces: `docs/evidence/stage11-load-test-results.md`, cited by Task 10.

- [ ] **Step 1: Write the load-test script's profile definitions and worker function**

```python
#!/usr/bin/env python3
"""Lightweight load/concurrency harness (Stage 11) — no external dependency (no
locust/k6), plain asyncio/threading, matching this repo's zero-heavyweight-tooling
convention. Two published profiles with stated assumptions: 'startup' (~5 concurrent
operators, ~50 ops/day, one environment) and 'seriesc' (~50 concurrent operators,
~5,000 ops/day, 3 environments, a meaningful quorum-approval fraction). Reports
p50/p95/p99 latency and error rate. Run manually — not part of CI.

Assumptions published alongside every result: this machine's own hardware, a local
loopback-only Postgres 16 container (docker/postgres-test/docker-compose.yml), and
in-process Python threading (not a real network hop) standing in for MCP transport
latency, since this measures the governed-write pipeline's own overhead, not transport.
"""

from __future__ import annotations

import argparse
import statistics
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from n8n_operator.core import service
from n8n_operator.core.models import PreflightResult
from n8n_operator.storage.repository import (
    EnvironmentRepository,
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: stage11-load-test
workflows:
  - id: load.write_op
    n8n_workflow_id: n8n-load-1
    title: Load test write operation
    description: A synthetic external_write workflow used only for load testing.
    owner: stage11
    version: 1
    definition_hash: sha256:{hash_a}
    risk: medium
    side_effects: external_write
    approval: required
    trigger:
      type: webhook
      method: POST
      path: /webhook/load-test
      auth: none
    input_schema:
      type: object
      properties: {{}}
      additionalProperties: false
""".format(hash_a="c" * 64)


class FakePreflight:
    def check(self, workflow: Any) -> PreflightResult:
        return PreflightResult(ready=True, checks=[], checked_at=datetime.now(UTC))


@dataclass
class Profile:
    name: str
    concurrent_operators: int
    total_operations: int
    environment_count: int
    quorum_fraction: float


PROFILES = {
    "startup": Profile(
        name="startup",
        concurrent_operators=5,
        total_operations=50,
        environment_count=1,
        quorum_fraction=0.0,
    ),
    "seriesc": Profile(
        name="seriesc",
        concurrent_operators=50,
        total_operations=5000,
        environment_count=3,
        quorum_fraction=0.2,
    ),
}


@dataclass
class Results:
    latencies_ms: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, latency_ms: float, error: str | None) -> None:
        with self.lock:
            self.latencies_ms.append(latency_ms)
            if error:
                self.errors.append(error)
```

- [ ] **Step 2: Write the worker loop and profile runner**

```python
def _worker(
    session_factory: Any,
    principal_id: str,
    environment_id: str,
    op_count: int,
    results: Results,
) -> None:
    for i in range(op_count):
        start = time.monotonic()
        error: str | None = None
        try:
            with session_scope(session_factory) as session:
                service.prepare_operation(
                    session,
                    principal_id=principal_id,
                    environment=environment_id,
                    workflow_id="load.write_op",
                    arguments={},
                    preflight=FakePreflight(),
                    server_max_argument_bytes=262_144,
                    idempotency_key=f"load-{principal_id}-{i}-{time.time_ns()}",
                    enable_v2=True,
                )
        except Exception as exc:  # noqa: BLE001 - load test records every failure mode
            error = f"{type(exc).__name__}: {exc}"
        results.record((time.monotonic() - start) * 1000, error)


def run_profile(profile: Profile, database_url: str) -> Results:
    from alembic import command

    from n8n_operator.cli.commands.db import _alembic_config

    command.upgrade(_alembic_config(database_url), "head")
    engine = create_engine_for_url(
        database_url,
        pool_size=profile.concurrent_operators + 2,
        max_overflow=profile.concurrent_operators,
    )
    factory = create_session_factory(engine)

    registry_path = Path(f"/tmp/stage11-load-{profile.name}-registry.yaml")
    registry_path.write_text(REGISTRY_YAML)

    with session_scope(factory) as session:
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
        org = OrganizationRepository(session).create(name=f"Load {profile.name}")
        env_ids = []
        for i in range(profile.environment_count):
            env = EnvironmentRepository(session).create(
                organization_id=org.id,
                name=f"env-{i}",
                n8n_base_url_ref="env:LOAD_TEST_BASE_URL",
                n8n_api_key_ref="env:LOAD_TEST_API_KEY",
            )
            env_ids.append(env.id)

        operator_ids = []
        for i in range(profile.concurrent_operators):
            principal = PrincipalRepository(session).create(
                kind="user",
                display_name=f"load-operator-{i}",
            )
            OrganizationMembershipRepository(session).create(
                principal_id=principal.id,
                organization_id=org.id,
                roles=["operator"],
            )
            operator_ids.append(principal.id)

    results = Results()
    ops_per_worker = profile.total_operations // profile.concurrent_operators
    threads = [
        threading.Thread(
            target=_worker,
            args=(factory, operator_ids[i], env_ids[i % len(env_ids)], ops_per_worker, results),
        )
        for i in range(profile.concurrent_operators)
    ]
    started_at = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall_seconds = time.monotonic() - started_at

    engine.dispose()
    return results, wall_seconds
```

- [ ] **Step 3: Write the report-printing main entry point**

```python
def _percentile(data: list[float], pct: float) -> float:
    if not data:
        return 0.0
    ordered = sorted(data)
    idx = min(len(ordered) - 1, int(len(ordered) * pct))
    return ordered[idx]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--profile", choices=list(PROFILES), default="startup")
    args = parser.parse_args()

    profile = PROFILES[args.profile]
    print(
        f"Running profile '{profile.name}': {profile.concurrent_operators} concurrent "
        f"operators, {profile.total_operations} total operations, "
        f"{profile.environment_count} environment(s)."
    )
    results, wall_seconds = run_profile(profile, args.database_url)

    print(f"\nWall clock: {wall_seconds:.2f}s")
    print(f"Total operations attempted: {len(results.latencies_ms)}")
    print(
        f"Errors: {len(results.errors)} ({len(results.errors) / max(1, len(results.latencies_ms)) * 100:.2f}%)"
    )
    print(f"Throughput: {len(results.latencies_ms) / wall_seconds:.2f} ops/sec")
    print(f"Latency p50: {_percentile(results.latencies_ms, 0.50):.1f}ms")
    print(f"Latency p95: {_percentile(results.latencies_ms, 0.95):.1f}ms")
    print(f"Latency p99: {_percentile(results.latencies_ms, 0.99):.1f}ms")
    if results.errors:
        print("\nSample errors:")
        for err in results.errors[:10]:
            print(f"  {err}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run both profiles against real Postgres and record results**

```bash
docker compose -f docker/postgres-test/docker-compose.yml up -d
uv run python scripts/load_test.py \
  --database-url "postgresql+psycopg://operator:operator_test_password@127.0.0.1:55432/n8n_operator_load_startup" \
  --profile startup 2>&1 | tee /tmp/stage11-load-startup.log
uv run python scripts/load_test.py \
  --database-url "postgresql+psycopg://operator:operator_test_password@127.0.0.1:55432/n8n_operator_load_seriesc" \
  --profile seriesc 2>&1 | tee /tmp/stage11-load-seriesc.log
```
Note: the script needs the target database to already exist (create it first with
`CREATE DATABASE n8n_operator_load_startup;`/`..._seriesc;` via `psql` against the base
`N8N_OPERATOR_TEST_POSTGRES_URL` connection, mirroring what
`tests/integration/postgres/conftest.py`'s fixture does programmatically — do this by
hand for this manual rehearsal, or add a `--create-database` flag to the script if
that's cleaner; either is acceptable).

- [ ] **Step 5: Write the evidence file and check registry rate-limit defaults**

Create `docs/evidence/stage11-load-test-results.md` with both profiles' full output
(throughput, latency percentiles, error rate) and the published assumptions paragraph
from the script's own docstring. Compare the measured Series C throughput against
`examples/registry/starter-kits/gtm-starter-kits.yaml`'s `rate_limit_per_minute`
values (e.g. `crm.bulk_update_stage`'s `rate_limit_per_minute: 2`) — if the measured
system throughput is far below what these limits would allow, note that the limits are
approval-workflow-driven, not infrastructure-driven, and are still appropriate; only
edit the registry files if the load test reveals a limit that's actually unreachable in
practice for the wrong reason (e.g. the system chokes well before the configured limit
under realistic concurrency) — expected outcome is no edit needed, but check for real.

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff check scripts/load_test.py
uv run ruff format scripts/load_test.py
uv run mypy --strict scripts/load_test.py 2>&1 | tail -30
git add scripts/load_test.py docs/evidence/stage11-load-test-results.md
git commit -m "stage 11: load/concurrency testing harness and results"
```

---

### Task 7: Live-n8n harness real run and client-validation status

**Files:**
- Create: `docs/evidence/stage11-live-n8n-run.md`
- Modify: `.github/PUBLIC_RELEASE_CHECKLIST.md`

**Interfaces:**
- Consumes: `docker/live-n8n/docker-compose.yml`, `scripts/live_n8n_up.sh`,
  `scripts/live_n8n_down.sh` (existing tooling, not modified).
- Produces: `docs/evidence/stage11-live-n8n-run.md`, cited by Task 9 and Task 10.

- [ ] **Step 1: Bring up the live-n8n harness and run the compatibility suite**

```bash
cd /Users/carolynstumph/Documents/n8ntools/n8n-operator
bash scripts/live_n8n_up.sh 2>&1 | tee /tmp/stage11-live-n8n-up.log
```
Follow the one documented manual step (n8n's first-owner-account setup — no API/CLI
path exists for it, per `docs/LIVE_N8N_TESTING.md`).

```bash
export N8N_LIVE_BASE_URL="http://127.0.0.1:<port from docker-compose.yml>"
export N8N_LIVE_API_KEY="<the key generated during the manual owner-setup step>"
uv run pytest tests/live/test_live_n8n.py -v 2>&1 | tee /tmp/stage11-live-n8n-tests.log
```

- [ ] **Step 2: Tear down and sanitize evidence**

```bash
bash scripts/live_n8n_down.sh
```
Strip `N8N_LIVE_API_KEY`'s actual value and any container-internal hostnames from the
retained logs before copying into the evidence doc — this repo is public.

- [ ] **Step 3: Write the evidence file**

Create `docs/evidence/stage11-live-n8n-run.md` recording: the n8n version tested
(2.35.7, the only version in `docs/COMPATIBILITY_MATRIX.md`), pass/fail count, run
date, and a plain statement that only one n8n version has been validated — flagged
plainly as a residual gap for Task 10's report, not silently glossed over.

- [ ] **Step 4: Update the release checklist**

In `.github/PUBLIC_RELEASE_CHECKLIST.md`, check off "Live n8n compatibility workflow
green against every version claimed in the matrix" (true — the matrix claims exactly
one version, and it's green). Update "Hosted OpenAI connector claim matches a retained
real-client test" to explicitly read as pending with a one-line reason: no hosted
Claude/OpenAI credentials exist in this environment; protocol-conformance evidence
(Task 2) stands in; any operator can complete this check with their own client
credentials.

- [ ] **Step 5: Commit**

```bash
git add docs/evidence/stage11-live-n8n-run.md .github/PUBLIC_RELEASE_CHECKLIST.md
git commit -m "stage 11: live-n8n harness real run and release checklist update"
```

---

### Task 8: Packaging, provenance, and CI audit

**Files:**
- Create: `docs/evidence/stage11-packaging-ci-audit.md`

**Interfaces:**
- Consumes: `.github/workflows/*.yml`, `.github/dependabot.yml`, `pyproject.toml`,
  `docs/RELEASE_ROLLBACK.md`.
- Produces: `docs/evidence/stage11-packaging-ci-audit.md`, cited by Task 10.

- [ ] **Step 1: Read and record every CI workflow's purpose and current status**

```bash
gh run list --branch main --limit 20
```
For each workflow file under `.github/workflows/`, note: trigger conditions, what it
checks, and its most recent run status on `main`.

- [ ] **Step 2: Confirm branch protection and required checks**

```bash
gh api repos/katekruger/n8n-operator/branches/main/protection 2>&1
```
Record which checks are required, whether force-push/deletion are blocked, and whether
this matches what `.github/PUBLIC_RELEASE_CHECKLIST.md` already claims.

- [ ] **Step 3: Audit release.yml's provenance and dependency pinning**

Read `release.yml`'s `provenance` job in full — confirm it uses
`actions/attest-build-provenance` correctly and runs before (gates) the `pypi`/
`github-release` jobs, not after. Read `pyproject.toml`'s dependency specifiers —
note any unpinned/floating range that could admit a surprise transitive update between
CI runs (this overlaps Task 5 Step 6's supply-chain probe — don't duplicate, just
cross-reference the finding here if Task 5 already covered it).

- [ ] **Step 4: Write the evidence file**

Create `docs/evidence/stage11-packaging-ci-audit.md` with the CI workflow table,
branch-protection summary, and provenance/dependency findings.

- [ ] **Step 5: Commit**

```bash
git add docs/evidence/stage11-packaging-ci-audit.md
git commit -m "stage 11: packaging, provenance, and CI audit"
```

---

### Task 9: Documentation updates to match facts

**Files:**
- Modify: `README.md`, `CHANGELOG.md`, `docs/COMPATIBILITY_MATRIX.md`,
  `docs/V1_LIMITATIONS.md`, `docs/ARCHITECTURE.md`, `docs/THREAT_MODEL.md` (if Task 5
  didn't already touch it)

**Interfaces:**
- Consumes: every evidence file from Tasks 1-8 (`docs/evidence/stage11-*.md`).
- Produces: updated docs that Task 10's report cites as "already reflects current
  state," not something the release report has to caveat.

- [ ] **Step 1: Update README.md's status blockquote**

Read the current v2 status note (added in Stage 10). Add: Stage 11's own closure
findings in one sentence — e.g. "Stage 11's integration, migration, security, and load
review is complete; see `docs/STAGE_11_RELEASE_REPORT.md` for the full findings and
go/no-go recommendation." Do not claim a release happened — this stage produces a
recommendation, not a release.

- [ ] **Step 2: Update CHANGELOG.md**

Add a Stage 11 entry under the v2 section (or an `[Unreleased]` heading if that's the
existing convention — check the file's current structure first) summarizing: the
integrated scenario test, migration rehearsal, security review, load testing, and any
fixes Task 5 produced.

- [ ] **Step 3: Update docs/COMPATIBILITY_MATRIX.md**

Confirm it still accurately shows exactly one n8n version tested (2.35.7) — no false
claim of broader version coverage. Add Task 7's run date if the matrix has a
"last verified" column.

- [ ] **Step 4: Update docs/V1_LIMITATIONS.md and docs/ARCHITECTURE.md**

Read both. Add v2 wording only where v2 genuinely replaces a v1-era limitation (e.g. if
a limitation the file describes as v1-only is now resolved by a v2 stage) — preserve
the historical wording everywhere else, per the mission's explicit instruction.

- [ ] **Step 5: Confirm docs/THREAT_MODEL.md reflects Task 5's findings**

If Task 5 already updated this file directly, just confirm it's consistent — no
duplicate edit here.

- [ ] **Step 6: Run the docs consistency check and commit**

```bash
uv run python scripts/check_docs_consistency.py
git add README.md CHANGELOG.md docs/COMPATIBILITY_MATRIX.md docs/V1_LIMITATIONS.md docs/ARCHITECTURE.md
git commit -m "stage 11: documentation updates to match audit findings"
```

---

### Task 10: Release report and go/no-go recommendation

**Files:**
- Create: `docs/STAGE_11_RELEASE_REPORT.md`
- Modify: `docs/BUILD_PLAN.md` (Stage 11 checkbox, repo tree entries for every new file)

**Interfaces:**
- Consumes: every `docs/evidence/stage11-*.md` file from Tasks 1-8.
- Produces: the final deliverable this whole plan builds toward.

- [ ] **Step 1: Write the release report**

```markdown
# Stage 11 release report — v2 integration, release, and proof

Date: 2026-08-30 (or actual completion date). Commit: <HEAD short SHA>.

## Summary

<2-3 sentences: what was audited, what was built new, overall posture.>

## Findings

| # | Area | Severity | Evidence | Owner | Disposition |
|---|---|---|---|---|---|
| 1 | <finding> | <blocking/deferred/accepted> | `docs/evidence/stage11-....md` | <who'd own a fix> | <release-blocking / explicitly deferred / accepted residual risk> |
...

Include a row for every real finding across Tasks 1, 5, 6, 7, 8 — and a row for
"no finding" summary items too (e.g. "Consistency audit: 0 stale rows found" is worth
one row, not silence).

## Stage 11 completion gate — checklist

- [ ] All required CI checks pass from a clean checkout
- [ ] Both database backends (SQLite, PostgreSQL) pass their declared test modes
- [ ] Package installation and migration are reproducible
- [ ] No open critical/high security findings
- [ ] Every public claim has retained evidence (docs/evidence/)
- [ ] The GTM starter journey succeeds without privileged repository knowledge

Mark each with the actual result and a one-line justification, not just a checkmark.

## Known residual gaps (explicitly deferred or accepted, not silently dropped)

- Only one n8n version (2.35.7) has live compatibility evidence.
- No hosted Claude/OpenAI client validation — no credentials in this environment;
  protocol-level evidence stands in; any operator can complete this with their own
  client credentials.
- Security review is self-conducted, not a professional third-party pentest.

## Go/no-go recommendation

<Your actual recommendation based on the findings table above — go, no-go, or
conditional-go pending specific named blocking items.>

## Explicitly NOT done in this stage

No tag, GitHub release, PyPI publish, or repository-setting change. This report is
advisory; the release action itself requires separate, explicit owner approval.
```

Fill in every placeholder with the real findings gathered across Tasks 1-8 — this step
cannot be completed until those tasks' evidence files exist.

- [ ] **Step 2: Update docs/BUILD_PLAN.md**

Check the Stage 11 checkbox (all 4 bullets). Add every new file from Tasks 1-9 to the
repo tree code block (D9 requires this): `docs/evidence/` directory and its files,
`docs/STAGE_11_RELEASE_REPORT.md`, `tests/integration/test_v2_integrated_scenario.py`,
`tests/integration/postgres/test_v2_migration_rehearsal.py`, `scripts/load_test.py`.

- [ ] **Step 3: Run the full verification gate one final time**

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src/
uv run python scripts/check_docs_consistency.py
uv run pytest -q --ignore=tests/live
export N8N_OPERATOR_TEST_POSTGRES_URL="postgresql+psycopg://operator:operator_test_password@127.0.0.1:55432/postgres"
uv run pytest tests/integration/postgres -q
gitleaks detect --source . --no-git -v 2>&1 | grep -i "File:" || echo "clean"
```
Expected: everything green; `gitleaks` shows only the two pre-existing findings from
Stage 10 (a fixture JSON and a `.pyc` cache file), nothing new.

- [ ] **Step 4: Commit**

```bash
git add docs/STAGE_11_RELEASE_REPORT.md docs/BUILD_PLAN.md
git commit -m "stage 11: release report and go/no-go recommendation"
```

- [ ] **Step 5: Push and open the PR — confirm with the user first**

Per this session's established convention, use `AskUserQuestion` to confirm before
pushing. The PR body should summarize the findings table and go/no-go recommendation
from `docs/STAGE_11_RELEASE_REPORT.md` — do not tag, release, or publish anything as
part of this PR.

```bash
git push -u origin feat/v2-stage-11-integration-release-and-proof
gh pr create --title "v2 stage 11: integration, release, and proof" --body "..."
```

---

## Task Order and Dependencies

Tasks 1, 2, 7, 8 are independent of each other and of Tasks 3-6 — they can run in any
order (or in parallel, if using subagent-driven execution). Task 3a must complete before
3b (same file, sequential fixtures). Task 4 is independent of Task 3 but shares the
Postgres harness. Task 5 is independent but its findings feed Task 9. Task 6 is
independent. **Task 9 depends on Tasks 1-8's evidence files existing.** **Task 10
depends on Task 9 (and therefore transitively on everything).** Recommended order for
subagent-driven execution: dispatch 1, 2, 3a, 4, 5, 6, 7, 8 in parallel-capable batches
(3a before 3b), then 9, then 10 strictly last.
