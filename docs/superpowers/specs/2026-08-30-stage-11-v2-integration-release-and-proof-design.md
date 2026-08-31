# Stage 11 — v2 integration, release, and proof (design)

Status: approved by user, 2026-08-30. Implementation via `writing-plans`.

## Context

Stages 00–10 (organizations, RBAC, environments, team approvals, governed retry,
structural diffs, metrics/audit query, alert hooks, external audit anchoring, GTM
starter kits) are all merged on `main`. `docs/V2_TRACEABILITY.md` (29 rows) is already
fully checked off with a real test and doc reference for every row. `docs/BUILD_PLAN.md`
Stage 11 itself is four bullets: full AC-01–AC-50 pass (AC-23's tool-count check becomes
20, not 12), the `live_n8n` pytest layer closing its v1-recorded-as-never-built gap
against v2's multi-environment surface, `V2_TRACEABILITY.md` fully checked off, and
repeating the phase-9 release process for the v2 tag.

Scoping research (2026-08-30) found:

- **Already mature, verification-only territory**: `docs/THREAT_MODEL.md` (STRIDE
  analysis through T-65, residual-risk register RR-1..RR-15), the Postgres migration
  test harness (`tests/integration/postgres/test_migration.py`, 404 lines covering
  empty/populated/duplicate/interrupted/refused-destination cases), the live-n8n Docker
  harness (`docker/live-n8n/`, scriptable except one manual n8n first-owner-account
  step), CI (`ci.yml`/`codeql.yml`/`secret-scan.yml`/`release.yml`/`live-n8n.yml`,
  `dependabot.yml`), and `release.yml`'s existing Sigstore-backed provenance job.
- **Genuine gaps**: no test scenario combines two organizations and three environments
  end to end in one run (existing coverage is pairwise/unit-level); no load or
  concurrency *throughput* harness exists (only correctness-under-concurrency tests);
  the compatibility matrix has exactly one n8n version tested (2.35.7); no hosted
  Claude/OpenAI credentials exist in this environment, and none will be added — this
  claim stays explicitly pending, backed by existing protocol-level evidence
  (`scripts/mcp_session_smoke.py`, `tests/integration/test_mcp_http_openai_compat.py`);
  `.github/PUBLIC_RELEASE_CHECKLIST.md` has two explicitly unchecked items that map
  directly onto this stage's own gaps.
- **Resolved with the user**: no new credential-intake feature in n8n-operator itself —
  n8n-operator's MCP server never holds an Anthropic/OpenAI key; that's the *client's*
  own credential, unrelated to n8n-operator's server-side `env:`/`keyring:`-indirected
  n8n credentials (ADR-006). Load-test scale uses my own published, stated assumptions
  (startup/Series C profiles below) rather than user-supplied numbers. Security review
  is a rigorous self-conducted audit — explicitly labeled as not a substitute for
  professional pentesting, since no external pentest budget exists here.

## Design

### 1. Mechanized consistency audit — verification pass

Re-walk every row of `V2_TRACEABILITY.md` against current code/tests; confirm nothing
drifted silently since being marked `done`. Run `scripts/check_docs_consistency.py`
(already mechanizes D1–D13: states, transitions, tool counts, AC/invariant/boundary/rule
ranges, ADR structure, doc-link resolution, repo-tree-vs-filesystem). Extend it only if
a genuinely missing check surfaces during the audit — not expected to need much, given
its existing depth. No new file expected unless a gap is found.

### 2. Tool-count and protocol-session verification — verification pass

Run `scripts/mcp_session_smoke.py` and `tests/integration/test_mcp_http_openai_compat.py`
for real against a freshly built wheel; confirm 20 tools enumerate in v2 mode and 12 in
v1-compatibility mode (AC-23). Retain session transcripts as evidence
(`docs/evidence/` — new directory, sanitized, referenced from the release report).

### 3. Integrated two-organization, three-environment scenario — new test

New `tests/integration/test_v2_integrated_scenario.py`, run against real Postgres. Two
organizations (cross-org isolation as a first-class assertion, not just pairwise
coverage); three environments (staging, production, and a second production-like
environment, so environment-scope narrowing exercises more than the two-environment
minimum every other test uses). One scenario walking, in order: OIDC-authenticated
bootstrap in both orgs → RBAC grants scoped per org/environment → `prepare_operation`
through quorum approval → governed retry off a `FAILED` operation → reconciliation of a
forced `UNKNOWN` → `diff_workflow_definition` detecting live drift → `get_metrics`/
`list_audit_events` scoped correctly per organization (an org-A caller never sees org-B
totals) → an alert hook firing on a simulated threshold breach → both `AuditAnchor`
implementations (local file, HTTPS webhook mock) publishing and verifying the same
chain. This is the one artifact that actually proves integration, as distinct from every
prior stage's own isolated coverage.

### 4. PostgreSQL migration rehearsal — real run, not new code

Seed a realistic SQLite v1 dataset (operations across every terminal state, approvals,
an audit chain with several anchored segments) using the existing fixtures/factories.
Run `n8n-operator db migrate-to-postgres` for real; verify row counts, principal/identity
mapping, audit-chain integrity (`anchor verify`), and that historical operations remain
readable (`operations show` on pre-migration IDs) on the Postgres side. Then rehearse
rollback: restore from the pre-migration SQLite backup and confirm the v1 database is
unaffected by the attempted migration. Document the rollback procedure concretely in
`docs/POSTGRES_OPERATIONS.md` if it isn't already walked step by step there — this is
the one new *procedure*, not new code, this section produces.

