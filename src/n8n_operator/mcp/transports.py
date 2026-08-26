"""stdio and Streamable HTTP transports.

stdio is the default: the parent process is the security boundary and no network
listener exists. Streamable HTTP binds 127.0.0.1 by default; a non-loopback bind
requires a bearer token **and** an Origin allowlist, or startup fails (boundary B9,
AC-20). The Origin check is DNS-rebinding defense (threat T-34).

Phase 5 (BUILD_PLAN section 12).
"""

from __future__ import annotations
