"""Typed models for n8n API responses.

Responses are parsed into typed models; a parse failure yields a structured FAILED
result rather than an unhandled exception (threat T-32). n8n output is untrusted input
(ARCHITECTURE section 5).

Phase 4 (BUILD_PLAN section 12).
"""

from __future__ import annotations
