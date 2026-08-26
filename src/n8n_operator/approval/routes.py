"""Approval routes.

    GET  /approve/{token}   render the pending operation for a human
    POST /approve/{token}   T06 -> APPROVED
    POST /reject/{token}    T07 -> REJECTED

GET grants nothing. Approval requires a POST from a human session with a CSRF token,
Origin and Host validation, and SameSite=Strict cookies (threats T-08, T-15, T-16).
Tokens are 256-bit, single-use, TTL-bounded, and stored only as sha256 hashes
(AC-21).

Phase 6 (BUILD_PLAN section 12).
"""

from __future__ import annotations
