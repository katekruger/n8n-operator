# v2 staged build prompts

These prompts turn [Phase 10](../../BUILD_PLAN.md#phase-10--v2) into an ordered build
sequence for Claude Code. Each file is intentionally executable on its own, but later
stages assume every earlier stage has merged into `main`.

The sequence is designed for two audiences at once:

- platform owners at modern startups who need a safe way to let agents operate revenue
  workflows; and
- RevOps, marketing operations, and GTM engineers at scaling and Series C companies who
  need identity, separation of environments, approvals, auditability, and useful failure
  visibility without exposing the raw n8n control plane.

## Order and release gates

| Stage | Outcome | Depends on |
|---|---|---|
| 00 | Close v2 contracts, UX journeys, acceptance criteria, and architecture | v1 baseline |
| 01 | PostgreSQL production store and verified SQLite migration | 00 |
| 02 | Organizations, principals, service identities, and OIDC | 01 |
| 03 | Default-deny RBAC across every adapter and query | 02 |
| 04 | Named environments and registry overlays | 03 |
| 05 | N-of-M team approvals and delivery routing | 04 |
| 06 | Governed retries and exact-ID reconciliation annotations | 05 |
| 07 | Safe structural workflow-definition diffs | 04 |
| 08 | Scoped metrics, audit queries, and alert hooks | 06, 07 |
| 09 | Signed-file and authenticated-webhook audit anchoring | 08 |
| 10 | GTM starter kits, examples, onboarding, and operational recipes | 09 |
| 11 | Integrated v2 proof, migration rehearsal, security review, and release candidate | all |

Do not combine stages merely to move faster. Identity, authorization, environment scope,
and approval quorum are load-bearing security boundaries. Each needs its own reviewable
change and retained evidence.

## Product guardrails for every stage

- Preserve the existing `prepare -> approve -> execute` lifecycle and all twelve states.
- Preserve the exact v2 inventory of eight new MCP tools and twenty total tools.
- Never expose raw n8n workflow IDs, instance URLs, tokens, or credential references.
- Approval remains out-of-band. `request_approval` routes a request; it never grants one.
- An unauthorized object is indistinguishable from a nonexistent object.
- A retry creates a new operation and recalculates current policy; it never reuses approval.
- `UNKNOWN` remains terminal. Reconciliation adds evidence, never a transition.
- v2 does not edit workflows, schedule work, autonomously remediate failures, or build a
  dashboard. Those boundaries prevent the control plane from becoming another execution
  engine or an ungoverned admin surface.
- Keep the transport-agnostic core. MCP, CLI, HTTP, identity providers, databases,
  notification sinks, and anchor sinks remain adapters around explicit ports.

## Definition of a useful v2

A useful result is not merely “all eight tools exist.” A new GTM engineer should be able
to clone the project, run a local demo, understand staging versus production, register a
representative CRM or campaign workflow, see why it is blocked, request the correct human
approval, execute it once, and explain the outcome from redacted evidence. A Series C
platform owner should be able to prove which identity acted, what policy applied, who
approved, which environment was touched, and whether the audit chain still agrees with an
external anchor.

Each prompt therefore requires usability evidence, negative authorization tests,
operational documentation, and a concise handoff—not only implementation code.
