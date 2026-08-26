"""httpx client for the n8n instance.

Explicit connect and read timeouts on every request. **No retry logic, no backoff
helper, and no ``retries=`` transport setting** — a dispatch whose outcome cannot be
confirmed becomes UNKNOWN and is resolved by a human (ADR-005). A contract test greps
for the absence of retry machinery (AC-17).

TLS verification is not disableable by configuration (threat T-26).

Phase 4 (BUILD_PLAN section 12).
"""

from __future__ import annotations
