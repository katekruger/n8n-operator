"""n8n Operator — a governed MCP control plane for approved n8n workflows.

v1: registry, MCP server (stdio + Streamable HTTP), n8n integration, the full
prepare/approve/execute lifecycle, and the operator CLI. See ``docs/BUILD_PLAN.md``
section 12 for the phase checklist and ``docs/V1_LIMITATIONS.md`` for what v1
deliberately does not do.

Layering (ADR-001, enforced by a contract test):

    cli, mcp, approval  ->  core  ->  registry, storage, audit, n8n

``core`` must not import any adapter package, ``fastapi``, ``typer``, or the MCP SDK.
"""

__version__ = "1.0.0rc3"

__all__ = ["__version__"]
