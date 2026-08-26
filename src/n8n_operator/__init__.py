"""n8n Operator — a governed MCP control plane for approved n8n workflows.

Phase 0 (architecture and bootstrap): this package is a structural skeleton only.
No product functionality is implemented yet. See ``docs/BUILD_PLAN.md`` section 12
for the phase checklist that fills it in.

Layering (ADR-001, enforced by a contract test):

    cli, mcp, approval  ->  core  ->  registry, storage, audit, n8n

``core`` must not import any adapter package, ``fastapi``, ``typer``, or the MCP SDK.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
