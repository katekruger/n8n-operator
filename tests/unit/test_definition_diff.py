"""``core.definition_diff.diff_canonical_definitions`` (stage 07, ADR-008) — the pure
structural diff algorithm, tested in complete isolation from I/O, redaction, and the
registry (its two arguments are already-canonical ``{nodes, connections, settings}``
structures, exactly what ``n8n.canonicalization.canonical_form`` produces).
"""

from __future__ import annotations

from typing import Any

from n8n_operator.core.definition_diff import (
    MAX_DIFF_ENTRIES,
    MAX_VALUE_BYTES,
    diff_canonical_definitions,
)


def _node(
    node_id: str, name: str, *, node_type: str = "n8n-nodes-base.set", **params: Any
) -> dict[str, Any]:
    return {"id": node_id, "name": name, "type": node_type, "parameters": params}


def _base() -> dict[str, Any]:
    return {
        "nodes": [
            _node("n1", "Webhook", node_type="n8n-nodes-base.webhook"),
            _node("n2", "Set", url="old"),
        ],
        "connections": {"Webhook": {"main": [[{"node": "Set", "type": "main", "index": 0}]]}},
        "settings": {"timezone": "UTC"},
    }


def test_identical_structures_produce_no_entries() -> None:
    base = _base()
    entries, truncated, total = diff_canonical_definitions(base, base)
    assert entries == []
    assert truncated is False
    assert total == 0


def test_a_parameter_change_is_a_single_modified_entry() -> None:
    registered = _base()
    live = _base()
    live["nodes"][1]["parameters"]["url"] = "new"
    entries, _, total = diff_canonical_definitions(registered, live)
    assert total == 1
    assert entries[0].path == "/nodes/1/parameters/url"
    assert entries[0].change_type == "modified"
    assert entries[0].registered_value == "old"
    assert entries[0].live_value == "new"


def test_node_added_is_reported_with_a_bounded_summary_not_the_full_body() -> None:
    registered = _base()
    live = _base()
    live["nodes"].append(_node("n3", "New", secret_param="should-not-appear"))
    entries, _, total = diff_canonical_definitions(registered, live)
    assert total == 1
    assert entries[0].change_type == "added"
    assert entries[0].path == "/nodes/2"
    assert entries[0].summary == {"type": "n8n-nodes-base.set", "name": "New"}
    assert entries[0].live_value is None  # never the full node body


def test_node_removed_is_reported_with_a_bounded_summary() -> None:
    registered = _base()
    live = _base()
    live["nodes"].pop()
    entries, _, total = diff_canonical_definitions(registered, live)
    assert total == 1
    assert entries[0].change_type == "removed"
    assert entries[0].path == "/nodes/1"
    assert entries[0].summary == {"type": "n8n-nodes-base.set", "name": "Set"}


def test_node_reordering_with_no_content_change_is_reported_as_moved() -> None:
    registered = _base()
    live = {
        **registered,
        "nodes": [registered["nodes"][1], registered["nodes"][0]],
    }
    entries, _, total = diff_canonical_definitions(registered, live)
    assert total == 2  # both nodes moved
    change_types = {e.change_type for e in entries}
    assert change_types == {"moved"}
    by_path = {e.path: e for e in entries}
    assert by_path["/nodes/0"].from_index == 1
    assert by_path["/nodes/0"].to_index == 0
    assert by_path["/nodes/1"].from_index == 0
    assert by_path["/nodes/1"].to_index == 1


def test_duplicate_node_names_are_matched_by_id_never_confused() -> None:
    registered = {
        "nodes": [_node("a", "Set", value="1"), _node("b", "Set", value="2")],
        "connections": {},
        "settings": {},
    }
    live = {
        "nodes": [_node("a", "Set", value="1"), _node("b", "Set", value="99")],
        "connections": {},
        "settings": {},
    }
    entries, _, total = diff_canonical_definitions(registered, live)
    assert total == 1
    assert entries[0].path == "/nodes/1/parameters/value"
    assert entries[0].registered_value == "2"
    assert entries[0].live_value == "99"


