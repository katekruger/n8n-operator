# n8n Compatibility Matrix

Quick reference. The full empirical record — every request/response pair, sanitized
fixtures, and reasoning — is [N8N_COMPATIBILITY.md](N8N_COMPATIBILITY.md); this page
summarizes it and states what is and isn't covered at a glance.

## Tested version

| n8n version | Method | Result |
|---|---|---|
| **2.35.7** | Live spike against a real, local-only instance (phase 4) — every finding in [N8N_COMPATIBILITY.md](N8N_COMPATIBILITY.md) is a captured request/response pair, not a guess. | Fully compatible — every feature below verified working. |
| **2.35.7** | Repeatable Docker harness ([`LIVE_N8N_TESTING.md`](LIVE_N8N_TESTING.md)), 2026-08-28 — a real, freshly stood-up instance, `scripts/live_n8n_up.sh`, `uv run pytest -m live_n8n -v` | **8/8 passed.** Found and fixed two real compatibility bugs in the process — see below. |

**What the repeatable-harness run found that the original phase-4 spike didn't:**
n8n 2.35.7 returns `pinData`/`settings` as an explicit JSON `null` (not an omitted
key) for a workflow that's never had either set — `WorkflowDefinition`
(`src/n8n_operator/n8n/types.py`) now coerces `None` to `{}` for both. And a webhook
node's trigger is registered by the running n8n process using its `webhookId` field as
the real lookup key, not the declared `path` alone — a webhook node imported without
one (as `examples/registry/synthetic_test_workflow.json` originally was) never gets
its route registered, regardless of activation method. Full story:
[`LIVE_N8N_TESTING.md`](LIVE_N8N_TESTING.md).

**This is one version, tested once.** Everything below this line describes what was
empirically confirmed against 2.35.7. Operator uses only the stable, documented n8n
Public REST API (`/api/v1/...`) — no internal or UI-only endpoint — which is the basis
for expecting broader compatibility, but "expected" is not "verified." Treat any other
n8n version as untested until someone runs the phase-4 harness against it (see
[Extending this matrix](#extending-this-matrix)).

## Feature support (against 2.35.7)

| Feature | Status | Notes |
|---|---|---|
| Webhook trigger dispatch (`POST`/`GET`) | ✅ Verified | [N8N_COMPATIBILITY.md §3](N8N_COMPATIBILITY.md#3-retrieval), §7 |
| Response-envelope correlation (`trigger.correlation: response_envelope`) | ✅ Verified | Execution ID recovered via `$execution.id` in a `Respond to Webhook` node body — [§7](N8N_COMPATIBILITY.md#7-correlation-executionid-through-the-respond-to-webhook-envelope) |
| Workflow definition read (`GET /api/v1/workflows/{id}`) | ✅ Verified | Basis for `definition_hash` and drift detection |
| Definition-hash canonicalization | ✅ Verified, empty exclusion allowlist | [§12](N8N_COMPATIBILITY.md#12-canonicalization-allowlist-established-by-this-pass) — every candidate field tested was found behaviorally significant; nothing is excluded from the hash in v1 |
| Instance reachability (`GET /healthz`) | ✅ Verified | Unauthenticated; the one endpoint called without the API key |
| API version proxy (`GET /api/v1/openapi.yml`) | ✅ Verified, `warn`-only | Confirmed to be the *API surface* version, not the n8n release version — no endpoint exposes the latter without a UI session Operator does not acquire ([§10](N8N_COMPATIBILITY.md#10-instance-reachability-and-version)) |
| Credential binding visibility (`GET /api/v1/workflows/{id}`) | ✅ Verified | Bound/unbound only — a credential's secret data is never present in any read ([§9](N8N_COMPATIBILITY.md#9-credential-binding-visibility)) |
| Credential *validity* checking | ❌ Confirmed unreliable, not used | `POST /credentials/{id}/test` returned an unrelated internal error against a real credential — Operator never calls it; reports `unverifiable` always ([§9](N8N_COMPATIBILITY.md#9-credential-binding-visibility)) |
| Execution status/result read (`GET /api/v1/executions/{id}`) | ✅ Verified | Basis for `get_execution_result`/`get_execution_log` |
| Workflow publish/version management | Not used | Operator reads the live definition and dispatches to the live webhook; it does not manage n8n's publish lifecycle ([§11](N8N_COMPATIBILITY.md#11-publishversion-endpoints--explicitly-out-of-scope-for-v1-dispatch)) |
| Workflow editing/creation | Not used (by design) | Operator never writes a workflow definition, in any version (BUILD_PLAN section 3) |

## What this matrix does not cover

- **A version range.** One version was tested once. There is no confirmed lower or
  upper bound of n8n releases Operator works against.
- **Every credential type.** Only `httpBasicAuth` was tested against the (unreliable,
  unused) validity-check endpoint. Binding visibility is expected to generalize across
  credential types — n8n's own workflow-read shape doesn't vary by type — but this was
  not separately re-verified per type.
- **Multi-node or bulk-edit scenarios.** Canonicalization was verified one field at a
  time on a single node. Expected to generalize; not separately re-tested.
- **n8n Cloud-specific behavior**, if it differs from a self-hosted instance in any way
  relevant to the Public API surface Operator uses. The phase-4 spike ran against a
  self-hosted instance.
- **Any n8n Enterprise-only feature.** Nothing in the v1 scope touches one.

Full detail on all of the above: [N8N_COMPATIBILITY.md §13](N8N_COMPATIBILITY.md#13-limitations-and-what-a-follow-up-pass-should-add).

## Extending this matrix

Adding a version means re-running the same evidence-gathering pass ADR-008 requires
before any canonicalization exclusion, against the new version:

The repeatable smoke contract is documented in
[`LIVE_N8N_TESTING.md`](LIVE_N8N_TESTING.md). It covers the stable runtime path; the
broader field-by-field canonicalization pass below remains required for a new version.

1. Stand up the target n8n version, isolated: bump the image tag in
   [`docker/live-n8n/docker-compose.yml`](../docker/live-n8n/docker-compose.yml) and run
   `scripts/live_n8n_up.sh` (Docker required for this path), or stand one up any other
   way — Docker is not required in general; see
   [N8N_COMPATIBILITY.md §1](N8N_COMPATIBILITY.md#1-test-environment) for how the
   original phase-4 spike ran without it.
2. Recreate (or reuse) the synthetic test workflow:
   [`examples/registry/synthetic_test_workflow.json`](../examples/registry/synthetic_test_workflow.json).
3. Repeat the field-by-field comparison in
   [N8N_COMPATIBILITY.md §6](N8N_COMPATIBILITY.md#6-field-by-field-comparison) and the
   correlation/credential/reachability checks in §7–§10.
4. Add a row to the table above; if any finding differs from 2.35.7, add a dedicated
   section explaining the divergence and its consequence for `n8n/canonicalization.py`
   or `n8n/preflight.py`, the same way the original spike is written up.
5. Update `docs/adr/ADR-008-conservative-definition-canonicalization.md`'s exclusion
   allowlist only on new evidence — never on assumption that a newer version behaves
   the same as 2.35.7.
