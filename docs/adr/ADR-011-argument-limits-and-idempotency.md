# ADR-011: Core argument limits and idempotency namespaces

- **Status:** Accepted
- **Date:** 2026-08-26
- **Deciders:** Lead architect
- **Phase:** 0.1 (architecture-decision closure), implemented in phases 1 and 3
- **Related:** [ADR-003](ADR-003-operation-handles.md), [ADR-004](ADR-004-sqlite-to-postgres.md), [BUILD_PLAN.md](../BUILD_PLAN.md) sections 8.1, 9.2, [THREAT_MODEL.md](../THREAT_MODEL.md) T-12

## Context

Two phase-0 loose ends, both about the boundary between what a caller may submit and what
Operator durably keeps.

**Argument size.** Threat T-12 — a caller exhausting disk by preparing operations with huge
argument payloads — was mitigated only "at the transport" and carried as `partial`. That is
a weak answer for two reasons. Transport limits differ per transport, so the effective cap
depends on how the client connected; and the CLI, which is also an adapter over the same
core, has no transport limit at all. A limit that varies by entry point is not a limit on
the system.

**Idempotency scope.** The unique constraint was `(principal_id, idempotency_key)`. v1 has
exactly one principal, so in practice the namespace was *the whole installation*: two MCP
clients sharing the local principal share a key space, and a client that reuses a natural
key like `sync-contact-2026-08-26` across two different workflows collides with itself. The
collision surfaces as an idempotency conflict — spelled `IDEMPOTENCY_KEY_CONFLICT` in the
phase-0 taxonomy, superseded below — which reads as a caller error when it is really a
design artifact. v2's environments make it worse, not better.

## Decision

### 1. A core-enforced maximum canonical argument size

1. The limit lives in `core/`, applied inside the use case, and is therefore identical for
   every adapter — MCP over stdio, MCP over Streamable HTTP, and the CLI
   ([ADR-001](ADR-001-portable-mcp-core.md)).
2. It is measured over the **canonical JSON serialization** of the arguments — the same
   bytes the fingerprint is taken over ([ADR-003](ADR-003-operation-handles.md)) — not over
   the raw request body. One well-defined quantity, independent of transport framing,
   whitespace, or encoding choices.
3. **Oversized arguments fail before persistence.** The check runs before the operation row
   is written, so an oversized payload never reaches the database. The failure is the error
   `ARGUMENTS_TOO_LARGE` with the observed and permitted sizes in `details`; no operation is
   created and no state is entered. This is deliberately *not* an `INVALID` operation:
   `INVALID` means "we recorded your request and it failed validation", and recording is the
   thing we are refusing to do.
4. `N8N_OPERATOR_MAX_ARGUMENT_BYTES` sets the server ceiling (default 256 KiB). A registry
   entry may lower it per workflow via `limits.max_argument_bytes`; it may never raise it
   above the server ceiling (registry rule R11).
5. **Transport limits remain, as defense in depth.** They stop an abusive body earlier and
   more cheaply. They are no longer the mechanism the guarantee rests on.

Threat **T-12 moves from `partial` to `mitigated`** when this lands. Invariant **I10**
states the property: no operation is persisted whose canonical arguments exceed the
effective limit.

### 2. Idempotency namespaces

The idempotency namespace is **`(principal, environment, workflow_id, idempotency_key)`**.

| Situation | Result |
|---|---|
| Same namespace and key, same argument fingerprint | The existing operation is returned. `idempotent_replay: true`. No second operation. |
| Same namespace and key, different argument fingerprint | `IDEMPOTENCY_CONFLICT`. No operation is created. |
| Same key, different workflow (or environment, or principal) | Different namespace — an ordinary, independent operation. |

- `principal` and `environment` are **explicit columns** on `operations` in v1, carrying
  `local` and `default`. They are not implicit, not omitted, and not defaulted at read time.
  When v2 introduces real identities and named environments, the namespace semantics do not
  change and no migration rewrites existing keys.
