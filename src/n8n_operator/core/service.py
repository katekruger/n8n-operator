"""Use-case orchestration — the portable core (ADR-001).

Exposes prepare / approve / execute / cancel / inspect as functions over plain domain
types. Every adapter calls into here; none of them reimplements policy. Request flows
are diagrammed in ``docs/ARCHITECTURE.md`` section 4.

Phase 3 (BUILD_PLAN section 12).
"""

from __future__ import annotations
