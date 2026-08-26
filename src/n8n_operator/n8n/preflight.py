"""Preflight checks: reachable, exists, active, unchanged, credentialed.

Runs before an operation is offered for approval, and the definition-hash check runs
*again* at execute time — approval and execution are separated in time, so a workflow
modified in between cannot run under the old approval (boundary B8, AC-13).

Check codes are enumerated in ``docs/MCP_TOOLS.md`` section 2.5. Statuses are ``pass``,
``fail``, ``skipped``, and the two non-blocking statuses from ADR-009 — ``warn`` and
``unverifiable``. **Only ``fail`` produces BLOCKED.**

Two honesty constraints from ADR-009 apply here:

* credential checks report whether a credential is **bound**, never whether it is valid;
  validity is ``unverifiable`` absent a supported n8n mechanism that tests it (threat T-41); and
* a workflow declaring ``correlation: none`` gets a ``warn`` so the reduced reconciliation
  capability is visible before an approver decides, not during an incident (threat T-40).

Definition canonicalization is conservative: inclusion by default, exclusion only on
harness evidence (CAN-01 through CAN-07, ADR-008).

Phase 4 (BUILD_PLAN section 12).
"""

from __future__ import annotations
