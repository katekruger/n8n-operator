"""Contract: ``errors.py`` implements exactly the taxonomy MCP_TOOLS.md section 4 defines.

Parses the normative table directly out of the document (the same technique
``scripts/check_docs_consistency.py`` uses) rather than hand-copying it a second time
into this test — a hand-copied expectation could itself drift from the document and this
test would stop meaning anything.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from n8n_operator.errors import TAXONOMY

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_TOOLS = REPO_ROOT / "docs" / "MCP_TOOLS.md"


def _strip_markdown(text: str) -> str:
    return text.replace("**", "").replace("`", "")


def _parse_taxonomy_table() -> dict[str, tuple[str, bool]]:
    """code -> (remediation, is_v2_only), from the doc table.

    A row whose meaning column starts with the ``*(v2)*`` marker documents a v2 error
    contract fixed at stage 00 (contract closure) but not yet implemented -- v2 stages
    land the corresponding ``errors.py`` class incrementally. A v1 row carries no such
    marker and must already be implemented.
    """
    text = MCP_TOOLS.read_text(encoding="utf-8")
    start = text.index("## 4. Error taxonomy")
    stop = text.index("### 4.1 Error shape", start)
    block = text[start:stop]

    table: dict[str, tuple[str, bool]] = {}
    for line in block.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 3:
            continue
        code = cells[0].strip("`")
        meaning = cells[1]
        remediation = cells[2].strip("*")  # doc bolds "Never retry." etc. with **...**
        table[code] = (remediation, meaning.startswith("*(v2)*"))
    return table


@pytest.fixture(scope="module")
def documented_taxonomy() -> dict[str, tuple[str, bool]]:
    table = _parse_taxonomy_table()
    assert len(table) == 29, f"expected 29 rows, parsed {len(table)} from MCP_TOOLS.md"
    return table


@pytest.mark.contract
def test_errors_py_implements_exactly_the_documented_codes(
    documented_taxonomy: dict[str, tuple[str, bool]],
) -> None:
    """Every implemented code is documented, and every documented v1 code is
    implemented. A documented v2-only code may or may not be implemented yet -- v2
    stages land its ``errors.py`` class when they build the tool that raises it."""
    documented_codes = set(documented_taxonomy.keys())
    v1_codes = {code for code, (_, is_v2) in documented_taxonomy.items() if not is_v2}
    assert set(TAXONOMY.keys()) <= documented_codes, (
        f"errors.py implements undocumented code(s): {set(TAXONOMY.keys()) - documented_codes}"
    )
    assert v1_codes <= set(TAXONOMY.keys()), (
        f"errors.py is missing documented v1 code(s): {v1_codes - set(TAXONOMY.keys())}"
    )


@pytest.mark.contract
@pytest.mark.parametrize(
    "code",
    sorted(code for code in _parse_taxonomy_table() if code in TAXONOMY),
)
def test_remediation_matches_the_documented_next_move(
    code: str, documented_taxonomy: dict[str, tuple[str, bool]]
) -> None:
    cls = TAXONOMY[code]
    remediation_doc, _ = documented_taxonomy[code]
    documented = _strip_markdown(remediation_doc)
    remediation = _strip_markdown(cls.remediation)
    # The doc's markdown emphasis/backticks are stripped; the underlying sentence must
    # still match exactly, since this text is what a model reads to decide what to do.
    assert remediation == documented, f"{code}: {remediation!r} != {documented!r}"


@pytest.mark.contract
def test_no_error_code_appears_outside_the_taxonomy_module() -> None:
    """A code invented ad hoc elsewhere in the source tree, never routed through
    ``errors.py``, would defeat the entire point of having one taxonomy."""
    documented = set(_parse_taxonomy_table().keys())
    src = REPO_ROOT / "src" / "n8n_operator"
    pattern = re.compile(r'"([A-Z][A-Z_]{6,})"')
    for path in src.rglob("*.py"):
        if path.name == "errors.py":
            continue
        text = path.read_text(encoding="utf-8")
        for match in pattern.findall(text):
            if match in documented:
                pytest.fail(
                    f"{path.relative_to(REPO_ROOT)} references {match!r} directly; "
                    f"raise the errors.py exception class instead"
                )


@pytest.mark.contract
def test_superseded_idempotency_key_conflict_spelling_is_absent_from_source() -> None:
    """ADR-011 supersedes IDEMPOTENCY_KEY_CONFLICT with IDEMPOTENCY_CONFLICT. The old
    spelling must not appear anywhere in the implementation."""
    src = REPO_ROOT / "src" / "n8n_operator"
    for path in src.rglob("*.py"):
        assert "IDEMPOTENCY_KEY_CONFLICT" not in path.read_text(encoding="utf-8"), path
