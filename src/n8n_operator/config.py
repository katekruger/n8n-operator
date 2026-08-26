"""Process configuration.

Pydantic v2 ``BaseSettings`` with the ``N8N_OPERATOR_`` prefix, validated at process
start so a malformed configuration is a startup failure rather than a runtime surprise.
Settings are enumerated in ``docs/ARCHITECTURE.md`` section 7.

Credentials are resolved here from the environment or the OS keyring, held in memory,
and never written to the registry, the database, or logs (ADR-006).

Phase 1 (BUILD_PLAN section 12).
"""

from __future__ import annotations
