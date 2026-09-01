# Reachability and live-versus-replay report

Point-in-time findings, not a convention — see [AGENTS.md](../AGENTS.md) for how to
work in this repo day to day. This document answers two questions the portfolio's own
recurring-defect pattern (well-tested code nothing calls; a live path and a
reconstruction path that quietly disagree after a restart) raised for this repo
specifically. Both investigations are report-only: nothing was wired, deleted, moved,
or fixed as a result of what's written here.

Run 2026-09-01, against `main` at the commit this document itself lands on. Two prior,
lighter passes on this same repo (merged in earlier PRs) reached a directionally
similar conclusion using in-process coverage; this report supersedes them with genuine
subprocess-based evidence and a real cross-process reproduction, per this round's own
explicit methodological requirement — an in-process `sys.modules` census proves
nothing here, since a Python process already importing the CLI/MCP framework has
imported half the package tree before a single real command runs.

## Part 1 — module reachability, genuine subprocess evidence

### Method

Every entry point was driven as a real, separate OS process — `.venv/bin/n8n-operator
<command>` invoked as `python -m coverage run --rcfile=<scratch>/.coveragerc <bin>
<args>` per command (one fresh process per invocation, `coverage.py` in `parallel =
true` mode so each subprocess writes its own data file, combined afterward), and
`n8n-operator serve stdio` launched as the literal child process
`mcp.client.stdio.stdio_client` spawns — confirmed via `ps aux` mid-run to be a genuine
forked `coverage run ... n8n-operator serve stdio` process, not an in-process import.
The whole sweep ran twice end to end, fresh scratch SQLite database each time: once
with `N8N_OPERATOR_ENABLE_V2` unset (12 MCP tools registered, confirmed), once with it
set to `true` (20 tools registered, confirmed).

**CLI commands exercised** (~49 per mode): every command group's `--help`, plus real
invocations against the scratch database — `db init/status/migrate`; `registry
validate/list/hash/reload`; `identity bootstrap/list-orgs/add-membership/
list-memberships/preview-permissions/create-service-principal/list-service-principals/
disable-principal/enable-principal/remove-membership`; `environment create/list/
show-safe/health/registry-diff/validate-overlay/reload-overlay/archive`; `operations
list`; `audit list/verify/export`; `notifications retry-failed/check-alerts`; `metrics
show`; `anchor init-key/publish/verify/status`; `health`.

**MCP session, both modes**: a real `mcp` `ClientSession` drove `initialize`,
`list_tools`, `list_resources`, `list_resource_templates`, `read_resource` on both
registered URIs, and every registered tool — all 12 in v1 mode
(`list_workflows`, `describe_workflow`, `get_instance_health`, `validate_input`,
`preflight_workflow`, `prepare_operation`, `get_operation`, `execute_operation`,
`cancel_operation`, `list_operations`, `get_execution_result`, `get_execution_log`),
all 20 in v2 mode (the 12 above plus `diff_workflow_definition`, `get_metrics`,
`list_audit_events`, `whoami`, `list_environments`, `request_approval`,
`get_approval_status`, `retry_operation`).

**Skipped, and why**: live n8n instance success paths (the configured base URL points
at an unbound loopback port, so only the `INSTANCE_UNREACHABLE` branch runs); real
OIDC IdP verification; real PostgreSQL migration; real webhook delivery. Each needs an
external dependency this investigation didn't stand up — noted per-module below, not
silently absorbed into the coverage numbers.

### Result

**Combined coverage** (parallel data files combined per mode, `coverage report -m`
against 7131 statements): **v1-mode 57%** (3049 missed), **v2-mode 61%** (2747
missed).

**Unreached in both modes — exactly one file:**

