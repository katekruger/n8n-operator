"""Hypothesis properties for ``core/redaction.py`` (BUILD_PLAN section 10.2, phase 3):

- Redaction totality: for any payload and any registered redaction path, no redacted
  value appears anywhere in the serialized output, including nested and array positions
  (AC-19).
- Secret non-leakage: for any tool result and any configured secret value, the secret
  does not appear in the serialization (boundary B5).
- ``cap_output`` always produces valid, budget-respecting JSON.
"""

from __future__ import annotations

import json
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from n8n_operator.core.redaction import cap_output, redact, scrub_secrets

SAFE_TEXT = st.text(alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E), max_size=20)

_json_leaf = st.none() | st.booleans() | st.integers(min_value=-1000, max_value=1000) | SAFE_TEXT
_json_value = st.recursive(
    _json_leaf,
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(
            st.text(alphabet="abcdefgh", min_size=1, max_size=6), children, max_size=4
        )
    ),
    max_leaves=15,
)

# --------------------------------------------------------------------------------------
# Redaction totality (AC-19)
# --------------------------------------------------------------------------------------


@given(
    secret_value=st.text(alphabet="abcdefghijklmnop", min_size=6, max_size=12),
    wrapper=st.sampled_from(["flat", "nested", "in_list", "in_list_of_dicts"]),
)
@settings(max_examples=100)
def test_a_redacted_value_never_appears_in_the_output_at_any_position(
    secret_value: str, wrapper: str
) -> None:
    data: dict[str, Any]
    if wrapper == "flat":
        data = {"secret": secret_value}
        paths = ["$.secret"]
    elif wrapper == "nested":
        data = {"a": {"b": {"secret": secret_value}}}
        paths = ["$.a.b.secret"]
    elif wrapper == "in_list":
        data = {"items": [secret_value, secret_value, "other"]}
        paths = ["$.items[*]"]
    else:
        data = {"records": [{"secret": secret_value}, {"secret": secret_value}]}
        paths = ["$.records[*].secret"]

    result = redact(data, paths)
    serialized = json.dumps(result)
    assert secret_value not in serialized


@given(value=_json_value)
@settings(max_examples=50)
def test_redact_with_no_paths_never_changes_the_value(value: object) -> None:
    assert redact(value, []) == value


@given(value=_json_value)
@settings(max_examples=50)
def test_redact_never_mutates_its_input(value: object) -> None:
    import copy

    original = copy.deepcopy(value)
    redact(value, ["$..nonexistent"])
    assert value == original


# --------------------------------------------------------------------------------------
# Secret non-leakage (boundary B5)
# --------------------------------------------------------------------------------------


@given(
    secret=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=8, max_size=24),
    prefix=SAFE_TEXT,
    suffix=SAFE_TEXT,
)
@settings(max_examples=100)
def test_a_configured_secret_never_appears_in_the_scrubbed_output(
    secret: str, prefix: str, suffix: str
) -> None:
    data = {
        "message": f"{prefix}{secret}{suffix}",
        "nested": {"deep": [f"token={secret}", "clean"]},
    }
    result = scrub_secrets(data, [secret])
    serialized = json.dumps(result)
    assert secret not in serialized


@given(value=_json_value, secrets=st.lists(st.text(min_size=1, max_size=10), max_size=3))
@settings(max_examples=50)
def test_scrub_secrets_never_crashes_on_arbitrary_json_shapes(
    value: object, secrets: list[str]
) -> None:
    result = scrub_secrets(value, secrets)
    json.dumps(result)  # must remain JSON-serializable


# --------------------------------------------------------------------------------------
# cap_output: always valid JSON, always within budget
# --------------------------------------------------------------------------------------


@given(value=_json_value, max_bytes=st.integers(min_value=1, max_value=2000))
@settings(max_examples=100)
def test_cap_output_always_produces_valid_json_within_the_byte_budget(
    value: object, max_bytes: int
) -> None:
    result, _truncated = cap_output(value, max_bytes=max_bytes)
    encoded = json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert len(encoded) <= max_bytes
    json.loads(json.dumps(result))  # round-trips as valid JSON


@given(value=_json_value, max_bytes=st.integers(min_value=1, max_value=2000))
@settings(max_examples=100)
def test_cap_output_marks_truncation_consistently_with_whether_it_changed_the_value(
    value: object, max_bytes: int
) -> None:
    result, truncated = cap_output(value, max_bytes=max_bytes)
    if not truncated:
        assert result == value
    else:
        assert (isinstance(result, dict) and result.get("truncated") is True) or result == 0
