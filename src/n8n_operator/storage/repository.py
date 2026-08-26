"""Data access. Portable SQL only.

No raw SQL strings outside migrations (ADR-004 D5). Every mutation of ``operations``
carries a ``state_version`` optimistic-concurrency guard, and the handle burn is an
explicit compare-and-set — correctness never relies on SQLite's single writer, because
PostgreSQL will not provide it (ADR-004 D7).

Phase 1 (BUILD_PLAN section 12).
"""

from __future__ import annotations
