# n8n Compatibility — Empirical Findings

> This document is the evidence record ADR-008 requires before any field joins the
> definition-canonicalization exclusion allowlist, and the evidence record ADR-009
> requires before Operator claims anything about correlation or credential validity. It
> is not exploratory: every claim below is backed by a request/response pair against a
> real, running n8n instance, and every sanitized fixture referenced here lives under
> `tests/fixtures/canonicalization/`.
>
> **Scope.** One n8n version (2.35.7), one seed workflow, one request per candidate
> field. This is a first pass, not a version matrix — see [§9](#9-limitations-and-what-a-follow-up-pass-should-add).
> Nothing here contradicts ADR-008 or ADR-009; both are confirmed by what follows.
> New, version-specific implementation facts not anticipated by either ADR are recorded
> and folded into `n8n/canonicalization.py` and `n8n/preflight.py` directly.

## 1. Test environment

**Docker was not available** on the machine this spike ran on (checked `docker`,
`docker-compose`, `podman`, `colima`, `lima` — none installed). With the user's
explicit approval, n8n ran standalone via Node.js instead of in a container — the same
isolation guarantee (a fresh, local-only instance used for nothing but this spike, never
production), a different mechanism for getting there:

- n8n requires Node 20–22; the system Node was v25, which fails to build n8n's native
  `isolated-vm` dependency against a newer V8 API. Node 22.23.2 LTS was installed via
  `nvm` specifically for this spike.
- n8n was installed locally (`npm install n8n` inside a scratch directory outside the
  repository, never committed) and started with `N8N_USER_FOLDER` pointed at an isolated
  data directory, `N8N_LISTEN_ADDRESS=127.0.0.1`, telemetry and version-notification
  environment variables disabled, and no networking beyond loopback.
- Every request in this document targets `http://127.0.0.1:5678`. Nothing was ever
  pointed at a production or shared instance.

**Tested n8n version: 2.35.7** (confirmed three ways: `n8n --version`, the settings UI
sidebar, and consistent behavior across the whole spike).

## 2. The synthetic workflow

Created via `POST /api/v1/workflows` (not the editor UI, for exact reproducibility) and
activated via `POST /api/v1/workflows/{id}/activate`:

```
Webhook (POST /spike-test)
  -> Validate Input (Set node: valid = typeof body.value === 'number', value = body.value)
    -> Route (IF: valid == true)
        true  -> Process (Code node: result = value * 2)
                   -> Respond Success (200, {"n8n_operator":{"execution_id": $execution.id}, "data":{"result": ...}})
        false -> Respond Error (intended 400, {"n8n_operator":{"execution_id": $execution.id}, "error": "invalid input"})
```

This covers every required element: a webhook trigger, input validation/transform, one
harmless processing node, `Respond to Webhook`, and an error path (the IF node's false
branch). One node-configuration bug was found and is noted, not fixed, since it does not
affect any compatibility finding: the `Respond Error` node's `responseCode: 400`
parameter did not change the actual HTTP status (still 200) — the correct parameter
path for this node's `typeVersion` (1.4) is nested under `options`, not top-level. Not
investigated further; it has no bearing on canonicalization, correlation, or preflight.

Full node/connection JSON: `tests/fixtures/canonicalization/position_before.json`
(the first clean read, before any mutation).

## 3. Retrieval

`GET /api/v1/workflows/{id}` (the n8n Public API, authenticated with `X-N8N-API-KEY`,
matching ADR-006's server-owned-credential model). This is the same call
`n8n/client.py`'s `get_workflow` makes.

## 4. Unchanged repeated reads

Three reads, several seconds apart, with no mutation between them:
`tests/fixtures/canonicalization/unchanged_read_a.json` /
`unchanged_read_b.json` — **zero differing leaf paths.** Reads are fully deterministic
at rest; nothing needs excluding just to make repeated reads agree.

## 5. The critical structural finding: `activeVersion` is not always present

n8n 2.35.7 has a **publish/version model** layered over the workflow row: alongside the
top-level `nodes`/`connections`/`settings` a workflow carries `versionId`,
`activeVersionId`, an `activeVersion` object (a full nested duplicate of the published
node/connection graph plus its own timestamps and `workflowPublishHistory`), and a
`versionCounter`. None of this is documented in BUILD_PLAN or either ADR, because
neither anticipated it.

**When a workflow is deactivated, `activeVersion` and `activeVersionId` become `null`
entirely** — not stale, not empty, absent. Confirmed directly:
`tests/fixtures/canonicalization/active_state_before.json` (active: `activeVersion` is a
full object) vs. `active_state_after.json` (inactive: `"activeVersion": null,
"activeVersionId": null`). The **top-level** `nodes`, `connections`, `name`, and
`settings` fields are present and correct in both.

**Consequence for the implementation:** canonicalization must read the **top-level**
`nodes`/`connections`/`settings` fields, never `activeVersion.*`. A workflow that
happens to be paused (a real, ordinary state — an operator disabling something
temporarily, or a preflight `WORKFLOW_INACTIVE` check firing) would otherwise make
`activeVersion` disappear and drift-checking would have nothing to hash. This is
recorded as the first entry in `n8n/canonicalization.py`'s field-source table, not left
as an implicit assumption.

A second consequence: `versionId`/`activeVersionId`/`versionCounter`/
`workflowPublishHistory` are n8n's own internal change-tracking, not Operator's
`definition_hash`. They are useful signal for nothing here (they bump on **any** save,
cosmetic or not — see §6) and must be excluded from the canonical form as
structurally-not-part-of-the-definition, the same way `id`, `createdAt`, `updatedAt`,
and `isArchived` are: administrative metadata about the *row*, not the *graph*.

## 6. Field-by-field comparison

Methodology per ADR-008's harness: one seed, one field changed in isolation, before/after
definitions captured, and — critically — **the live production webhook called before and
after** to observe actual behavior, not just infer it from the diff. Every fixture pair
below is `tests/fixtures/canonicalization/<name>_before.json` /
`<name>_after.json`.

| # | Change | Diff | Behavior before → after | Verdict |
|---|---|---|---|---|
| 1 | Node position only | `nodes[i].position` changes; `versionId`/`activeVersionId`/`versionCounter`/`workflowPublishHistory` also change (see below) | `{"value":10}` → `result:20` both times | **Cosmetic** |
| 2 | Workflow name | `name` changes; triggers a deactivate+reactivate pair in `workflowPublishHistory`; `versionId` **unchanged** | `{"value":5}` → `result:10` both times | **Cosmetic** |
| 3 | Pin data (`pinData`) | `pinData.<node>` populated; `versionId` **unchanged** | Live webhook with `{"value":7}` while `Webhook` node was pinned to `{"value":999}` → `result:14` (the **real** body, not the pinned one) | **Cosmetic** — proven inert for production webhook dispatch specifically (see caveat below) |
| 4 | Active state | `active` flips; `activeVersion` disappears entirely (§5) | N/A — this is a lifecycle flag, not graph content | **Excluded structurally**, not via CAN-02 (it is not part of the graph to begin with; `WORKFLOW_INACTIVE` is a separate preflight check) |
| 5 | Node parameter (`Code.jsCode`, `*2` → `*3`) | `nodes[i].parameters.jsCode` changes | `{"value":5}` → `result:10` then `result:15` | **Semantic** (matches CAN-05, not tested for exclusion — never excludable) |
| 6 | Connection topology (`Route` true-branch rewired to skip `Process`) | `connections.Route.main[0]` changes | `{"value":5}` → `data:{"result":...}` then `data:{}` (downstream field silently absent) | **Semantic** (CAN-05) |
| 7 | Credential binding (added `HTTP Call` node bound to a test credential) | `nodes[].credentials.<type>.{id,name}` appears | Not executed (unconnected node); binding presence alone is CAN-05-semantic by definition | **Semantic** (CAN-05) — see §8 for what's visible |
| 8 | Webhook path (`spike-test` → `spike-test-NEWPATH`) | `nodes[0].parameters.path` changes | Old path → `404`; new path → normal response | **Semantic** (CAN-05: trigger configuration) |
| 9 | Node **name** (not tested directly — see reasoning) | — | — | **Semantic by construction**: `connections` addresses nodes **by name**, not by `id` (confirmed directly — see `position_before.json`'s `connections` object, keyed by node name). Renaming a node without updating every connection referencing it breaks the graph. Node name is therefore load-bearing structure, not a label, and stays included without needing a separate behavioral test. |

**Pin-data caveat, stated precisely:** the finding is that pinned data has no effect on
a **production** webhook call (`/webhook/<path>`). n8n also has a separate **test**
webhook URL (`/webhook-test/<path>`) used by the editor's "Execute workflow" button,
where pin data is documented n8n behavior and *does* apply. Operator's `n8n/client.py`
only ever dispatches to the production URL (ADR-006, ADR-009 §6 — "preparation stays
coupled to live preflight" against the real instance, never the editor's test mode), so
the production-only finding is the one that matters here, and it is the one recorded.

## 7. Correlation: `$execution.id` through the Respond to Webhook envelope

**Confirmed working exactly as ADR-009 §2 specifies.** Both the success and error
branches return `{"n8n_operator": {"execution_id": "<n>"}, ...}`, and the ID matches a
real, independently-queryable execution:

```
$ curl -X POST http://127.0.0.1:5678/webhook/spike-test -d '{"value": 21}'
{"n8n_operator":{"execution_id":"1"},"data":{"result":42}}
```

`$execution.id` is available inside a `respondToWebhook` node's expression editor with
no special configuration — no correlation feature flag, no workflow setting. A registry
entry declaring `trigger.correlation: response_envelope` (BUILD_PLAN §6.3) is purely a
statement that the workflow's author included this in their `Respond to Webhook` body;
n8n imposes no obstacle to it existing.

## 8. What's queryable after dispatch

`GET /api/v1/executions?workflowId=<id>` lists recent executions:
`id`, `finished`, `mode`, `status` (`success`/`error`/...), `startedAt`, `stoppedAt`,
`retryOf`/`retrySuccessId` (both `null` in every execution observed — v1 never retries,
so Operator will never populate these, and their presence in n8n's own schema is not an
invitation to use them).

`GET /api/v1/executions/{id}?includeData=true` returns full per-node run data: status,
timing, and each node's **output**, including the `Webhook` node's own output — which is
the **raw inbound request**: headers, query, and body verbatim. Sanitized shape (payload
redacted, see the file's own note) at
`tests/fixtures/canonicalization/execution_detail_shape.json`.

**This is exactly the kind of data boundary B6 exists for.** Raw per-node execution data
must never reach a client unshaped: it can contain request headers (potentially bearing
upstream auth material the *caller* sent to the webhook, not Operator's own credential,
but still not Operator's to disclose), full argument payloads independent of the
workflow's own `output.redact` configuration, and internal n8n bookkeeping. `n8n/types.py`
models this response, and `n8n/client.py`'s execution-fetching methods return only the
fields `core/service.py`'s `record_execution_outcome` needs (status, timing, the node
carrying the final result) — never the full `runData` tree — so the redaction and
size-capping `core/redaction.py` already performs is the *only* place a result payload
is shaped, not a second, adapter-side copy of that policy.

## 9. Credential-binding visibility

Confirmed via `GET /api/v1/workflows/{id}` on a node with a bound credential:

```json
"credentials": {
  "httpBasicAuth": { "id": "<credential id>", "name": "<credential display name>" }
}
```

The credential's **secret data** (username, password, token — whatever the credential
type holds) is never present in a workflow read, a credential list
(`GET /api/v1/credentials`), or a credential-creation response. Confirmed by creating a
real `httpBasicAuth` credential and reading back every representation n8n offers: `id`,
`name`, `type`, and bookkeeping timestamps only.

This maps directly onto the language the task (and ADR-009 §4) requires:

- **BOUND** — a `credentials.<type>` entry is present on the node. Detectable with
  certainty from a single workflow read.
- **MISSING** — a node whose type requires a credential (per its node-type definition)
  has no `credentials.<type>` entry, or the referenced credential ID no longer resolves.
- **UNKNOWN / unverifiable** — whether the bound credential's secret is actually valid
  against the downstream service. n8n does expose `POST /credentials/{id}/test`
  ("Tests a credential by ID using the stored credential data"), which sounds like a
  path to a real `VALID` status — **it was tried, and it is not reliable enough to build
  on.** Calling it against a real, just-created `httpBasicAuth` credential returned
  `400 {"message":"Unrecognized node type: n8n-nodes-base.graphqlTool"}` — an internal
  error entirely unrelated to the credential under test. This is direct, empirical
  support for ADR-009 §4's position, not a workaround needed to reach it:
  `n8n/preflight.py` never calls this endpoint. `MISSING_NODE_CREDENTIALS` reports
  binding absence only; credential validity is always `unverifiable` /
  `CREDENTIAL_VALIDITY_UNVERIFIED`, exactly as ADR-009 specifies, and this is now a
  tested claim, not a cautious default.

## 10. Instance reachability and version

- `GET /healthz` — unauthenticated, `{"status":"ok"}`, `200`. Used for the
  `instance_reachable` preflight check; the one endpoint Operator calls without its API
  key.
- **No public API endpoint returns the running n8n release version.** The internal
  (session-cookie-authenticated, UI-only) `GET /rest/settings` does, but that requires a
  browser login Operator's server-owned-API-key credential model (ADR-006) does not have
  and should not acquire.
- `GET /api/v1/openapi.yml` is unauthenticated and its `info.version` field (`1.1.1` on
  this instance) is the **API surface version**, not the n8n release version — it moves
  when the public API's own shape changes, not on every n8n release. Recorded as the
  best available proxy; `n8n/preflight.py`'s `compatible_version` check treats a
  mismatch here as `warn`, never `fail` — this is not the precise signal ADR-009 would
  want for a hard gate, and claiming otherwise would overstate what's actually known.
- A bad or missing API key: `401 {"message":"unauthorized"}`. A nonexistent workflow ID:
  `404 {"message":"Not Found"}`. Both are unambiguous and stable — no case where a
  missing resource and an authorization failure are confusable.

## 11. Publish/version endpoints — explicitly out of scope for v1 dispatch

n8n 2.35.7 exposes `/workflows/{id}/publish`, `/unpublish`, `/{id}/{versionId}` (fetch a
specific historical version), and `/{id}/history`. None of these are called by
`n8n/client.py`: Operator reads the current live definition and dispatches to the live
webhook; it does not manage n8n's own publish lifecycle or pin execution to a specific
n8n-internal version. Noted here so a future contributor finds the endpoints
deliberately unused rather than missed.

## 12. Canonicalization allowlist established by this pass

Per ADR-008 CAN-02/CAN-03, each entry below is justified by the evidence in §5/§6 above,
scoped to exactly n8n **2.35.7** (a single version, not yet a range — see §9 limitations
below). This is the literal content of `n8n/canonicalization.py`'s `EXCLUSION_ALLOWLIST`.

| Field path | Kind | Evidence | n8n version evidence covers |
|---|---|---|---|
| `nodes[].position` | Proven cosmetic (CAN-02) | §6 row 1 | 2.35.7 |
| `pinData` (whole field) | Proven cosmetic (CAN-02), production-webhook-scoped | §6 row 3 | 2.35.7 |

Structurally excluded (never part of "the definition" — CAN-01 does not apply, since
these are not graph content in the first place): `id`, `name`, `active`, `isArchived`,
`createdAt`, `updatedAt`, `versionId`, `activeVersionId`, `activeVersion`,
`versionCounter`, `workflowPublishHistory`, `meta`, `staticData`, `tags`, `shared`.
`name` is here, not in the CAN-02 table above, because the argument for it is structural
(it is the row's label, not part of `nodes`/`connections`/`settings`) rather than a
behavioral-equivalence claim — though §6 row 2 also confirms it behaviorally, belt and
braces.

Everything else — every node's `type`, `typeVersion`, `parameters`, `credentials`,
`name` (the node's own name, load-bearing per §6 row 9), every `connections` entry, and
the whole `settings` object — is included, per CAN-01's default and CAN-05's permanent
inclusion list. No wildcard or regex entry exists (CAN-03).

## 13. Limitations and what a follow-up pass should add

- **One n8n version.** Every allowlist entry above is evidence-scoped to 2.35.7 only. A
  version range requires re-running this harness against the boundaries of whatever
  range Operator claims to support, per ADR-008's harness step 5.
- **One corpus item per field**, not a corpus. Each comparison used one representative
  input (`{"value": N}`) rather than a spread of edge cases. Sufficient to establish "at
  least one divergence" would have disqualified a field; not exhaustive proof of "no
  divergence under any input" for the fields that passed.
- **No credential type other than `httpBasicAuth` was tested** against
  `/credentials/{id}/test`. The finding that the endpoint is unreliable is real for this
  type; it is not extended here to a blanket claim about every credential type n8n
  supports — `n8n/preflight.py` simply never calls it for any type, which makes the
  per-type question moot for this codebase regardless.
- **No multi-node-position or bulk-cosmetic-edit scenario** was tested (e.g. an
  auto-layout pass moving every node at once). The single-node test is expected to
  generalize (position is position, however many nodes move), but it is worth an
  explicit note that this was not separately re-verified.
