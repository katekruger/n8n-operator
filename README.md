# n8n Operator

**A governed MCP control plane for discovering, validating, executing, and debugging
approved n8n workflows from Claude, ChatGPT, Codex, and compatible MCP clients.**

> **Status: configuration and storage foundation (phase 1).** The documentation set is
> complete and the architecture decisions from phase 0.1 are closed. Phase 1 adds
> validated configuration, the full error taxonomy, the complete v1 database schema and
> its first migration, a portable storage layer, and the `db init | migrate | status`
> CLI. No registry, MCP tool, n8n integration, or workflow-execution behavior is
> implemented yet — see [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md) section 12 for the phase
> checklist.

---

## The problem

n8n is an excellent workflow engine and a poor agent surface. Pointing an LLM at a raw
n8n instance hands it an unbounded, unversioned, credential-bearing remote-execution
primitive: the workflow list is discovered at runtime, the input contract is implicit in
the node graph, failures surface as raw execution JSON, and every webhook is a live
production side effect.

n8n Operator is the place to stand between "the model decided to do a thing" and "the
thing happened."

## What it does

| | |
|---|---|
| **Discover** | Only workflows an operator explicitly registered, with human-authored descriptions, risk classes, and input schemas. |
| **Validate** | Arguments checked against a declared JSON Schema before anything reaches n8n. |
| **Preflight** | Target verified live, active, and unmodified since registration, before approval is sought. |
| **Execute** | An explicit `prepare → approve → execute` lifecycle with single-use handles, idempotency, and a durable audit trail. |
| **Debug** | Redacted, structured execution traces — enough to diagnose, not enough to exfiltrate. |

## What it refuses to do

- Expose a workflow that is not in the registry, however live it is on the instance.
- Accept a raw n8n workflow ID, URL, or payload in any tool argument.
- Return a credential, token, n8n ID, or instance URL in any tool result.
- Let an MCP client approve its own operation — approval is out-of-band, always.
- Retry anything automatically. Ambiguous outcomes surface as `UNKNOWN` for a human.
- Edit workflows (v1 and v2). Authoring stays in the n8n UI.

## Documentation

| Document | What it covers |
|---|---|
| [BUILD_PLAN.md](docs/BUILD_PLAN.md) | **Normative.** Product definition, version boundaries, state machine, registry schema, tool inventory, storage model, security boundaries, tests, acceptance criteria, phase checklist. |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, layering, request flows, data trust, persistence, configuration. |
| [THREAT_MODEL.md](docs/THREAT_MODEL.md) | Assets, trust boundaries, STRIDE analysis, LLM-specific threats, residual risks. |
| [WORKFLOW_REGISTRY.md](docs/WORKFLOW_REGISTRY.md) | How to register a workflow, and how to classify it correctly. |
| [MCP_TOOLS.md](docs/MCP_TOOLS.md) | **Normative** for tool arguments, results, and the error taxonomy. |

### Decision records

| ADR | Decision |
|---|---|
| [001](docs/adr/ADR-001-portable-mcp-core.md) | Portable, transport-agnostic core; MCP is an adapter |
| [002](docs/adr/ADR-002-default-deny-registry.md) | Default-deny YAML workflow registry |
| [003](docs/adr/ADR-003-operation-handles.md) | Operation handles as single-use capabilities |
| [004](docs/adr/ADR-004-sqlite-to-postgres.md) | SQLite in v1, PostgreSQL in v2, one schema throughout |
| [005](docs/adr/ADR-005-no-automatic-retry-v1.md) | No automatic retries in v1 |
| [006](docs/adr/ADR-006-server-owned-n8n-credentials.md) | Server-owned n8n credentials |
| [007](docs/adr/ADR-007-deterministic-before-llm.md) | Deterministic enforcement before LLM judgment |
| [008](docs/adr/ADR-008-conservative-definition-canonicalization.md) | Conservative workflow-definition canonicalization |
| [009](docs/adr/ADR-009-dispatch-correlation.md) | Dispatch correlation and indeterminate outcomes |
| [010](docs/adr/ADR-010-approval-delivery-and-expiry.md) | Approval delivery and expiry semantics |
| [011](docs/adr/ADR-011-argument-limits-and-idempotency.md) | Core argument limits and idempotency namespaces |
| [012](docs/adr/ADR-012-governed-retry-and-audit-anchoring.md) | Governed retry and external audit anchoring |

## Version boundaries

**v1** — single user, one n8n instance, SQLite, stdio and remote Streamable HTTP MCP,
CLI, YAML registry, read-only inspection, input validation, preflight,
prepare/approve/execute, local approval page, idempotency, audit log. No retries, no
workflow editing.

**v2** — PostgreSQL, multiple users and organizations, OAuth/OIDC, RBAC, multiple n8n
environments, team approvals, monitoring, governed retries, workflow definition diffs.

**v3** — declarative workflow compiler, evaluation lab, governed workflow changes,
remediation assistant, template library, enterprise controls.

Full boundary table: [BUILD_PLAN.md](docs/BUILD_PLAN.md) section 3.

## Development

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

```bash
uv run pytest
```

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

```bash
uv run python scripts/check_docs_consistency.py
```

The last command enforces that the documentation set agrees with itself and with the
repository tree — state names, transition IDs, tool inventory, acceptance criteria,
cross-document links, and the published file tree. It runs in CI and as a contract test.

## License

Apache-2.0. See [LICENSE](LICENSE).
