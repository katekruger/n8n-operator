"""Output redaction and size capping.

Applies a workflow's ``output.redact`` JSONPath expressions, replacing matched values
with ``"[REDACTED]"`` before a result leaves the process — before it reaches the model
and before it is persisted. Enforces ``output.max_bytes`` with an explicit
``truncated: true`` marker.

Redaction totality across nested and array positions is a Hypothesis property
(BUILD_PLAN section 10.2, AC-19).

Phase 3 (BUILD_PLAN section 12).
"""

from __future__ import annotations
