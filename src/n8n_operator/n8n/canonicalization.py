"""Versioned, evidence-driven workflow-definition canonicalization (ADR-008).

Implements CAN-01 through CAN-07 (BUILD_PLAN section 6.8) over the **raw** dict a
``GET /api/v1/workflows/{id}`` response parses to — never a Pydantic-reconstructed value
(see ``n8n/types.py``'s module docstring for why: CAN-01 is a security property, not a
convenience, and this module does not want its guarantee to depend on a model's
round-trip fidelity).

**Structural scope, decided before CAN-01 ever applies.** Only ``nodes``, ``connections``,
``settings``, and ``pinData`` are workflow-*graph* content. Everything else a workflow
read returns — ``id``, ``name``, ``active``, ``isArchived``, ``createdAt``, ``updatedAt``,
``versionId``, ``activeVersionId``, ``activeVersion``, ``versionCounter``,
``workflowPublishHistory``, ``tags``, ``meta``, ``staticData``, ``shared`` — is
administrative metadata about the *row*, not the *graph*, and was never a candidate for
inclusion to begin with (docs/N8N_COMPATIBILITY.md section 12). ``activeVersion`` in
particular is ``null`` whenever the workflow is inactive (section 5) — canonicalizing it
would make drift-checking break on every paused workflow.

**Within that scope**, CAN-01 applies: every field of ``nodes``/``connections``/
``settings`` contributes to the canonical form unless it matches
:data:`EXCLUSION_ALLOWLIST`. That allowlist currently has exactly two entries, each
justified by an empirical harness run against a live instance
(docs/N8N_COMPATIBILITY.md section 6), each scoped to the single n8n version the evidence
covers (CAN-03). ``pinData`` is structurally in scope (a workflow-content field) but
entirely excluded by evidence, not by the structural-scope decision above.

Everything CAN-05 names — node type, node parameters, credential bindings, connections,
workflow settings, trigger configuration, error-handling configuration — is simply never
added to the allowlist; there is no code path that could exclude it.

Phase 4 (BUILD_PLAN section 12).
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CANONICALIZATION_VERSION",
    "EXCLUSION_ALLOWLIST",
    "ExclusionEntry",
    "canonical_bytes",
    "canonical_form",
    "compute_definition_hash",
]

CANONICALIZATION_VERSION = 1
"""CAN-07: part of the hash preimage (domain separation). Bumping this changes every
hash computed by this module; it must accompany a new registry ``apiVersion`` and a
deliberate re-hash, never a silent revaluation of existing entries."""


@dataclass(frozen=True)
class ExclusionEntry:
    """One row of the exclusion allowlist (CAN-03): an enumerated, justified table in
    code — no wildcard or regex family is permitted here."""

    field_path: str
    evidence: str
    n8n_versions: str


EXCLUSION_ALLOWLIST: tuple[ExclusionEntry, ...] = (
    ExclusionEntry(
        field_path="nodes[].position",
        evidence=(
            "docs/N8N_COMPATIBILITY.md section 6 row 1: a node-position-only edit did "
            "not change webhook dispatch behavior (same input, same output, before and "
            "after)."
        ),
        n8n_versions="2.35.7",
    ),
    ExclusionEntry(
        field_path="pinData",
        evidence=(
            "docs/N8N_COMPATIBILITY.md section 6 row 3: pinning the trigger node's "
            "output had no effect on a live production webhook dispatch — the real "
            "request body was used, not the pinned value. Scoped to production webhook "
            "dispatch specifically; n8n's separate test-webhook path is not what "
            "Operator ever calls (ADR-006, ADR-009 section 6)."
        ),
        n8n_versions="2.35.7",
    ),
)

_EXCLUDED_NODE_FIELDS = frozenset(
    entry.field_path.removeprefix("nodes[].")
    for entry in EXCLUSION_ALLOWLIST
    if entry.field_path.startswith("nodes[].")
)
_EXCLUDED_TOP_LEVEL_FIELDS = frozenset(
    entry.field_path for entry in EXCLUSION_ALLOWLIST if "[]" not in entry.field_path
)


def _nfc_normalize(value: Any) -> Any:
    """CAN-04: strings NFC-normalized, recursively, structure otherwise preserved."""
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {_nfc_normalize(k): _nfc_normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_nfc_normalize(v) for v in value]
    return value


def canonical_form(raw_definition: dict[str, Any]) -> dict[str, Any]:
    """The canonical, NFC-normalized ``{nodes, connections, settings}`` scope of
    ``raw_definition`` — the exact structure :func:`canonical_bytes` serializes.

    ``raw_definition`` is the dict a ``GET /api/v1/workflows/{id}`` response parses to
    (``response.json()``), not a reconstructed model. Every field within
    ``nodes``/``connections``/``settings`` is preserved except the two allowlist entries
    above (CAN-01); ``pinData`` is read only to decide whether it *would* contribute
    (it never does — it is fully excluded) and is otherwise ignored.
    """
    nodes = raw_definition.get("nodes", [])
    scoped_nodes = [
        {k: v for k, v in node.items() if k not in _EXCLUDED_NODE_FIELDS} for node in nodes
    ]
    scoped = {
        "nodes": scoped_nodes,
        "connections": raw_definition.get("connections", {}),
        "settings": raw_definition.get("settings", {}),
    }
    return _nfc_normalize(scoped)  # type: ignore[no-any-return]


def canonical_bytes(raw_definition: dict[str, Any]) -> bytes:
    """CAN-04 deterministic serialization of :func:`canonical_form`, with CAN-07's
    version domain separation folded into the preimage."""
    payload = {
        "canonicalization_version": CANONICALIZATION_VERSION,
        "definition": canonical_form(raw_definition),
    }
    text = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return text.encode("utf-8")


def compute_definition_hash(raw_definition: dict[str, Any]) -> str:
    """``sha256:<hex>`` over :func:`canonical_bytes` — the value compared against a
    registry entry's ``definition_hash`` at preflight and again at execute (boundary B8)."""
    digest = hashlib.sha256(canonical_bytes(raw_definition)).hexdigest()
    return f"sha256:{digest}"