| Module | v1 | v2 | Why |
|---|---|---|---|
| `src/n8n_operator/__main__.py` | 0% (0/7) | 0% (0/7) | The known `python -m n8n_operator.cli.main` trap — no `__main__` guard. The installed `n8n-operator` console script (`pyproject.toml`'s `[project.scripts]`) is what every real deployment invokes, and it bypasses this file entirely; neither the CLI sweep nor the `serve stdio` subprocess spawn goes through it. |

This confirms both prior sessions' claim, now on genuine subprocess evidence rather
than in-process corroboration.

**Reached only under v2 (expected, not a surprise — RBAC, environment scoping,
identity/OIDC-adjacent resolution, and the 8 v2-only tool handlers)**:

| Module | v1 | v2 |
|---|---|---|
| `core/authorization.py` | 46% | 79% |
| `core/identity.py` | 50% | 90% |
| `core/service.py` | 34% | 43% |
| `logging_setup.py` | 66% | 90% |
| `mcp/server.py` | 48% | 68% |
| `mcp/tools.py` | 59% | 77% |
| `storage/repository.py` | 65% | 69% |

Smaller bumps in `config.py`, `registry/schema.py`, `n8n/client.py`. No module crossed
from 0% to nonzero between modes — every module already had some coverage under v1
via `--help`/import paths; v2 mode exercises materially more of each one's actual
logic.

**Everything else under the eleven subpackages** (`approval`, `audit`, `audit_anchor`,
`cli`, `core`, `identity`, `mcp`, `n8n`, `notifications`, `registry`, `storage`) has
nonzero real-usage coverage in both modes. Modules held down by a genuine external
dependency, not a reachability gap:

| Module | Needs |
|---|---|
| `identity/oidc.py` | A live OIDC IdP — only config-resolution paths run without one. |
| `core/postgres_migration.py`, `storage/postgres_migration.py` | A live PostgreSQL instance for the migrate-to-postgres path. |
| `notifications/webhook.py`, `audit_anchor/webhook.py` | A live webhook receiver for delivery/retry paths. |

### Proposed test shape — not implemented

A `tests/test_reachability.py`, matching the shape `deliverability-guard` already
ships: spawn the real console script via `subprocess.run([sys.executable, "-m",
"coverage", "run", ...])` for a fixed CLI sweep plus one `serve stdio` MCP session
(the same driver shape this investigation used), combine coverage, then assert every
module under `src/n8n_operator` has `Cover% > 0` unless its module path appears in a
hardcoded `EXEMPTIONS: dict[str, str]` — and, separately, assert every key in
`EXEMPTIONS` still names a file that exists on disk, so a fixed trap or a deleted
module fails the test loudly instead of the exemption list silently going stale.

Exemptions proposed for this repo, one per row, each with the reason a passing test
would need to keep proving:

```python
EXEMPTIONS: dict[str, str] = {
    "src/n8n_operator/__main__.py": (
        "known python -m n8n_operator.cli.main entry-point trap; unused in "
        "production, the installed n8n-operator console script bypasses it entirely"
    ),
    "src/n8n_operator/identity/oidc.py": "requires a live OIDC IdP",
    "src/n8n_operator/core/postgres_migration.py": "requires a live PostgreSQL instance",
    "src/n8n_operator/storage/postgres_migration.py": "requires a live PostgreSQL instance",
    "src/n8n_operator/notifications/webhook.py": "requires a live webhook receiver",
    "src/n8n_operator/audit_anchor/webhook.py": "requires a live webhook receiver",
}
```

No other module needs a full-file exemption — everything else already has nonzero
coverage from this sweep and, if this repo wants a stricter gate later, is a candidate
for a percentage floor rather than an outright exemption.

## Part 2 — live-versus-replay: does a restart change any answer?

### 1. Does any test rebuild persisted state from disk between assertions?

Yes, and this is the dominant shape in `tests/integration/`, not a rare pattern.
**145 test functions** (re-derived by AST-parsing every `test_*` function across 55
files in `tests/integration/*.py` and counting literal `session_scope(...)` calls per
function body — 582 total call sites project-wide) use two or more separate
`session_scope` blocks in one function. `tests/conftest.py`'s `sqlite_url` fixture is
deliberately file-backed, never `:memory:`, specifically because "WAL mode and
cross-connection visibility both need a real file" — so a test with two blocks is a
genuine disk round-trip through a fresh `Session` object, not Python-object reuse.

Clearest example, `tests/integration/test_repository.py:158-172`:

```python
def test_operation_create_and_get(
    session_factory: sessionmaker[Session], seed: dict[str, Any]
) -> None:
    with session_scope(session_factory) as session:
        _make_operation(session, seed)

    with session_scope(session_factory) as session:
        op = OperationRepository(session).get("op_test1")
        assert op is not None
        assert op.state == "PREPARING"
```

The first block commits and closes; the second opens a brand-new `Session` against the
same file and asserts against what it reads back — a real round-trip.

### 2. Could a token be re-usable after a restart? A genuine cross-process reproduction was run — no.

`storage/models.py`'s `Approval` table encodes consumption directly on the row
(`decision`, `decided_by`, `decided_at`), never in a separate log a restart could
desync from. `resolve_approval_token` (`core/service.py`) raises
`ApprovalTokenAlreadyUsedError` the instant `approval_row.decision is not None` —
compare-and-set safety comes from `OperationRepository.get_for_update`'s `SELECT ...
FOR UPDATE` row lock, held for the rest of the transaction.

A real reproduction was run, not just reasoned about: **Process A** opened a
file-backed SQLite database, ran `prepare_operation` → `resolve_approval_token` →
`approve_operation` through the real service layer, printed the raw token, and exited
completely. A **second, fully separate `python` process** — no shared memory, only the
SQLite file and the token string — then attempted to resolve the same token:

```
PREPARED op_id=op_01M1EWE2KXX2GG5B5PVRSG7WAY state=PENDING_APPROVAL
APPROVED op_id=op_01M1EWE2KXX2GG5B5PVRSG7WAY state=APPROVED
TOKEN=[REDACTED — a real 256-bit token from a throwaway scratch database, deleted
       after this reproduction ran; the value itself isn't the finding]
--- now launching a FRESH python process (process B) to attempt reuse ---
RESULT=CORRECTLY-REJECTED-ApprovalTokenAlreadyUsedError
```

Same reproduction for TTL expiry — a token minted with `approval_ttl_seconds=1`, left
to expire with **no sweeper process running anywhere**, then a fresh process attempting
to approve it after the deadline:

```
PREPARED op_id=op_01M1EWETVV6PR9JBC7GG36XYF7 state=PENDING_APPROVAL ttl=1s
TOKEN=[REDACTED — same as above, a real token from the same now-deleted scratch database]
sleeping 2s past the 1s TTL, no sweeper process running anywhere...
RESULT=CORRECTLY-REJECTED-ApprovalNotPendingError details={'current_state': 'EXPIRED'}
```

Both correctly rejected across a genuine process boundary. No divergence found.

### 3. Could an approval state be reset by a restart via a replay path?

There is no replay path to disagree with the live one — confirmed by searching, not
assumed. The only "replay" concept anywhere in `src/` is
`core/idempotency.py`'s `IdempotencyResolution.REPLAY`, which is `prepare_operation`'s
idempotency-key dedup (same fingerprint → return the existing operation, mint no new
token) — unrelated to approval decisions, and its own comment states it deliberately
never reconstructs anything. No code path anywhere replays the audit log or any event
sequence to derive `Approval`/`Operation` state; every reader
(`resolve_approval_token`, `get_approval_decision_context`, `_apply_lazy_expiry`) reads
the row's own current columns directly. **State reset by restart is structurally
impossible here**: there is nothing that reconstructs state from anything other than
the row's own current values, and a restart cannot alter those.

### 4. Does the audit anchoring guarantee hold across a restart?

Yes. [ADR-021](adr/ADR-021-external-anchoring-guarantee-is-manual-and-narrow.md)
documents anchoring as manual (an explicit CLI action, never scheduled) and narrow
(protects only against database-only tampering) — that narrowness is a property of
what anchoring *covers*, not of whether the coverage computation itself is
restart-safe, and it is. `audit_anchor/local_file.py`'s `publish` and `verify_file`
both read anchor-coverage state fresh from the anchor file under `fcntl.flock` on
every call — no cached state carried across calls. The value being anchored comes from
direct current-row reads (`AuditLogRepository.get_last()`,
`AuditAnchorRepository.get_latest()`), never a replay of anything. Since `anchor
publish` is already a fresh CLI process per invocation, a restart changes nothing a
continuing process would have computed differently — every input to the computation is
re-read from disk/DB state a restart cannot alter.

### Equivalence-testing proposal

`deliverability-guard`'s exact shape — generate event-sequence permutations, apply via
the live path, compare against a separate replay-from-log reconstruction — **does not
transplant here**, because this repo has no second, replay-based path to compare
against (confirmed in Part 2 §3): there is exactly one way approval/operation state is
ever computed, a direct row read, so "live vs. replay" collapses to "live vs. live."

The meaningful adaptation for this codebase: generate permutations of `{prepare,
approve, reject, expire (via TTL), second-decision-attempt, cancel}` for one
operation's lifecycle, apply each sequence through `core.service` exactly as today's
tests do, but after **every** step close the session/engine and open a **fresh**
`sessionmaker`/`Session` — or, for the strongest version, a genuinely separate
subprocess against the same SQLite file, as this report's own reproduction does —
before the next read or assertion. Compare that against the existing same-process
assertion shape already used throughout `tests/integration/`. A second, orthogonal
axis worth testing: two genuinely concurrent fresh processes racing
`approve_operation`/`reject_operation` against the same operation, asserting exactly
one wins. Not implemented, per this round's own constraints.

## Bottom line

Both reports come back clean. **Reachability**: every module under
`src/n8n_operator/` is reached by a real subprocess in at least one mode except the
one already-known, already-explained `__main__.py` entry-point trap — not a hidden
gap, a documented one this report re-confirms with stronger evidence than either prior
pass. **Live-versus-replay**: no divergence exists, and for the highest-stakes case
(approval-token reuse and TTL expiry) that conclusion rests on an actual cross-process
reproduction, not just code reading — both correctly rejected reuse across a real
restart. This is a real result: for this repo, the recurring portfolio defect these
two checks exist to catch is not present. The proposed reachability-guard test and the
adapted equivalence-testing shape above are the concrete next steps if this repo wants
that absence enforced going forward rather than merely reported once.
