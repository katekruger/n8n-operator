"""The only module that inserts into ``audit_log``.

The audit row commits in the same transaction as the state change it records. If the
audit write fails, the transition did not happen (invariant I6).

Phase 3 (BUILD_PLAN section 12).
"""

from __future__ import annotations