### 5. Security review — self-conducted, explicitly labeled

Re-verify every `THREAT_MODEL.md` entry (T-01..T-65, RR-1..RR-15) against current v2
code — confirm each `mitigated` status still holds, each `accepted` residual risk is
still deliberately accepted (not silently worse). Then specifically probe, beyond what's
already documented: SSRF via `n8n_base_url_ref`/webhook config resolving to an internal
address; approval-token forgery beyond T-57's existing per-approver binding coverage;
webhook delivery (notification + anchor) for SSRF/replay; metrics-privacy edge cases
beyond ADR-019's sample-size floor; supply-chain config (dependency pinning strategy,
`provenance` job correctness, Dependabot coverage). Any real finding gets a negative
test seeded in the same PR, not deferred to a follow-up. Findings and non-findings both
get a row in the release report (Section 10) — a clean pass on a probed area is evidence
worth recording, not just a silent absence.

Labeled plainly in the release report as an internal, self-conducted review — not a
substitute for professional third-party penetration testing.

### 6. Load and concurrency testing — new lightweight harness

New `scripts/load_test.py` — no external dependency (no locust/k6), plain
`asyncio`/`threading` driving either the CLI or a local Streamable HTTP MCP session,
matching this repo's existing zero-heavyweight-tooling convention. Two published
profiles, my own stated assumptions:

- **Startup**: ~5 concurrent operators, ~50 operations/day, one environment.
- **Series C**: ~50 concurrent operators, ~5,000 operations/day, 3 environments, a
  meaningful fraction of operations requiring quorum approval.

Measures p50/p95/p99 latency and error rate per profile against real Postgres. Publishes
exact assumptions (hardware, DB configuration, loopback-only network) alongside results
so nothing reads as an internet-scale claim. Results feed back into whether the example
registries' `rate_limit_per_minute` defaults are realistic — corrected in
`examples/registry/` if the load test shows they aren't.

### 7. Live-n8n and client validation — real run + explicitly-pending claim

Re-run `docker/live-n8n/` for real against n8n 2.35.7 (the only version in
`docs/COMPATIBILITY_MATRIX.md`); retain sanitized evidence under `docs/evidence/`,
matching stage 09/10's own rehearsal-evidence pattern. No hosted Claude/OpenAI
credentials exist in this environment and none will be added (resolved with the user).
The release report states the hosted-client claim as **pending**, backed by the
protocol-level evidence from Section 2, with a one-line note that any operator can
complete this check later using their own client credentials — the same credentials
they'd already need to use the product at all. No new credential-handling feature is
built anywhere in n8n-operator itself.

### 8. Packaging, provenance, and CI review — audit, not rebuild

Audit `release.yml`'s verify → provenance → publish chain, `dependabot.yml` coverage,
`codeql.yml`, and branch-protection settings (already largely covered by
`.github/PUBLIC_RELEASE_CHECKLIST.md`). Close the checklist's two explicitly-unchecked
items: live-n8n workflow green (Section 7's real run satisfies this) and the
hosted-client claim (becomes explicitly pending per Section 7, not silently dropped from
the checklist).

### 9. Documentation updates — match facts, preserve history

README, `CHANGELOG.md`, `docs/COMPATIBILITY_MATRIX.md`, `docs/V1_LIMITATIONS.md`,
`docs/ARCHITECTURE.md`, `docs/THREAT_MODEL.md` updated to reflect what this stage
actually finds. v2 wording added only where v2 genuinely changes something; v1-era
wording preserved as history rather than deleted, per the mission's own instruction.

### 10. Release report and go/no-go — advisory only

New `docs/STAGE_11_RELEASE_REPORT.md` (user's choice: a durable doc, not just a PR body).
Findings-first: every item from Sections 1–9 gets a row with severity, evidence
(file/test/run link), owner, and disposition (release-blocking / explicitly deferred /
accepted residual risk). Ends with a clear go/no-go recommendation and the Stage 11
completion-gate checklist restated with pass/fail against each item.

**No tag, GitHub release, PyPI publish, or repository-setting change happens in this
stage without the user's explicit, separate approval** — this stage's PR is the audit
itself and whatever fixes it drives, never the release action.

## Testing

Every new test (Section 3's integrated scenario, any negative tests from Section 5, the
load harness's own smoke-correctness in Section 6) runs in CI-equivalent form: SQLite
where applicable, real Postgres 16 (Docker) for anything Postgres-specific, `ruff`/
`mypy --strict` clean, `scripts/check_docs_consistency.py` clean. The live-n8n and load
harnesses are real, manually-run rehearsals (matching stage 09/10 precedent) with
retained evidence — not something CI runs on every push.

## Out of scope

- Actually tagging, releasing, or publishing anything.
- Any new n8n-operator product feature (including any credential-intake mechanism for
  LLM provider API keys — resolved explicitly with the user as out of scope).
- Testing n8n versions beyond 2.35.7 (no second instance available to test against;
  flagged in the release report as a residual gap, not silently ignored).
- External/professional penetration testing (no budget available; the self-conducted
  review is explicitly labeled as not a substitute).
