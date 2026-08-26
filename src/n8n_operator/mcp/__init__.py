"""The MCP adapter — thin by design (ADR-001).

Built on the official MCP Python SDK v2 (``mcp.server.MCPServer``). Translates protocol
input into a single ``core.service`` call and shapes the result for the transport. It
never decides policy, touches the database, calls n8n, or writes audit records.

This is the product's primary attack surface, which is exactly why it contains no
security logic: whatever a client sends arrives at the core as a validated argument
object, and the core behaves identically regardless of who called it.
"""
