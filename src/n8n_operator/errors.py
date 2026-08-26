"""The error taxonomy.

One taxonomy, defined normatively in ``docs/MCP_TOOLS.md`` section 4 and implemented
once here. Adapters map these to MCP tool errors, CLI exit codes, or HTTP status
without inventing new codes (ARCHITECTURE section 9).

Every error carries a stable machine-readable ``code``, a human-readable ``message``,
optional structured ``details``, and an advisory ``retryable`` flag which is ``False``
for every side-effect-adjacent failure (ADR-005).

Phase 1 (BUILD_PLAN section 12).
"""

from __future__ import annotations
