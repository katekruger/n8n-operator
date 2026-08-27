# v1 Limitations

A plain-language index of what v1 deliberately does not do, and what it does but only
partially. Everything here is either a stated version boundary
([BUILD_PLAN.md](BUILD_PLAN.md) section 3) or a threat with a `partial`/`accepted`
status in [THREAT_MODEL.md](THREAT_MODEL.md) section 5 — this document exists so an
operator can find the practical consequence without cross-referencing a threat table.

Nothing here is a bug tracker. Each item is a known, considered v1 boundary; where a
fix exists, it is aimed at v2/v3 (BUILD_PLAN section 3) unless stated otherwise.

## Single user, single n8n instance

v1 has exactly one principal (`"local"`) and talks to exactly one n8n instance. There
is no authentication, no multi-tenancy, and nothing to spoof (T-14, accepted by
design). Running two operators against the same database is unsupported. v2 adds
OAuth/OIDC, multiple principals, and multiple n8n environments.

## No automatic retry, ever

A dispatch that cannot be confirmed becomes `UNKNOWN` and stays `UNKNOWN` forever — no
code path in v1 moves it anywhere else ([ADR-005](adr/ADR-005-no-automatic-retry-v1.md)).
Resolving it is a human task: see [Reconciling UNKNOWN operations](RECONCILING_UNKNOWN.md).

## A crash-stranded `EXECUTING` operation has no automated recovery

If the Operator process is killed in the narrow window between burning the handle
(committing `EXECUTING`) and the dispatch call completing, the operation is left in
`EXECUTING` permanently. v1 provides **no** automatic sweep and **no** CLI command that
moves it forward — it is not silently marked `SUCCEEDED`, and nothing retries it, but
nothing marks it `UNKNOWN` either (T-37, downgraded from `mitigated` to `partial` during
the phase 9 release audit — the original threat-model text overstated what was actually
implemented). Two practical consequences:

- The operation's `EXECUTING` state doesn't resolve on its own. `get_operation`/
  `operations show` will keep reporting `EXECUTING` indefinitely.
- Because `max_concurrent` counts `EXECUTING` operations, a stranded operation
  permanently occupies one concurrency slot for its workflow until resolved.

