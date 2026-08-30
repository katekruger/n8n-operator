"""The structural diff algorithm (stage 07, ADR-008, MCP_TOOLS.md section 5.6).

A pure function over two already-canonical ``{nodes, connections, settings}``
structures — the exact scope ``n8n.canonicalization.canonical_form`` produces and
``compute_definition_hash`` hashes, so a diff and a drift detection can never disagree
about what changed. No I/O, no redaction, no registry/database access — everything this
module needs is already in its two arguments, which is what makes it independently
testable and deterministic by construction.

**Node identity.** ``nodes`` is matched by each node's own stable ``id`` field, never by
array index or by ``name`` — ``name`` is user-editable and ``connections`` addresses
nodes *by name* (docs/N8N_COMPATIBILITY.md section 6/12), so matching nodes by ``id``
is what turns a rename into a clean ``modified`` entry on the matched node (plus a
separate, honest ``connections``-key change) instead of a false delete-and-add of
unrelated content, and what keeps two same-named nodes (different ids) from ever being
confused with each other.

**Everything else** (``connections``, ``settings``, and each matched node's own
content) is a generic recursive structural diff: dicts compared key-by-key, lists
compared index-by-index (no identity heuristic exists for arbitrary n8n-authored JSON
the way ``id`` exists for the top-level ``nodes`` array — array order is significant
generically, per CAN-04).

Phase 10 (v2) stage 07.
"""

from __future__ import annotations

import json
from typing import Any

from n8n_operator.core.models import DiffEntry

__all__ = ["MAX_DIFF_ENTRIES", "MAX_VALUE_BYTES", "diff_canonical_definitions"]

MAX_DIFF_ENTRIES = 200
"""The "huge diffs" bound: once this many entries have been emitted, the algorithm
stops adding more — the caller (``core.service.diff_workflow_definition``) reports
``truncated: True`` and the real ``total_changes`` count rather than silently returning
a partial list that looks complete."""

MAX_VALUE_BYTES = 4096
"""The "large expressions" bound: a single leaf value larger than this (as its JSON
text) is truncated with a trailing marker rather than embedded whole."""


def _bounded(value: Any) -> tuple[Any, bool]:
    """``(value, truncated)`` — ``value`` unchanged if its JSON text fits
    :data:`MAX_VALUE_BYTES`, else a truncated string preview."""
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = str(value)
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_VALUE_BYTES:
        return value, False
    preview = encoded[:MAX_VALUE_BYTES].decode("utf-8", errors="ignore")
    return f"{preview}… ({len(encoded)} bytes, truncated)", True


class _Budget:
    """Tracks the entry-count cap across the whole recursive walk — a single counter
    threaded through every recursive call, since the cap is global to one diff, not
    per-substructure."""

    def __init__(self) -> None:
        self.entries: list[DiffEntry] = []
        self.total_changes = 0

    def add(self, entry: DiffEntry) -> None:
        self.total_changes += 1
        if len(self.entries) < MAX_DIFF_ENTRIES:
            self.entries.append(entry)

    @property
    def truncated(self) -> bool:
        return self.total_changes > len(self.entries)


def _node_summary(node: dict[str, Any]) -> dict[str, Any]:
    return {"type": node.get("type"), "name": node.get("name")}


