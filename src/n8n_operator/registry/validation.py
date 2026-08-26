"""Caller-argument validation against a workflow's declared input schema.

JSON Schema draft 2020-12 with ``additionalProperties: false`` required on every
registry ``input_schema`` (rule R4). Errors carry JSON-Pointer paths so a model can
repair its own call without guessing (AC-04).

Phase 2 (BUILD_PLAN section 12).
"""

from __future__ import annotations