This window is one process between two adjacent statements — not an extended period —
so it is rare in practice, not a routine occurrence. When it happens, resolving it
requires a direct, careful database edit; see
[Reconciling UNKNOWN operations](RECONCILING_UNKNOWN.md#a-crash-stranded-executing-operation)
for the exact procedure. v2 is expected to add a supported reconciliation command so
this never requires touching the database by hand.

## Arguments are stored raw at rest

`operations.arguments` holds the caller's arguments **unredacted** in the database.
This is intentional, not an oversight: `execute_operation`'s argument-fingerprint
re-verification and the actual dispatch to n8n both need the real values, and a value
redacted at write time can never be un-redacted later for that check. Redaction
(`output.redact`) is applied only at read boundaries — `get_operation`,
`operations show`, and `audit export` all redact before they return anything — never
at rest. `execution_results` (the n8n response) *is* redacted before it is ever
written, since nothing downstream needs the raw value again.

Practical consequence: an operator (or anything) with direct read access to the
database sees caller-supplied arguments in the clear, including anything a workflow's
`output.redact` list would otherwise hide from a tool result. No credential is ever
written to the database (ADR-006) — this is about caller-supplied business data, not
secrets. Filesystem permissions on the database file are the only v1 control here
(T-36, accepted). v1 also adds no encryption at rest.

## Redaction is only as good as what the registry author configured

`output.redact` is a list of JSONPath expressions an operator writes per workflow. If a
field carrying PII isn't listed, it isn't redacted — Operator provides the mechanism,
not a guarantee of completeness (T-30). There is no default redaction heuristic in v1.

## Audit tampering is detectable, not preventable

`n8n-operator audit verify`/`audit export` (phase 8) reliably detect a row edited or
deleted from the hash chain and name the exact sequence number where verification
first fails (AC-22). Detection is the whole design goal in v1 — there is no external
anchor: an attacker with write access to the SQLite file can still rewrite the entire
chain undetected if they recompute every hash consistently. v2 adds `AuditAnchor`
(a signed local file, then an authenticated HTTPS webhook) to pin chain state somewhere
that access alone doesn't reach (T-35, [ADR-012](adr/ADR-012-governed-retry-and-audit-anchoring.md)).

## Rate limiting is workflow-scoped, not global

`rate_limit_per_minute` and `max_concurrent` are both per-workflow. A caller cannot
flood a single workflow past its configured limit, but nothing stops a caller from
issuing requests against many different workflows at once — there is no per-principal
or system-wide quota in v1 (T-11, partial). v2 adds per-principal quotas.

## A workflow's title and description are not verified against its behavior

`definition_hash` guarantees the workflow that runs is the workflow that was reviewed
— it says nothing about whether the review itself was accurate. Operator cannot infer
intent from a node graph; a benign-sounding title over a destructive graph is not
something any v1 control catches (T-28). The operator registering a workflow is the
only review gate. v3's evaluation lab is aimed at this.

## No exact-ID reconciliation without `trigger.correlation: response_envelope`

A workflow that doesn't opt into the response envelope gives Operator no execution ID
to reconcile against. `UNKNOWN` for such a workflow can only be resolved by checking
n8n directly and reasoning about timing — there is nothing exact to match against
(T-40). `preflight_workflow` reports this with `NO_EXECUTION_CORRELATION` (a
non-blocking `warn`) before approval, so the limitation is visible while it can still
be fixed by editing the workflow to add the envelope.

## Approval fatigue is a human problem, not a solved one

The CLI and the approval page both lead with risk, side-effect class, full arguments,
and drift status before asking for a decision (ADR-010). Neither can stop a human who
approves without reading (T-20). This is explicitly out of scope for a software
control in v1 (BUILD_PLAN section 9.5).

## Availability is not a design goal

Operator is not built for high availability. An n8n outage blocks *new* work rather
than queueing unverified work — preparation requires a successful live preflight — and
an Operator outage means nothing can be prepared, approved, or executed through it
until it's back. This is the intended failure direction (fail closed), not a gap.

## What is deliberately not tested

n8n's own behavior, MCP client conformance beyond the documented schemas, load and
performance characteristics, and browser-level testing of the approval page (its logic
is tested through the FastAPI test client, not a real browser) — BUILD_PLAN section
10.5.

## The `live_n8n` layer needs a one-time manual step, even with the reproducible harness

BUILD_PLAN section 10.1's fourth layer exists in `tests/live/` and is runnable locally
or through the manual `Live n8n compatibility` GitHub Actions workflow. `docker/live-n8n/`
+ `scripts/live_n8n_up.sh` fully automate standing up a pinned, isolated instance and
importing and activating the synthetic workflow — but n8n has no documented REST or CLI
path to create the first owner account or an API key; both are UI-only. One person has
to click through that once per instance before the suite can authenticate at all. See
[`LIVE_N8N_TESTING.md`](LIVE_N8N_TESTING.md) for the exact procedure.

The suite verifies instance health, authenticated workflow retrieval (with an exact
workflow-ID match), active status, deterministic definition hashing, webhook dispatch,
response-envelope correlation, exact execution retrieval, drift detection (both the
no-drift and the detected-drift case, against the real live definition), and clean,
typed failures for a wrong API key, a wrong workflow ID, a wrong webhook path, and an
unreachable instance. It is excluded from ordinary CI because the repository does not
provision or retain a credential-bearing n8n instance — a GitHub Actions run still needs
the `live-n8n` environment's secret populated manually, from an instance someone stood
up (locally, or however they choose) and completed that one UI step against.

Practical consequence: ordinary pull requests prove the full deterministic and mocked
contract but cannot prove a hosted n8n target is currently reachable or unchanged. A
release operator must run the manual live workflow and retain its successful run URL.

---

Full detail, severity, and adversary framing for every item above: [THREAT_MODEL.md](THREAT_MODEL.md).