- Enforcement is a unique index on
  `(principal_id, environment, workflow_id, idempotency_key)` where `idempotency_key IS NOT
  NULL` — a database constraint, not an application check (ADR-004 rule D8).
- Invariant **I8** is restated in these terms: two `prepare_operation` calls sharing a
  namespace and key return the same operation, never two.

### 3. Error-code supersession

The phase-0 taxonomy spelled this error `IDEMPOTENCY_KEY_CONFLICT`. The normative spelling
is now **`IDEMPOTENCY_CONFLICT`**, because the conflict is between *requests within a
namespace*, not between keys. `IDEMPOTENCY_KEY_CONFLICT` is superseded and must not appear
in code, tests, or documentation; a doc-consistency check enforces its absence. Nothing is
implemented yet, so this rename costs one search-and-replace and is recorded here so the
change is legible rather than mysterious.

## Consequences

### Positive

- The argument limit is a property of the system rather than of the entry point. A contract
  test can assert it once and have it hold for every adapter.
- Measuring the canonical form means the limit and the fingerprint agree about what "the
  arguments" are — no gap between what was measured and what was bound.
- Failing before persistence makes T-12 a genuine mitigation: the disk-exhaustion path
  requires getting past a check that runs before any write.
- Per-workflow lowering lets an operator hold a `read_only` reporting workflow to a few
  kilobytes without loosening anything globally.
- Namespacing removes a class of confusing false conflicts, and makes natural,
  human-meaningful idempotency keys safe to use.
- Making `principal` and `environment` explicit in v1 means v2 adds *values*, not *columns*
  — the multi-tenant migration does not touch idempotency semantics.

### Negative

- A legitimately large payload now fails where it previously might have been accepted, and
  256 KiB is a judgement call that will be wrong for someone. Mitigated by the per-workflow
  override and by an error that reports both sizes.
- Canonicalizing before size-checking means we serialize a large payload in memory to
  discover it is too large. Bounded by the transport limits that remain in front of it,
  which is part of why they stay.
- A four-part unique index is wider than a two-part one, and every idempotency lookup now
  carries four values through the core.
- Renaming an error code, even a not-yet-implemented one, is churn in every document that
  named it.

### Neutral

- 256 KiB is well above any plausible legitimate workflow input and well below anything that
  threatens local storage. It is a starting point, tunable per deployment.
- `environment` in v1 is a constant `default`, which looks like dead weight until v2, when
  it is the difference between a mechanical migration and a semantic one.

## Alternatives considered

**Keep transport-level limits only.** The phase-0 position. Rejected: the limit varies by
entry point, and the CLI has none, so the system-level property does not exist.

**Cap the raw request body instead of the canonical form.** Cheaper — check before parsing.
Rejected as the primary mechanism: the raw body's size depends on whitespace, encoding, and
framing, so the cap would be fuzzy and would not correspond to what is stored or
fingerprinted. Kept as the outer, cheap layer.

**Persist the operation as `INVALID` when arguments are oversized.** Better auditability of
attempts, and rejected: it makes the disk-exhaustion threat succeed by design. The attempt
is still audited — the rejection is an audit event; what is not created is an operation row
holding the payload.

**Namespace by principal alone (phase-0 behavior).** Rejected: within a single-principal v1
it makes the whole installation one key space, which produces conflicts between unrelated
workflows and unrelated clients.

**Namespace by principal and key, scoped per organization in v2.** The phase-0 v2 sketch.
Rejected in favor of including `workflow_id`: an idempotency key is naturally scoped to the
thing being made idempotent, and cross-workflow collision is a real, easy mistake.

**Derive the key from the argument fingerprint when the caller omits one.** Rejected:
implicit idempotency would silently collapse two deliberate, identical invocations — "send
this same reminder again" — into one, which is a correctness change made on the caller's
behalf without being asked.
