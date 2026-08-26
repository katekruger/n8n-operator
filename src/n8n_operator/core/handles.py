"""Operation handles: mint, bind, verify, burn (ADR-003).

A handle is an opaque ``op_<ULID>`` bound at mint time to
``(principal_id, workflow_id, definition_hash, argument_fingerprint)``. Possessing one
is not authority — the operation must also be APPROVED, unburnt, within its deadline,
and free of definition drift.

The burn is a conditional UPDATE whose affected-row count is checked, so exactly-once is
enforced by the database rather than by application logic (invariant I4).

Phase 3 (BUILD_PLAN section 12).
"""

from __future__ import annotations
