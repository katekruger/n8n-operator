# Stage 09 prompt — external audit anchoring

Copy this entire file into a fresh Claude Code session after Stage 08 is merged.

## Mission

Implement ADR-012’s external tamper-evidence boundary through the `AuditAnchor` interface,
a signed append-only local file, and an authenticated HTTPS webhook sink.

## Required work

- Implement typed `ChainAnchor`, `AnchorReceipt`, and `AnchorVerification` contracts that
  contain sequence, entry hash, covered count, timestamp, and no audit content.
- Implement a signed local anchor file stored outside the database. Define key generation,
  file permissions, rotation, append/locking semantics, crash recovery, verification, and
  backup/restore procedures. Never store a private signing key in the database.
- Implement an authenticated HTTPS webhook anchor with TLS verification, bounded timeouts,
  idempotency, signed or token-authenticated requests, receipt persistence, and no silent
  failure. Do not retry any business workflow when anchor delivery fails.
- Add on-demand CLI publish/verify/status commands and a bounded scheduled anchoring worker
  or documented external scheduler entrypoint, according to Stage 00. Failure must be
  visible through logs, metrics, audit records, health, and alert hooks.
- Verify anchors against an independent database copy without requiring Operator to mutate
  either source.
- Document the exact protection gained and the attacks not solved when an adversary owns
  the database, host, signing key, and sink.

## Required edge cases

Empty chain, repeated anchor, concurrent publishers, file truncation, modified historical
line, wrong key, key rotation, read-only filesystem, disk full, process crash mid-append,
webhook timeout, forged receipt, replayed receipt, sink rollback, clock skew, chain advanced
during publish, database restored from an older backup, and content accidentally added to
the anchor payload.

## Completion gate

Use deterministic test keys only in fixtures. Add property and tamper tests, artifact/secret
inspection, and an integration sink. Prove that publication failure is visible but does not
corrupt the chain or repeat an n8n execution. Return Stage 10 entry criteria.