def test_renamed_node_is_a_name_modification_plus_the_connections_key_change() -> None:
    registered = _base()
    live = _base()
    live["nodes"][1]["name"] = "SetRenamed"
    live["connections"] = {
        "Webhook": {"main": [[{"node": "SetRenamed", "type": "main", "index": 0}]]}
    }
    entries, _, total = diff_canonical_definitions(registered, live)
    paths = {e.path for e in entries}
    assert "/nodes/1/name" in paths
    assert "/connections/Webhook/main/0/0/node" in paths
    assert total == 2
    # Never a false delete+add of the whole node.
    assert not any(e.change_type in {"added", "removed"} for e in entries)


def test_connections_key_order_change_is_a_generic_added_removed_pair() -> None:
    registered = {
        "nodes": [],
        "connections": {"A": {"main": [[]]}},
        "settings": {},
    }
    live = {
        "nodes": [],
        "connections": {"B": {"main": [[]]}},
        "settings": {},
    }
    entries, _, total = diff_canonical_definitions(registered, live)
    assert total == 2
    change_types = {(e.path, e.change_type) for e in entries}
    assert ("/connections/A", "removed") in change_types
    assert ("/connections/B", "added") in change_types


def test_settings_change_is_a_leaf_level_modified_entry() -> None:
    registered = _base()
    live = _base()
    live["settings"]["timezone"] = "America/New_York"
    entries, _, total = diff_canonical_definitions(registered, live)
    assert total == 1
    assert entries[0].path == "/settings/timezone"
    assert entries[0].change_type == "modified"


def test_unrecognized_field_still_surfaces_as_a_real_diff() -> None:
    """CAN-01: inclusion by default — a field this algorithm has never seen before
    (a hypothetical future n8n addition) must still be treated as content, never
    silently dropped."""
    registered = _base()
    live = _base()
    live["nodes"][1]["someFutureField"] = {"nested": "value"}
    entries, _, total = diff_canonical_definitions(registered, live)
    assert total == 1
    assert entries[0].path == "/nodes/1/someFutureField"
    assert entries[0].change_type == "added"


def test_unicode_values_diff_correctly() -> None:
    registered = _base()
    live = _base()
    live["nodes"][1]["parameters"]["url"] = "héllo wörld 日本語 🎉"
    entries, _, total = diff_canonical_definitions(registered, live)
    assert total == 1
    assert entries[0].live_value == "héllo wörld 日本語 🎉"


def test_a_connections_cycle_diffs_cleanly_with_no_traversal_error() -> None:
    """A cycle in the *graph* connections describe is not a cycle in the JSON value
    itself — this walks two finite trees, never "the graph"."""
    registered = {
        "nodes": [],
        "connections": {
            "A": {"main": [[{"node": "B", "type": "main", "index": 0}]]},
            "B": {"main": [[{"node": "A", "type": "main", "index": 0}]]},
        },
        "settings": {},
    }
    live = dict(registered)
    entries, truncated, total = diff_canonical_definitions(registered, live)
    assert entries == []
    assert truncated is False
    assert total == 0


def test_large_expression_values_are_bounded_and_marked_truncated() -> None:
    registered = _base()
    live = _base()
    live["nodes"][1]["parameters"]["url"] = "x" * (MAX_VALUE_BYTES * 2)
    entries, _, total = diff_canonical_definitions(registered, live)
    assert total == 1
    assert entries[0].truncated is True
    assert len(entries[0].live_value.encode("utf-8")) <= MAX_VALUE_BYTES + 100


def test_huge_diffs_are_capped_but_total_changes_is_still_honest() -> None:
    registered: dict[str, Any] = {"nodes": [], "connections": {}, "settings": {}}
    live: dict[str, Any] = {
        "nodes": [],
        "connections": {},
        "settings": {str(i): i for i in range(MAX_DIFF_ENTRIES + 50)},
    }
    entries, truncated, total = diff_canonical_definitions(registered, live)
    assert len(entries) == MAX_DIFF_ENTRIES
    assert truncated is True
    assert total == MAX_DIFF_ENTRIES + 50


def test_missing_node_id_is_never_matched_and_never_crashes() -> None:
    """A node lacking an ``id`` (not expected from a real n8n instance, but the
    algorithm must not crash on it) is simply excluded from identity-matching —
    neither an add nor a remove nor a crash."""
    registered = {"nodes": [{"name": "NoId"}], "connections": {}, "settings": {}}
    live = {"nodes": [{"name": "NoId"}], "connections": {}, "settings": {}}
    entries, _, total = diff_canonical_definitions(registered, live)
    assert entries == []
    assert total == 0
