"""Hash-chain construction and verification.

Each entry hashes the canonical serialization of its own fields together with the
previous entry's hash; genesis is 64 zeros. Verification walks a range and reports the
first break by sequence number (AC-22).

v2 adds the ``AuditAnchor`` interface -- content-free anchors published to a signed local
file or an authenticated HTTPS webhook -- so chain state is pinned somewhere an attacker
with database write access does not control (ADR-012, residual risk RR-4).

Phase 3 (BUILD_PLAN section 12).
"""

from __future__ import annotations