def _generic_diff(registered: Any, live: Any, *, path: str, budget: _Budget) -> None:
    """Recursive structural diff for anything with no identity-matching concept
    (``connections``, ``settings``, and each matched node's own content, once the
    top-level ``nodes`` array's own id-matching has already paired things up)."""
    if registered == live:
        return
    if isinstance(registered, dict) and isinstance(live, dict):
        for key in sorted(set(registered) | set(live)):
            child_path = f"{path}/{key}"
            if key not in registered:
                value, truncated = _bounded(live[key])
                budget.add(
                    DiffEntry(
                        path=child_path, change_type="added", live_value=value, truncated=truncated
                    )
                )
            elif key not in live:
                value, truncated = _bounded(registered[key])
                budget.add(
                    DiffEntry(
                        path=child_path,
                        change_type="removed",
                        registered_value=value,
                        truncated=truncated,
                    )
                )
            else:
                _generic_diff(registered[key], live[key], path=child_path, budget=budget)
        return
    if isinstance(registered, list) and isinstance(live, list):
        for index in range(max(len(registered), len(live))):
            child_path = f"{path}/{index}"
            if index >= len(registered):
                value, truncated = _bounded(live[index])
                budget.add(
                    DiffEntry(
                        path=child_path, change_type="added", live_value=value, truncated=truncated
                    )
                )
            elif index >= len(live):
                value, truncated = _bounded(registered[index])
                budget.add(
                    DiffEntry(
                        path=child_path,
                        change_type="removed",
                        registered_value=value,
                        truncated=truncated,
                    )
                )
            else:
                _generic_diff(registered[index], live[index], path=child_path, budget=budget)
        return
    # Scalars that differ, or a dict/list on one side against a different shape on the
    # other — either way, one leaf-level `modified` entry.
    registered_bounded, registered_truncated = _bounded(registered)
    live_bounded, live_truncated = _bounded(live)
    budget.add(
        DiffEntry(
            path=path,
            change_type="modified",
            registered_value=registered_bounded,
            live_value=live_bounded,
            truncated=registered_truncated or live_truncated,
        )
    )


def _diff_nodes(registered_nodes: list[Any], live_nodes: list[Any], *, budget: _Budget) -> None:
    registered_by_id = {n["id"]: (i, n) for i, n in enumerate(registered_nodes) if "id" in n}
    live_by_id = {n["id"]: (i, n) for i, n in enumerate(live_nodes) if "id" in n}

    for node_id in sorted(set(registered_by_id) - set(live_by_id)):
        index, node = registered_by_id[node_id]
        budget.add(
            DiffEntry(path=f"/nodes/{index}", change_type="removed", summary=_node_summary(node))
        )
    for node_id in sorted(set(live_by_id) - set(registered_by_id)):
        index, node = live_by_id[node_id]
        budget.add(
            DiffEntry(path=f"/nodes/{index}", change_type="added", summary=_node_summary(node))
        )
    for node_id in sorted(set(registered_by_id) & set(live_by_id)):
        registered_index, registered_node = registered_by_id[node_id]
        live_index, live_node = live_by_id[node_id]
        before_total = budget.total_changes
        _generic_diff(
            {k: v for k, v in registered_node.items() if k != "id"},
            {k: v for k, v in live_node.items() if k != "id"},
            path=f"/nodes/{live_index}",
            budget=budget,
        )
        content_changed = budget.total_changes != before_total
        if not content_changed and registered_index != live_index:
            # Unchanged content, different position — CAN-04 makes array order
            # hash-significant, so this must still surface as *something* (the
            # "hash differs ⟺ diff is non-empty" invariant), even though nothing
            # about the node itself changed.
            budget.add(
                DiffEntry(
                    path=f"/nodes/{live_index}",
                    change_type="moved",
                    from_index=registered_index,
                    to_index=live_index,
                )
            )


def diff_canonical_definitions(
    registered: dict[str, Any], live: dict[str, Any]
) -> tuple[list[DiffEntry], bool, int]:
    """``(entries, truncated, total_changes)`` — the structural diff between two
    canonical ``{nodes, connections, settings}`` forms. ``entries`` is capped at
    :data:`MAX_DIFF_ENTRIES`; ``total_changes`` is always the real count, so a caller
    can report both an honest total and a bounded list."""
    budget = _Budget()
    _diff_nodes(registered.get("nodes", []), live.get("nodes", []), budget=budget)
    _generic_diff(
        registered.get("connections", {}),
        live.get("connections", {}),
        path="/connections",
        budget=budget,
    )
    _generic_diff(
        registered.get("settings", {}), live.get("settings", {}), path="/settings", budget=budget
    )
    return budget.entries, budget.truncated, budget.total_changes
