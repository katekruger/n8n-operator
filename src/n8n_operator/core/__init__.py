"""The transport-agnostic domain core (ADR-001).

Governance lives here: the state machine, operation handles, argument fingerprints,
redaction, and the use cases that orchestrate them. This package knows nothing about
MCP, HTTP, or the terminal, and must not import ``n8n_operator.mcp``,
``n8n_operator.cli``, ``n8n_operator.approval``, ``fastapi``, ``typer``, or the MCP SDK.

The rule is enforced by a contract test, not by convention (BUILD_PLAN section 10.3).
"""
