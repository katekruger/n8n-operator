# Instructions for agents working on this repository

This file is for an agent modifying **this repo**. It is not documentation for
someone using the product — that is [README.md](README.md) and [docs/](docs/).
`CLAUDE.md` is a one-line pointer (`@AGENTS.md`) to this file, not a second copy. If
you find yourself editing `CLAUDE.md` to explain something, put the explanation here
instead.

## What this is

n8n Operator is a governed MCP control plane: it sits between an MCP client (Claude,
ChatGPT, or any compatible client) and a real n8n instance, so a model can discover,
validate, and request execution of a small, human-reviewed set of workflows — never an
arbitrary one — with every write gated behind an out-of-band human approval and a
tamper-evident audit trail. `docs/BUILD_PLAN.md` is normative for the state machine,
the registry schema, the MCP tool contracts, and the acceptance criteria; read the
relevant section before touching anything it governs. `docs/ARCHITECTURE.md`
(components, layering) and `docs/THREAT_MODEL.md` (what every control defends
against) are the next two documents worth reading before a non-trivial change.

## Non-negotiables — the approval gate above everything else

**No code path may ever let the entity that requested an operation also be the one
that approves it.** This is the entire reason the product exists. Concretely, no
change may:

- Add an MCP tool, or a code path reachable from one, that can decide an approval.
  Approval only ever happens through `n8n-operator operations approve`/`reject` (CLI,
  a human at a terminal) or a `POST /approve/{token}`/`POST /reject/{token}` from a
  browser with a valid session — never a tool call an agent can make on its own
  (`src/n8n_operator/approval/routes.py`). `GET /approve/{token}` renders a decision
  page and grants nothing; only `POST` can change state, and it independently
  re-verifies the token server-side rather than trusting anything the form claims.
- Weaken the approval token's guarantees: 256-bit, single-use, TTL-bounded, stored
  only as a `sha256` hash (never the raw value), never logged (access logging is
  disabled entirely on the approval app for this reason — `approval/app.py`), and
  never echoed back in a response body or header beyond the URL path the caller
  already has.
- Remove or bypass the approval app's Host-header check, Origin check on
  state-changing POSTs, or the CSRF cookie comparison (`hmac.compare_digest`, not
  `==`) in `approval/routes.py` — these are the DNS-rebinding, CSRF, and clickjacking
  defenses named in `docs/THREAT_MODEL.md` (T-08, T-15, T-16, T-34).
- Make the approval app bindable to anything but loopback (`127.0.0.1`) in v1.
  `config.Settings._validate_approval_bind_is_loopback` enforces this upstream —
  `approval/app.py` never even sees a non-loopback bind. This is boundary B10; do not
  add a flag that relaxes it.
- Add automatic retry of an indeterminate dispatch. If an outcome can't be confirmed,
  the operation becomes `UNKNOWN` and stays `UNKNOWN` — no code path in this repo
  moves it anywhere else (ADR-005). A human resolves it explicitly
  (`docs/RECONCILING_UNKNOWN.md`) or a *fresh*, re-authorized `retry_operation` call
  mints a new operation (never mutates the stale one).
- Accept a raw n8n workflow ID, URL, or payload in any MCP tool argument, or return a
  credential, token, n8n-internal ID, or instance URL in any tool result. Response
  shapes are closed allowlists (`WorkflowSummary`, `WorkflowDetail`, etc.) — these
  fields don't exist on the types a tool call can return, so there's no redaction
  step to remember or forget.
- Expose a workflow that isn't in the registry, however live it is on the n8n
  instance. The registry (`registry/`) is a default-deny allowlist (ADR-002); nothing
  executes without an entry a human wrote.

If a change touches any of the above, it touches a security boundary — update
`docs/THREAT_MODEL.md` too, and never mark a threat `mitigated` without a real, tested
control behind it. The phase-9 release audit (and, more recently, Stage 11's security
review — see `docs/STAGE_11_RELEASE_REPORT.md`) both found and corrected threat-model
entries that had drifted from what was actually implemented; don't reintroduce that
gap.

