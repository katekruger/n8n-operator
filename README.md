# n8n Operator

**A governed MCP control plane for discovering, validating, executing, and debugging
approved n8n workflows from Claude, ChatGPT/OpenAI, Codex, and compatible MCP clients.**

n8n is an excellent workflow engine and a poor agent surface. Pointing a model at a raw
n8n instance hands it an unbounded, unversioned, credential-bearing remote-execution
primitive. n8n Operator is the policy enforcement point that sits between MCP clients
and one or more n8n instances: it exposes a small, stable, well-typed tool surface and
refuses to do anything that is not explicitly approved in advance.

n8n executes. Operator governs.

---

## Status

**Phase 0 — architecture and bootstrap. No product functionality is implemented yet.**

What exists today in this repository is the normative design:

| Document | Contents |
|---|---|
| [`docs/BUILD_PLAN.md`](docs/BUILD_PLAN.md) | **Normative.** Product definition, version boundaries, operation state machine, registry schema, tool inventory, storage model, security boundaries, test strategy, acceptance criteria, phase checklist. |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Component map, layering rules, request flows, data/trust flow, configuration, processes. |
| [`docs/MCP_TOOLS.md`](docs/MCP_TOOLS.md) | **Normative for tool I/O.** Contracts for the 12 v1 tools, the v1 resources, and the error taxonomy. |
| [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) | Assets, trust boundaries, threats, mitigations, accepted residual risk. |

There is no installable package, no CLI, and no MCP server yet. Source directories
under `src/n8n_operator/` and `tests/` are empty scaffolding. Where `BUILD_PLAN.md`
section 4 describes files that do not exist yet, the document is describing the
intended structure, not the current state.

---

## Safety model

The design rests on six controls. All of them are specified; none of them are
implemented yet.

**1. Approved workflow registry (default deny).**
Only workflows an operator has explicitly registered in a YAML registry are visible or
runnable. A workflow that is live on the n8n instance but absent from the registry does
not exist as far as any client is concerned. The registry carries human-authored
titles, descriptions, risk classifications, and input schemas.

**2. Typed input contracts.**
Every registered workflow declares a JSON Schema for its arguments. Caller-supplied
arguments are validated against it *before* anything reaches n8n, and failures return
structured, model-actionable errors with JSON-Pointer paths.

**3. Prepare → approve → execute lifecycle.**
Running a workflow is not a single call. `prepare` resolves the workflow, validates
arguments, preflights the live instance (reachable, active, definition unchanged since
registration), and produces an operation. Approval happens **out of band** — a human
acts on a loopback-bound browser page, never through an MCP tool a compromised client
could call. `execute` then requires a single-use, server-issued operation handle bound
to the exact principal, workflow, and argument fingerprint.

**4. Idempotency.**
Operations are keyed by `(principal, idempotency_key)` with an argument fingerprint. A
retried client call returns the existing operation rather than creating a second one,
and a key reused with different arguments is a conflict error. Handles are burned via a
compare-and-set, so an approved operation executes at most once. v1 never retries
automatically; ambiguous outcomes surface as `UNKNOWN` for a human to reconcile.

**5. Audit logs.**
Every state transition and every decision is an append-only, hash-chained audit record.
A transition, its event row, and its audit row commit in one transaction — if the audit
write fails, the transition did not happen. The chain is tamper-*evident*, not
tamper-proof.

**6. Server-owned n8n credentials.**
n8n API keys and webhook secrets are owned by the server, resolved at startup, held in
memory, and scrubbed from logs. They are never returned by any tool, never placed in
the registry file, and never sent to a client. Neither are the n8n instance URL nor the
underlying `n8n_workflow_id` — clients only ever see registry IDs.

There will never be a generic `n8n_request` passthrough tool, in any version.

---

## Planned versions

**v1 — local-first governed operator.**
A single operator points an MCP client at their own n8n instance and safely runs a
curated set of workflows, with every side effect gated by an explicit human approval
and recorded in a tamper-evident audit log. SQLite, stdio + loopback Streamable HTTP,
12 tools. Explicit non-goals: multi-user, multi-instance, RBAC, retries, workflow
editing, scheduling, dashboards, notifications.

**v2 — hosted team operations.**
PostgreSQL, OAuth/OIDC identity carried into every audit record, roles
(`viewer`/`operator`/`approver`/`admin`), multiple named n8n environments with
per-environment approval policy, N-of-M approvals, explicitly audited governed retries,
structural definition diffs, and operational metrics.

**v3 — AI-native workflow engineering.**
Workflows become governed artifacts: a declarative source format that compiles
deterministically to n8n workflow JSON, an evaluation lab that scores a workflow
against fixture suites before promotion, `plan → review → apply` changes with rollback,
a remediation assistant that proposes but never applies fixes, a vetted template
library, and enterprise controls (SSO enforcement, data residency, retention,
break-glass, exportable compliance evidence).

---

## Local development

**There is nothing to run yet.** No verified setup, build, test, or serve commands
exist at this stage, and none are documented here for that reason. This section will be
filled in as Phase 1 lands.

The *planned* toolchain, per `BUILD_PLAN.md` section 4, is Python 3.12 with `uv`, an
`src` layout, Alembic migrations, and pytest + Hypothesis — but `pyproject.toml`,
`alembic.ini`, and the package modules do not exist in the repository yet.

To read the design, start with [`docs/BUILD_PLAN.md`](docs/BUILD_PLAN.md).

### Configuration

When the package exists, configuration will be read from `N8N_OPERATOR_`-prefixed
environment variables and validated at process start; a malformed configuration is a
startup failure, not a runtime surprise. See [`.env.example`](.env.example) for the
variable names and `docs/ARCHITECTURE.md` section 7 for defaults and notes.

---

## ⚠️ Never commit credentials

**Do not commit n8n API keys, n8n credential exports, webhook secrets, bearer tokens,
`.env` files, local SQLite databases, or real workflow execution data to this
repository.**

This repository may be made public in the future. Treat every commit as permanently
public from the moment it is written — deleting a file in a later commit does not
remove it from history, and a leaked key must be rotated, not merely deleted.

- Secrets belong in the environment or a keyring, never in the registry file, never in
  source, never in docs.
- `.env.example` contains variable **names and placeholders only**.
- Registry files, fixtures, and examples must use synthetic data. Real customer records
  and real execution payloads do not belong here.
- `.gitignore` covers the common cases, but it is a safety net, not a control. Check
  what you are staging.

---

## License

**Apache-2.0**, as declared in `pyproject.toml`.

Note that the `LICENSE` file anticipated by `BUILD_PLAN.md` section 4 has not been added
to the repository yet, so the full license text is not present. That gap must be closed
before this repository is made public.

---

## Current limitations

- Phase 0: design only. No implementation, no tests, no CI runs.
- Apache-2.0 is declared in `pyproject.toml`, but the `LICENSE` file itself is missing.
- v1 is single-operator and single-instance by design: no multi-user, no RBAC, no
  multi-environment support.
- v1 never retries automatically; `UNKNOWN` outcomes require a human to check the
  downstream system.
- Accepted residual risks (`BUILD_PLAN.md` section 9.5): a compromised operator machine
  defeats every control below it; n8n itself is trusted and Operator does not sandbox
  what a dispatched workflow does; a human who approves without reading the approval
  page defeats the human gate.
- n8n output is untrusted input. Results are structurally shaped, redacted, and
  size-capped, and are delivered as data — but semantic sanitization is not possible,
  and every subsequent side effect requires its own prepare, approval, and handle.
