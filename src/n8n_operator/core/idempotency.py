"""Canonical JSON and argument fingerprints.

``argument_fingerprint`` is sha256 over the canonical JSON serialization of the
arguments as submitted. Canonicalization must be idempotent and insensitive to key
order and insignificant whitespace, and sensitive to every structural difference —
both are Hypothesis properties (BUILD_PLAN section 10.2).

The fingerprint recorded at prepare is the fingerprint checked at execute, which is what
binds an approval to specific arguments (invariant I5).

Phase 3 (BUILD_PLAN section 12).
"""

from __future__ import annotations
