"""The twelve v1 tools.

Inventory is normative in BUILD_PLAN section 7.1; contracts in ``docs/MCP_TOOLS.md``:

    list_workflows        describe_workflow     get_instance_health
    validate_input        preflight_workflow    prepare_operation
    get_operation         execute_operation     cancel_operation
    list_operations       get_execution_result  get_execution_log

Arguments are Pydantic v2 models exported as JSON Schema 2020-12 with
``additionalProperties: false``. No tool accepts an n8n workflow ID, an instance URL, a
webhook path, or a raw request body — impossible by schema, not by check (boundary B1).

Results pass through an allowlist projection, so a new internal field is invisible by
default rather than leaked by default (boundary B5).

``approval_url`` is gated on caller locality: it is returned only over stdio or a
loopback-bound Streamable HTTP listener. Remote callers receive ``approval_required``, the
operation ID, and human-readable instructions instead of an address they cannot reach
(invariant I12, boundary B13, ADR-010).

Phase 5 (BUILD_PLAN section 12).
"""

from __future__ import annotations
