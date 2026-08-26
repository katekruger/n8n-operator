"""Parse, validate, canonicalize, hash, and snapshot the registry.

Loading is all-or-nothing: any violation of rules R1-R10 (BUILD_PLAN section 6.6) fails
the load and the server refuses to start. There is no partially-live allowlist (AC-02).

Each successful load produces a content-addressed snapshot persisted in
``registry_snapshots``; operations record the snapshot they were prepared against
(BUILD_PLAN section 6.7).

Phase 2 (BUILD_PLAN section 12).
"""

from __future__ import annotations
