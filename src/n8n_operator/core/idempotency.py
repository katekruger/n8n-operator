"""Canonical JSON and argument fingerprints.

``argument_fingerprint`` is sha256 over the canonical JSON serialization of the
arguments as submitted. Canonicalization must be idempotent and insensitive to key
order and insignificant whitespace, and sensitive to every structural difference —
both are Hypothesis properties (BUILD_PLAN section 10.2).

The fingerprint recorded at prepare is the fingerprint checked at execute, which is what
binds an approval to specific arguments (invariant I5).

Also home to the two limits ADR-011 fixes:

* the **core-enforced** maximum canonical argument size, applied identically for every
  adapter and **before** persistence, so an oversized payload never reaches the database
  (invariant I10, boundary B12); and
* the idempotency **namespace**, ``(principal, environment, workflow_id,
  idempotency_key)`` — same namespace and fingerprint returns the existing operation,
  same namespace and different fingerprint is ``IDEMPOTENCY_CONFLICT`` (invariant I8).

Phase 3 (BUILD_PLAN section 12).
"""

from __future__ import annotations
