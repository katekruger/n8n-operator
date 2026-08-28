# Stage 06 prompt — governed retry and reconciliation

Copy this entire file into a fresh Claude Code session after Stage 05 is merged.

## Mission

Implement safe recovery without converting failure handling into duplicate execution.
Follow ADR-005, ADR-009, ADR-012, invariant I7, and invariant I11 exactly.

## Required work

- Implement `retry_operation` as creation of a new operation with
  `parent_operation_id`. Re-run authorization, current registry resolution, validation,
  preflight, limits, idempotency, and approval policy. Mint a fresh handle and never touch
  the parent’s state, handle, approval, snapshot, or result.
- Define eligible parent outcomes and refuse ambiguous or unsafe cases according to the
  Stage 00 contract. A read-only auto-approved retry is a fresh policy calculation, not
  inherited authority.
- Add retry lineage to operation reads, lists, audit resources, CLI output, and human
  approval views without exposing unauthorized ancestors or descendants.
- Implement exact-execution-ID reconciliation annotations for `UNKNOWN` operations. An
  annotation records externally verified evidence and actor identity; it is not a state
  transition and must never initiate a retry.
- Add CLI/admin workflows for recording, listing, and verifying reconciliation evidence.
  Require exact IDs and explicit confirmation; never infer outcomes from elapsed time.
- Audit all retry requests, refusals, new-operation creation, and reconciliation evidence.

## Required edge cases

Concurrent retries, repeated idempotency keys, retry after workflow reclassification,
retry after environment removal, retry after definition drift, parent from another
organization, chain depth/cycles, parent `UNKNOWN`, missing correlation, execution ID that
belongs to a different workflow, stale n8n history, insufficient permission, changed
quorum, and database failure between parent lookup and child creation.

## GTM usability proof

Demonstrate a failed enrichment safely retried after a credential fix, a production CRM
write requiring fresh approval, and an indeterminate campaign dispatch that is reconciled
by exact execution ID before a human chooses whether to create a new attempt.

## Completion gate

Property-test that no retry path mutates the parent or reuses approval, and that `UNKNOWN`
has no outgoing transition. Prove one approval authorizes one operation. Run concurrent
PostgreSQL tests, both transports, audit verification, and lineage redaction tests. Return
Stage 07 entry criteria.