## Commands

Requires Python 3.12 (`requires-python = ">=3.12,<3.13"`) and
[uv](https://docs.astral.sh/uv/). Every command below was re-run from this checkout
while writing this file.

```bash
uv sync --all-extras --dev      # setup — installs postgres/keyring extras too
uv run ruff check .             # lint
uv run ruff format --check .    # format check
uv run mypy                     # strict mode (pyproject.toml: strict = true), no carve-out
uv run python scripts/check_docs_consistency.py   # doc/BUILD_PLAN/tree cross-checks
uv run pytest -m "not live_n8n" # full suite except the real-n8n-only layer
```

Verified: `ruff check` — all checks passed; `ruff format --check` — 264 files already
formatted; `mypy` — no issues in 199 source files; `check_docs_consistency.py` — OK,
222 tree entries verified; `pytest -m "not live_n8n"` — **1472 passed, 44 skipped, 8
deselected, 1 xfailed** (the one `xfail` is a real, tracked, still-open security
finding pinned by a strict test — see [Testing conventions](#testing-conventions) —
not a broken test).

These five commands are exactly what `.github/workflows/ci.yml`'s `check` job runs —
run them locally before opening a PR, all five must be clean.

**Coverage** — CI's separate `coverage` job enforces `--fail-under=90` against the
whole `src/n8n_operator` package (not a `core/`/`registry/`-only carve-out — see
[Divergences from README/CONTRIBUTING](#divergences-found-between-docs-and-reality)):

```bash
uv run pytest --cov --cov-report=term-missing
```

**Needs Docker running locally, opt-in via env var, not part of the default suite
above:**

```bash
# Postgres-marked tests — spin up docker/postgres-test/, then:
export N8N_OPERATOR_TEST_POSTGRES_URL=postgresql+psycopg://...   # your local instance
uv run pytest -m postgres

# Keycloak-marked tests — spin up docker/keycloak-test/, then:
export N8N_OPERATOR_TEST_KEYCLOAK_URL=http://localhost:...
uv run pytest -m keycloak
```

**Needs a real, credentialed n8n instance — not run by the commands above, not a
required CI check, entirely optional for a contributor without one:**

```bash
bash scripts/live_n8n_up.sh    # docker/live-n8n/, pinned to n8n 2.35.7
uv run pytest -m live_n8n
bash scripts/live_n8n_down.sh
```

`.github/workflows/live-n8n.yml` triggers on `workflow_dispatch` only (never on push
or PR) and needs `N8N_LIVE_BASE_URL`/`N8N_LIVE_WORKFLOW_ID`/`N8N_LIVE_WEBHOOK_PATH`
plus the `N8N_LIVE_API_KEY` secret, gated behind a GitHub `environment` a maintainer
controls. It is **not** in the branch-protection required-checks list — a PR from a
contributor with no n8n instance merges without this ever running.

## Layout

| Path | What it is |
|---|---|
| `src/n8n_operator/core/` | The domain layer. Every use case (`prepare_operation`, `approve_operation`, `execute_operation`, ~60 more) lives in `service.py`; `state_machine.py` is the 12-state/15-transition table; `authorization.py` is RBAC evaluation (ADR-015); `handles.py` mints/verifies single-use operation handles (ADR-003); `models.py` is every Pydantic v2 domain type. Every adapter (`cli/`, `mcp/`, `approval/`) calls exclusively into `core/service.py` — never into `storage/`, `n8n/`, or the other capability packages directly. |
| `src/n8n_operator/registry/` | The default-deny workflow allowlist. `schema.py` (structural Pydantic models), `loader.py` (`load_registry` — parses, validates rules R1-R12, canonicalizes, hashes; all-or-nothing, never touches storage), `validation.py` (JSON Schema 2020-12 validation of caller arguments). |
| `src/n8n_operator/storage/` | `models.py` (SQLAlchemy 2.0 typed ORM), `repository.py` (per-table repository classes — no state-machine logic here), `session.py` (engine/session lifecycle), `migrations/` (Alembic — see below). |
| `src/n8n_operator/approval/` | The product. See [Non-negotiables](#non-negotiables--the-approval-gate-above-everything-else). |
| `src/n8n_operator/audit/` + `src/n8n_operator/audit_anchor/` | See [Audit and anchoring](#audit-and-anchoring-what-it-actually-guarantees). |
| `src/n8n_operator/n8n/` | The only vendor boundary for n8n's REST API — `client.py` exposes named methods against an explicit allowlist of endpoints, no generic "call this path" escape hatch. No retry logic anywhere (a contract test asserts its absence, ADR-005/009); a transport failure becomes `InstanceUnreachableError` or an indeterminate `DispatchOutcome`, never silently treated as "the workflow didn't run." |
| `src/n8n_operator/identity/` | Vendor boundary for OIDC only (`oidc.py`: JWT/JWKS validation — signature, issuer, audience, expiry, clock skew). Never touches storage. |
| `src/n8n_operator/core/identity.py` | The orchestration `identity/` and `storage/` are each forbidden from doing themselves (capability packages don't import each other or `core/`). Turns a validated `(iss, sub)` into an Operator principal, JIT provisioning, `whoami`, environment/CLI-principal resolution. Don't confuse this with `identity/` above — different package, different job. |
| `src/n8n_operator/mcp/` | `server.py` is the composition root (wires `n8n/` adapters into `core.service`'s Ports); `tools.py` hand-constructs all 20 `Tool` objects with `extra="forbid"` schemas (not the SDK's decorator, deliberately — see the module docstring); `transports.py` implements stdio and Streamable HTTP. |
| `src/n8n_operator/notifications/` | `base.py` declares `NotificationEventLike`/`DeliveryOutcome` structurally (re-declared, not imported from `core.models` — same no-cross-capability-imports rule); `local.py` is a dev-only logging sink; `webhook.py` is the production HTTPS sink, no internal retry (bounded retry lives in `core.service.retry_failed_notifications`). |
| `src/n8n_operator/cli/` | Typer app, `cli/main.py`. Ten command groups (`db`, `registry`, `serve`, `operations`, `audit`, `identity`, `environment`, `notifications`, `metrics`, `anchor`) plus one bare `health` command. Every command opens a session via a local `_connected()` helper and calls straight into `core.service` — a command must never reimplement domain logic. |

**Where a new thing goes**: pure domain logic → `core/service.py` (with a
`state_machine.py` entry if it's a new transition). A new vendor integration → its own
capability package at the top level of `src/n8n_operator/`, following the existing
`n8n/`/`identity/`/`notifications/` shape (structural types only, no import of `core/`
or another capability package). A new MCP tool → `mcp/tools.py`'s hand-built schema
style, wired through a new or existing `core/service.py` function, never calling
`storage/`/`n8n/` directly. A new CLI command → the matching `cli/commands/*.py` file,
calling `core.service` the same way every other command does.

## Migrations

Alembic, configured in `alembic.ini` (`script_location =
src/n8n_operator/storage/migrations`, `sqlalchemy.url` deliberately omitted — supplied
at runtime by `env.py` from `N8N_OPERATOR_DATABASE_URL`). Seven migration files exist,
`0001` through `0007`, named `NNNN_slug.py` (four-digit zero-padded sequence,
`file_template = %(rev)s_%(slug)s`).

**Any change to `storage/models.py` needs a migration.** Every existing migration is
hand-written with an extensive docstring explaining the *why*, but is explicitly
verified against autogenerate producing an empty diff before being trusted
(`docs/BUILD_PLAN.md` documents this as AC-24) — write or generate the migration, then
confirm Alembic's `compare_metadata`/autogenerate sees no further difference between
`storage/models.py` and the migrated schema, on both SQLite and PostgreSQL. There is
no separate how-to doc for this beyond BUILD_PLAN's own AC-24 references and the CLI:

```bash
uv run n8n-operator db status    # current revision, whether at head
uv run n8n-operator db migrate   # bring an existing DB to head
uv run n8n-operator db init      # fresh DB: create + migrate to head + seed
```

Skipping this produces a repo that passes the test suite (which runs against
freshly-migrated scratch databases) and fails on any real deploy running the actual
migration path — the review checklist above (`check_docs_consistency.py`, the
`postgres`-marked suite) does not catch a missing migration on its own; a human
reviewer checking `storage/models.py` against `migrations/versions/` does.

## Testing conventions

27,000+ lines of tests, organized by what they need, via pytest markers declared in
`pyproject.toml`: `unit` (pure logic, no I/O), `property` (Hypothesis invariants,
`hypothesis>=6.165`), `contract` (MCP tool schema, error taxonomy, and
cross-capability layering rules — e.g. a test asserting `n8n/client.py` has no retry
logic, or that `audit/` never imports `storage/`), `integration` (real SQLite plus a
mock n8n transport — `tests/integration/mock_n8n.py`, never a live instance),
`live_n8n`/`postgres`/`keycloak` (the three opt-in, credentialed layers above). A new
test should declare the narrowest marker that's true and follow the naming/fixture
pattern of its nearest existing sibling in the same directory — this codebase is
consistent about it (e.g. every Postgres-marked test builds its own scratch database
via the shared `postgres_test_db_url` fixture in `tests/integration/conftest.py`,
never a shared one).

**`xfail(strict=True)` is a real pattern here, not a way to silence a broken test.**
`tests/integration/test_audit_workflow_branch_actor_scope.py` currently `xfail`s on
purpose: it pins a real, documented, tracked security finding (see
`docs/evidence/stage11-security-review-addendum.md`) that hasn't been fixed yet — the
moment someone patches the underlying query without also flipping the marker, the
suite starts failing loudly rather than silently going stale. If you ever see a new
`xfail(strict=True)` in this codebase, look for the tracking doc it should cite before
assuming it's dead weight.

`live_n8n`-marked tests are the one layer meaningfully different from the rest: they
run against `docker/live-n8n/`'s real n8n 2.35.7 instance instead of
`tests/integration/mock_n8n.py`'s fake transport, are never run in CI, and are the
only place this repo actually exercises the n8n REST API over the network rather than
against a mock.

## Release process

Currently `1.0.0rc3` (`pyproject.toml`, `src/n8n_operator/__init__.py`, and the
`v1.0.0rc3` git tag all agree — verified). This is deliberate, not stalled: the
project is holding at release-candidate status until two named, tracked things land,
both stated in README.md's own status block and `docs/RELEASE_ROLLBACK.md`:

1. A real hosted OpenAI connector call against a public TLS endpoint (the protocol
   shape is already automated in CI — `tests/integration/test_mcp_http_openai_compat.py`
   — but an actual hosted request has never been made).
2. A PyPI "trusted publisher" registered for this repo — a human, PyPI-account-holder
   action; `.github/workflows/release.yml`'s `pypi` job is `if: false` until then.

**v2 is now layered on top of that v1 status.** v2 (organizations, RBAC, environments,
team approvals, governed retry, structural diffs, metrics/audit query, alert hooks,
external audit anchoring) is merged, stage-by-stage tested
(`docs/V2_TRACEABILITY.md`), and Stage 11's integration/security/release review is
complete — see `docs/STAGE_11_RELEASE_REPORT.md` for the full findings. Its own
recommendation is **"conditional go — release candidate, not final GA"**: a real,
confirmed cross-organization audit-log leak was found and fixed (adversarially
re-verified), and one narrower, related finding is left open, explicitly named as a
blocker for a future GA release, not for the RC. Read that report before assuming v2's
security posture is fully closed.

`docs/RELEASE_ROLLBACK.md` documents rollback (GitHub Release deletion) and yank
(PyPI, once publishing starts, which can never delete an uploaded file — only yank it)
procedures — read it before ever cutting a release, not after something goes wrong.

## What this repo deliberately does not do

- **No automatic retry of anything**, ever, in any version. `UNKNOWN` is a permanent,
  human-resolved terminal-ish state (see [Non-negotiables](#non-negotiables--the-approval-gate-above-everything-else)).
- **No editing of n8n workflow definitions**, v1 or v2. Authoring stays in the n8n UI;
  this repo reads definitions (to diff/hash them) and dispatches their configured
  trigger — nothing writes back to a workflow's own logic.
- **No generic n8n API passthrough.** `n8n/client.py` exposes only named methods
  against a fixed endpoint allowlist; there is no "call this arbitrary path" escape
  hatch anywhere, by design (ADR-006's server-owned-credential model depends on this).
- **No multi-tenancy or authentication in v1** — v1 has exactly one principal
  (`"local"`) and one n8n instance; this is `docs/V1_LIMITATIONS.md`'s first, most
  fundamental entry, not an oversight. v2 adds OIDC, multiple principals, and multiple
  environments — but see the release-process note above about v2's own remaining
  security work before treating it as fully hardened.
- **No cross-capability imports.** `audit/`, `audit_anchor/`, `n8n/`, `identity/`,
  `notifications/`, `registry/` never import each other or `core/` — each is typed
  against small structural Protocols it declares itself, even when that means
  duplicating a few lines (e.g. `audit/chain.py` reimplements `registry/loader.py`'s
  canonical-JSON recipe rather than importing it). A contract test in `tests/contract/`
  enforces this; don't work around it with a "just this once" import.
- **No crash-recovery for a stranded `EXECUTING` operation** in v1. If the process
  dies between committing `EXECUTING` and the dispatch call completing, that operation
  stays `EXECUTING` forever — no sweep, no automatic `UNKNOWN`, no automatic anything
  (T-37, `docs/V1_LIMITATIONS.md`). Don't assume this is handled elsewhere before
  building on top of it.

## Audit and anchoring: what it actually guarantees

`audit/chain.py` hash-chains every audit entry (`entry_hash` over the entry's own
fields plus the previous entry's `entry_hash`; genesis is 64 zeros) — this alone is
only tamper-**evidence** inside the database: an attacker with database write access
could, in principle, recompute the entire chain to hide an edit, since nothing outside
the database holds a copy.

`audit_anchor/` (ADR-012) is what closes that gap, and only that gap: `LocalFileAnchor`
(an Ed25519-signed, `fcntl`-locked, append-only file, signing key held outside the
database) and `HttpsWebhookAnchor` (an authenticated POST to an external endpoint) each
publish a **content-free** anchor — `{covers_through_seq, entry_hash, entry_count,
anchored_at, signature, public_key}`, never any actual audit content, enforced
structurally (the anchor type has no field to leak one from). The guarantee, in the
`local_file.py` module docstring's own words, is protection against **"an attacker who
edits the database content but does not also hold this file and its signing key."**

What it does **not** guarantee, and what a future change could silently break:

- **Anchoring isn't automatic.** `anchor publish` is an explicit CLI action (or
  whatever cron/schedule an operator sets up around it) — if nobody runs it regularly,
  the chain accrues entries with no external checkpoint pinning them, and the
  protection above simply doesn't apply to anything published since the last run.
- **It only covers up to `covers_through_seq` at the last publish.** Anything appended
  after that is unprotected until the next publish.
- **It assumes the signing key and the anchor file/webhook target are actually kept
  separate from the database** an attacker might compromise. If both live on the same
  host with the same access controls, the guarantee collapses to nothing.
- A schema change to `audit_log` that alters what `entry_canonical_bytes` hashes
  (`audit/chain.py`) without updating both the hash-chain computation and anything
  that verifies historical anchors would silently break verification of every anchor
  published before the change — there's no version field in the anchor's own payload
  guarding against this today.

## ADRs live in two places — a known, unresolved duplication

`docs/adr/` holds 19 existing architectural decision records (`ADR-NNN-slug.md`, a
homegrown format: `Status`/`Date`/`Deciders`/`Phase`/`Related` metadata bullets, then
`Context` → `Decision` → `Consequences` → `Alternatives considered`) — this is this
repo's own established convention, referenced throughout `docs/BUILD_PLAN.md` and
`CONTRIBUTING.md`. `docs/decisions/` is a second, newer location using MADR 4.0.0
format (YAML frontmatter, `Context and Problem Statement` → `Considered Options` →
`Decision Outcome` → `Consequences`), added to match a portfolio-wide convention used
by sibling repos. Two records exist there so far, both deliberately narrower than and
cross-referencing an existing `docs/adr/` entry rather than duplicating it — see
`0001-token-link-approval-not-an-authenticated-web-session.md` (extends ADR-010) and
`0002-external-anchoring-guarantee-is-manual-and-narrow.md` (extends ADR-012).

**This is a real inconsistency, not a resolved one.** A new architectural decision
today has two plausible homes with different formats and no stated rule for which one
to use. Until this repo picks one convention and migrates (or explicitly decides to
keep both, with a stated reason), default to `docs/decisions/` for a genuinely new
decision and use `docs/adr/`'s format only when directly extending or superseding an
existing entry there.

## Divergences found between docs and reality

Found while writing this file — reported here rather than silently corrected in
place, per instruction:

1. **README.md's "Decision records" table lists ADR-001 through ADR-012 (12 rows)**,
   but `docs/adr/` on disk contains 19 files, ADR-001 through ADR-019. The seven
   missing rows (ADR-013 through ADR-019) are the v2-era decisions — organization/
   tenant model, OIDC trust, RBAC evaluation, environment overlays, team-approval
   quorum, notification/alert delivery, metrics cardinality — and are actively cited
   elsewhere in the codebase (e.g. `core/authorization.py` cites ADR-015).
2. **`CONTRIBUTING.md` line 76** says "see `docs/adr/` for the existing twelve and
   their format" — same staleness; should read nineteen.
3. **`CONTRIBUTING.md` lines 53-54** claim coverage is "gated on
   `src/n8n_operator/core/` and `src/n8n_operator/registry/` — at least 90% line
   coverage." The actual CI gate (`.github/workflows/ci.yml`'s `coverage` job,
   mirrored in `release.yml`) runs `--fail-under=90` against the **whole**
   `src/n8n_operator` package — `pyproject.toml`'s `[tool.coverage.run]` sets
   `source = ["src/n8n_operator"]` with no module-scoped carve-out anywhere. The
   *that there's a 90% gate* claim is correct; the *scope* claim is not.
4. **`examples/mcp-clients/README.md`** states (twice, lines 12 and 100) that its
   verification sessions confirm "the documented 12-tool/2-resource surface." The
   actual registered MCP surface is 20 tools (12 v1 + 8 v2, confirmed by counting
   `mcp/tools.py`'s registrations) — this file reflects only the v1 baseline and
   wasn't updated when the 8 v2 tools were added, even though README.md's own
   Documentation table elsewhere correctly says `docs/MCP_CLIENT_RECIPES.md` covers
   "the literal tool-call JSON for every step... using only the 20-tool surface."

None of these were fixed as part of writing this file — reporting them, per
instruction, rather than quietly writing the corrected version into `AGENTS.md` and
leaving the source docs stale.
