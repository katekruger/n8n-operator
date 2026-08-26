"""Constructs the ``MCPServer`` and registers tools and resources.

The same tool set is registered regardless of transport, so the stdio and Streamable
HTTP surfaces are provably identical (AC-23).

Phase 5 (BUILD_PLAN section 12).
"""

from __future__ import annotations
