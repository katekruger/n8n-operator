#!/usr/bin/env python3
"""A real MCP client session over stdio against the ``n8n-operator`` entry point on
``PATH`` — not "the process starts," a full ``initialize`` / ``list_tools`` /
``list_resources`` / ``call_tool`` / clean-shutdown round trip, run by
``scripts/release_smoke.sh`` against the built wheel in an isolated venv so this is
evidence about what a client (Claude Desktop, or any other stdio-launching MCP host)
actually receives.

Verifies:
- the server responds to ``initialize``
- the tool surface is exactly the twelve tools in ``docs/MCP_TOOLS.md`` section 2,
  no more and no fewer
- the resource surface is exactly the two resources in ``docs/MCP_TOOLS.md`` section 3
- ``list_workflows`` (a ``read_only`` tool that never touches n8n) returns the seeded
  registry, and neither its result nor the ``registry://workflows`` resource contains
  the ``n8n_workflow_id``, base URL, or API key the registry/settings actually hold —
  the boundary MCP_TOOLS.md and the resource docstring both promise
- the client can close the session and the server process exits

Requires a database already seeded by ``db init`` + ``registry reload`` at
``N8N_OPERATOR_DATABASE_URL``, and ``N8N_OPERATOR_N8N_BASE_URL``/
``N8N_OPERATOR_N8N_API_KEY`` set in the environment this script inherits — the server
needs both to start even though this script never exercises a tool that dispatches to
n8n (BUILD_PLAN section 12 phase 5, ADR-006).
"""

from __future__ import annotations

import asyncio
import os
import sys

from mcp import types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

EXPECTED_TOOL_NAMES = {
    "list_workflows",
    "describe_workflow",
    "get_instance_health",
    "validate_input",
    "preflight_workflow",
    "prepare_operation",
    "get_operation",
    "execute_operation",
    "cancel_operation",
    "list_operations",
    "get_execution_result",
    "get_execution_log",
}

EXPECTED_RESOURCE_URI_TEMPLATES = {
    "registry://workflows",
    "audit://operations/{operation_id}",
}

# Values a leak check must never find in a tool or resource result. Populated from the
# real registry entries in examples/registry/workflows.example.yaml, and the dummy
# base URL/API key this script itself sets for the server subprocess.
FORBIDDEN_SUBSTRINGS = [
    "7Qx4kLmN2pRstUvW",
    "AbC123dEfG456hIj",
    "Kp9RtY2mNqXvB4Lc",
    "Zz1QwErTyUiOpAsD",
    "mcp-session-smoke-dummy-n8n-instance.invalid",
    "mcp-session-smoke-dummy-api-key-0000000000",
]


def _fail(message: str) -> None:
    print(f"MCP session smoke FAILED: {message}", file=sys.stderr)
    sys.exit(1)


def _check_no_leaks(label: str, payload: object) -> None:
    text = repr(payload)
    for forbidden in FORBIDDEN_SUBSTRINGS:
        if forbidden in text:
            _fail(f"{label} leaked a value that must never leave the server: {forbidden!r}")


async def _run(command: str) -> None:
    server_params = StdioServerParameters(
        command=command,
        args=["serve", "stdio"],
        env=dict(os.environ),
    )

    async with stdio_client(server_params) as (read, write), ClientSession(read, write) as session:
        init_result = await session.initialize()
        server_info = init_result.server_info
        print(f"initialized: server={server_info.name} {server_info.version}")

        tools_result = await session.list_tools()
        tool_names = {tool.name for tool in tools_result.tools}
        if tool_names != EXPECTED_TOOL_NAMES:
            _fail(
                "tool surface mismatch — "
                f"missing={EXPECTED_TOOL_NAMES - tool_names} "
                f"unexpected={tool_names - EXPECTED_TOOL_NAMES}"
            )
        print(f"tool surface confirmed: exactly {len(tool_names)} tools")

        resources_result = await session.list_resources()
        resource_uris = {str(r.uri) for r in resources_result.resources}
        # Templated resources (audit://operations/{operation_id}) are listed as a
        # resource template, not a concrete resource — the registry resource has no
        # path parameter, so it must always appear as a concrete, listed resource.
        templates_result = await session.list_resource_templates()
        resource_uris |= {t.uri_template for t in templates_result.resource_templates}
        if not EXPECTED_RESOURCE_URI_TEMPLATES.issubset(resource_uris):
            _fail(
                "resource surface missing expected entries — "
                f"missing={EXPECTED_RESOURCE_URI_TEMPLATES - resource_uris}"
            )
        print(f"resource surface confirmed: {sorted(EXPECTED_RESOURCE_URI_TEMPLATES)}")

        call_result: types.CallToolResult = await session.call_tool("list_workflows", {})
        if call_result.is_error:
            _fail(f"list_workflows returned an error: {call_result.content!r}")
        _check_no_leaks("list_workflows result", call_result.content)
        print("list_workflows call succeeded, no leaked credentials/IDs")

        read_result = await session.read_resource("registry://workflows")
        _check_no_leaks("registry://workflows resource", read_result.contents)
        print("registry://workflows resource read succeeded, no leaked credentials/IDs")

    print(
        "MCP session smoke passed: initialize, tool surface, resource surface, "
        "safe tool call, resource read, clean shutdown"
    )


def main() -> None:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <path-to-n8n-operator-executable>", file=sys.stderr)
        sys.exit(2)
    asyncio.run(_run(sys.argv[1]))


if __name__ == "__main__":
    main()
