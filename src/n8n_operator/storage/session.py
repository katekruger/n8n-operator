"""Engine and session lifecycle.

SQLite runs in WAL mode with a busy timeout, configured at connection setup only so it
never leaks into the schema (ADR-004 D9).

Phase 1 (BUILD_PLAN section 12).
"""

from __future__ import annotations
