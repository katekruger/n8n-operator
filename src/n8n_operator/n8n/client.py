"""httpx client for the n8n instance.

Explicit connect and read timeouts on every request. **No retry logic, no backoff
helper, and no ``retries=`` transport setting** — a dispatch whose outcome cannot be
confirmed becomes UNKNOWN and is resolved by a human (ADR-005). A contract test greps
for the absence of retry machinery (AC-17).

TLS verification is not disableable by configuration (threat T-26).

A timeout is never interpreted as evidence that the workflow did not run: there is no
error-class check and no elapsed-time rule that turns an indeterminate dispatch into
``FAILED`` (ADR-009). Where a registry entry declares
``trigger.correlation: response_envelope``, the n8n execution ID is unwrapped from the
response here for reconciliation and debugging.

Phase 4 (BUILD_PLAN section 12).
"""

from __future__ import annotations
