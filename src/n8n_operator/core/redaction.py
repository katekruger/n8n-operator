"""Output redaction and size capping.

Applies a workflow's ``output.redact`` JSONPath expressions, replacing matched values
with ``"[REDACTED]"`` before a result leaves the process — before it reaches the model
and before it is persisted. Enforces ``output.max_bytes`` with an explicit
``truncated: true`` marker.

Three independent operations, composed by ``core/service.py`` in this order:

1. :func:`redact` — the workflow's own registered ``output.redact`` JSONPath rules
   (validated as parseable at registry-load time, rule R9).
2. :func:`scrub_secrets` — defense in depth against a *known, configured* secret value
   (the operator's own n8n API key or a webhook secret) appearing verbatim in a result,
   independent of whether any redaction path happens to cover it (boundary B5/B6).
3. :func:`cap_output` — ``output.max_bytes``, with an explicit ``truncated`` marker
   rather than a silently clipped, invalid payload.

Redaction totality across nested and array positions is a Hypothesis property
(BUILD_PLAN section 10.2, AC-19).

Phase 3 (BUILD_PLAN section 12).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from typing import Any

from jsonpath_ng import parse as parse_jsonpath

__all__ = ["REDACTED_MARKER", "cap_output", "redact", "scrub_secrets"]

REDACTED_MARKER = "[REDACTED]"


def redact(value: Any, paths: Sequence[str]) -> Any:
    """``value`` with every match of every JSONPath in ``paths`` replaced by
    :data:`REDACTED_MARKER`.

    Operates on, and returns, a deep copy — the caller's own ``value`` (which may still
    be needed unredacted elsewhere, e.g. for fingerprinting) is never mutated. Each path
    is expected to already be a valid, parseable JSONPath expression (rule R9 checks this
    at registry-load time); a path matching nothing is simply a no-op, not an error —
    a workflow's redaction list may legitimately not apply to every call's arguments.
    """
    result = copy.deepcopy(value)
    for path in paths:
        expr = parse_jsonpath(path)
        expr.update(result, REDACTED_MARKER)
    return result


def scrub_secrets(value: Any, secrets: Sequence[str]) -> Any:
    """``value`` with every literal occurrence of any string in ``secrets`` replaced by
    :data:`REDACTED_MARKER`, anywhere it appears inside a string leaf.

    Defense in depth distinct from :func:`redact`: a workflow's ``output.redact`` list is
    authored against expected result *shape* and can miss a secret that leaks through an
    unanticipated field (an error message quoting a header, for instance). This instead
    matches on the secret's own *value* — the operator's configured n8n API key or a
    webhook secret — wherever it appears, independent of shape (boundary B5/B6). Empty
    strings are ignored (an empty needle would "match" and mangle every string leaf).
    """
    nonempty = [s for s in secrets if s]
    if not nonempty:
        return value
    return _scrub_recursive(value, nonempty)


def _scrub_recursive(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        text = value
        for secret in secrets:
            text = text.replace(secret, REDACTED_MARKER)
        return text
    if isinstance(value, dict):
        return {k: _scrub_recursive(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_recursive(v, secrets) for v in value]
    return value


def _compact_json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def cap_output(value: Any, *, max_bytes: int) -> tuple[Any, bool]:
    """``(value, False)`` unchanged if its compact JSON form already fits in
    ``max_bytes``; otherwise a small, explicitly-marked envelope that always fits and is
    always valid JSON, and ``True``.

    An oversized JSON value cannot simply be byte-sliced — cutting a serialized object or
    array mid-structure produces invalid JSON. Instead, the *serialized form* is sliced to
    build a bounded text preview, which is then embedded as an ordinary JSON *string*
    value inside a small fixed envelope — valid JSON regardless of what the preview text
    itself contains, since a JSON string never needs to balance brackets. The preview
    length is chosen, and shrunk if necessary, so the *envelope's own* encoded size
    (accounting for any escaping introduced by re-embedding it as a string) still fits
    ``max_bytes`` — not just the raw slice before escaping.
    """
    data = _compact_json_bytes(value)
    if len(data) <= max_bytes:
        return value, False

    original_bytes = len(data)
    minimal_envelope: dict[str, Any] = {"truncated": True}
    if len(_compact_json_bytes(minimal_envelope)) > max_bytes:
        # max_bytes is smaller than any structured marker can fit in (below 18 bytes) —
        # fall back to the smallest possible valid JSON value there is: a single digit,
        # one byte. It carries no marker text, but it is valid JSON and it fits any
        # budget of at least 1 byte, which a small envelope cannot promise.
        return 0, True

    overhead = len(
        _compact_json_bytes({"truncated": True, "original_bytes": original_bytes, "preview": ""})
    )
    preview_budget = max_bytes - overhead
    if preview_budget <= 0:
        return minimal_envelope, True

    preview_text = data[:preview_budget].decode("utf-8", errors="ignore")
    envelope: dict[str, Any] = {
        "truncated": True,
        "original_bytes": original_bytes,
        "preview": preview_text,
    }
    while len(_compact_json_bytes(envelope)) > max_bytes and envelope["preview"]:
        envelope["preview"] = envelope["preview"][: max(0, len(envelope["preview"]) - 64)]

    if len(_compact_json_bytes(envelope)) > max_bytes:
        return minimal_envelope, True
    return envelope, True
