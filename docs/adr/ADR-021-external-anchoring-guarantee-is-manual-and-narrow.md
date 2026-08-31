# ADR-021: External audit anchoring protects only against database-only tampering, and publishing stays a manual, operator-initiated action

- **Status:** Accepted
- **Date:** 2026-08-31
- **Deciders:** Kate Kruger
- **Phase:** post-10 (v2 closure)
- **Related:** [ADR-012](ADR-012-governed-retry-and-audit-anchoring.md), [THREAT_MODEL.md](../THREAT_MODEL.md) RR-4

## Context

[ADR-012](ADR-012-governed-retry-and-audit-anchoring.md) already records why external
anchoring exists and how it's built: `audit_anchor/`'s `LocalFileAnchor` (Ed25519-signed,
append-only, `fcntl`-locked) and `HttpsWebhookAnchor` (authenticated POST to an external
endpoint) each publish a content-free checkpoint —
`{covers_through_seq, entry_hash, entry_count, anchored_at, signature, public_key}`,
never audit content — so that an attacker with database write access can't silently
rewrite `audit/chain.py`'s hash chain without the rewrite becoming detectable against
an independently-held anchor.

What isn't recorded as its own decision, and is easy to assume otherwise from the name
"external audit anchoring," is the guarantee's exact boundary: anchoring only protects
against *database-only* compromise, and publishing is not automatic. Both are load-
bearing for anyone reasoning about what this feature actually buys them, and both were
confirmed, not merely inferred, while writing this repository's `AGENTS.md`.

## Decision

**Record the guarantee's boundary as its own decision, separate from ADR-012's build
rationale.** The boundary itself — narrow, manual — is not being changed here; this
ADR documents an existing, deliberate design choice rather than proposing a new one,
because misreading "external audit anchoring" as "tamper-proof audit log, full stop"
is the single most likely misunderstanding a future engineer or auditor could bring to
this feature, and nothing in ADR-012 states the boundary as plainly as this:

* **What it protects against**: an attacker who edits the SQLite/PostgreSQL audit
  content but does not *also* hold the anchor file (or control the webhook endpoint)
  and the Ed25519 signing key — stated verbatim in `local_file.py`'s own module
  docstring, but not elsewhere.
* **What it does not protect against**: an attacker who compromises both the database
  and the anchor's own storage/key (e.g. both live on the same host under the same
  access controls) gets no benefit from anchoring at all — the guarantee degrades to
  nothing, silently, with no code-level signal that this has happened.
* **Anchoring is not automatic.** `anchor publish` is an explicit CLI action. Nothing
  in this repo schedules it. An operator who never runs it (or runs it once and
  stops) has a hash-chain with zero external checkpoints protecting it, and the
  product gives no warning that this is the case.
* **Coverage is a watermark, not a running guarantee.** Each publish covers entries
  up to that moment's `covers_through_seq`; anything appended after the last publish
  is unprotected until the next one runs.

Options considered:

* Record the guarantee's boundary as its own decision, so a future reader doesn't
  have to re-derive it from `local_file.py`'s module docstring (**chosen**)
* Extend `audit_anchor/` to publish automatically on some schedule or on every write,
  closing the "anchoring isn't automatic" gap now
* Leave the boundary implicit in the code and module docstrings, as it was before
  this ADR

## Consequences

### Positive

- A future engineer changing `audit/chain.py`'s `entry_canonical_bytes` (what
  actually gets hashed) now has a single, explicit place stating that such a change
  silently breaks verification of every anchor published before it — the anchor's own
  payload carries no version field guarding against this, which this ADR surfaces as
  a known gap rather than a novel one.
- This decision does not change any code — ADR-012 remains the authoritative record
  of *why* anchoring was built and *how*; this ADR is the record of *exactly how far
  its guarantee reaches*, for anyone deciding whether to rely on it.

### Negative

- Until publishing is made automatic (or at minimum, until the CLI warns an operator
  who has never run `anchor publish` at all, or whose last publish is far behind the
  current `audit_log` sequence), the practical value of this feature depends entirely
  on operational discipline this codebase does not enforce or even check for.
  Accepted: "closing" this gap by making publishing automatic was explicitly *not*
  chosen here — that would be new v2/v3 scope (a scheduler, a background task, a new
  failure mode for "the scheduled publish didn't run"), not a documentation task, and
  this ADR's job is to record the current, narrower guarantee accurately, not to
  expand it.

## Alternatives considered

See the options list above. Automating publication was considered and deliberately
deferred as new feature scope, not adopted here.
