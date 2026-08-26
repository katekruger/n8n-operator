"""Preflight checks: reachable, exists, active, unchanged, credentialed.

Runs before an operation is offered for approval, and the definition-hash check runs
*again* at execute time — approval and execution are separated in time, so a workflow
modified in between cannot run under the old approval (boundary B8, AC-13).

Check codes are enumerated in ``docs/MCP_TOOLS.md`` section 2.5.

Phase 4 (BUILD_PLAN section 12).
"""

from __future__ import annotations
