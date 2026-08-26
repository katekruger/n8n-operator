"""The FastAPI approval application, bound to 127.0.0.1 only.

Not configurable to a public interface in v1 (boundary B10). Also hosts the expiry
sweeper that writes T08 and T11 (ARCHITECTURE section 8).

Phase 6 (BUILD_PLAN section 12).
"""

from __future__ import annotations
