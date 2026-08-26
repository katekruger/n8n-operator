"""MCP resources.

``registry://workflows``      the active snapshot as the model sees it, excluding
                              n8n_workflow_id, trigger, and every secret_ref
``audit://operations/{id}``   the ordered event chain for one operation

No prompts are exposed in v1 (BUILD_PLAN section 7.1).

Phase 5 (BUILD_PLAN section 12).
"""

from __future__ import annotations
