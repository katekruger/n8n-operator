"""``n8n-operator registry`` — validate, list, show, hash, reload.

``registry validate`` exits non-zero and names the offending entry and rule on any
violation of R1-R10 (AC-02); it is meant to run in CI on the repository holding the
registry. ``registry hash`` computes a ``definition_hash`` for an entry
(WORKFLOW_REGISTRY section 5).

Phase 2 (BUILD_PLAN section 12).
"""

from __future__ import annotations
