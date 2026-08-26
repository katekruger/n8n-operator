# ADR-001: Portable, transport-agnostic core

- **Status:** Accepted
- **Date:** 2026-08-25
- **Deciders:** Lead architect
- **Phase:** 0 (architecture and bootstrap)
- **Related:** [ADR-007](ADR-007-deterministic-before-llm.md), [ARCHITECTURE.md](../ARCHITECTURE.md) section 2

## Context

n8n Operator must serve at least four callers: MCP clients over stdio (Claude Desktop),
MCP clients over Streamable HTTP (remote hosts), the operator's CLI, and the local
approval web app. All four need the same governance behavior — the same validation, the
same state machine, the same audit records.

The MCP specification and its Python SDK are both moving. The SDK went through a major
rework at v2 (`MCPServer`, a reorganized client, a migration guide from v1). The
protocol itself has been revised repeatedly. Anything we build directly against today's
SDK surface will need to move.

The tempting shape is to write the tools as the application: each tool handler validates,
checks policy, hits the database, calls n8n, and returns. It is the shortest path to a
working server and the one most MCP examples demonstrate.

## Decision

**Governance logic lives in a transport-agnostic core. MCP is an adapter over it, and so
is every other entry point.**

Concretely:

1. `core/service.py` exposes use cases (`prepare_operation`, `execute_operation`,
   `approve_operation`, …) that take plain Python arguments and return plain domain
   objects. It has no knowledge of MCP, HTTP, or the terminal.
2. `core/` may not import `mcp`, `fastapi`, `typer`, or any adapter module. This is a
   contract test, not a convention (BUILD_PLAN section 10.3).
3. `mcp/`, `cli/`, and `approval/` are thin: parse input, call one use case, shape the
   result for their transport. They never decide policy, touch the database, call n8n,
   or write audit records.
4. Domain types are Pydantic v2 models, shared by every adapter, so schema generation
   and validation are the same everywhere.

## Consequences

### Positive

- Governance is testable without a protocol in the loop. The bulk of the test suite —
  including every Hypothesis property in BUILD_PLAN section 10.2 — runs against pure
  functions with no transport, no server, and no network.
- SDK churn is contained. An MCP breaking change is a rewrite of `mcp/`, which is the
  thinnest layer in the codebase, and no security-relevant logic moves.
- The stdio and HTTP surfaces are provably identical, because both register the same
  tool set over the same core (AC-23).
- The CLI and approval app are first-class, not bolt-ons. The approval app performs a
  *state transition*, and it must go through the same state machine as everything else —
  which is only possible if the state machine is not inside the MCP layer.
- A future REST API, a Slack approval channel, or a second protocol adapter costs one
  new adapter, not a refactor.

### Negative

- One extra layer of indirection. A tool handler is two hops from the work.
- Some duplication between MCP argument models and core input types, where the transport
  shape and the domain shape differ.
- The discipline needs enforcement; without the import-graph test it decays quietly, as
  layering rules always do.

### Neutral

- We adopt the MCP Python SDK v2 line (`mcp >= 2.1, < 3`), which is the current stable
  release and the one that supports the current protocol revision. The v1 line is in
  maintenance and would need this migration anyway.

## Alternatives considered

**Tool handlers as the application.** Fastest to a demo. Rejected: it puts security
logic in the layer most exposed to protocol churn, makes the approval app either a
second implementation of the state machine or an awkward MCP client of itself, and makes
the core untestable without a running server.

**A framework-level abstraction over MCP.** Wrap the SDK in a generic protocol interface.
Rejected as premature — we have one protocol. The adapter boundary gives the same
insulation without inventing an abstraction we cannot yet validate against a second
implementation.

**Separate services per transport.** An MCP service and an approval service over a shared
API. Rejected for v1: distributed-systems complexity for a single-user tool, and the
shared API would just be this core exposed over a network.
