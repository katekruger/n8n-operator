"""Architecture-decision records are structured, wired in, and not orphaned.

The doc-consistency checker (check D12) enforces the same properties as part of the whole
documentation sweep. These tests assert them directly so a failure names the ADR rather
than pointing at a checker run, and so the decisions closed in phase 0.1 cannot be quietly
detached from the normative documents that depend on them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"
ADR_DIR = DOCS / "adr"

NORMATIVE_DOCS = [
    DOCS / "BUILD_PLAN.md",
    DOCS / "ARCHITECTURE.md",
    DOCS / "THREAT_MODEL.md",
    DOCS / "WORKFLOW_REGISTRY.md",
    DOCS / "MCP_TOOLS.md",
]

ADRS = [
    "ADR-001-portable-mcp-core.md",
    "ADR-002-default-deny-registry.md",
    "ADR-003-operation-handles.md",
    "ADR-004-sqlite-to-postgres.md",
    "ADR-005-no-automatic-retry-v1.md",
    "ADR-006-server-owned-n8n-credentials.md",
    "ADR-007-deterministic-before-llm.md",
    "ADR-008-conservative-definition-canonicalization.md",
    "ADR-009-dispatch-correlation.md",
    "ADR-010-approval-delivery-and-expiry.md",
    "ADR-011-argument-limits-and-idempotency.md",
    "ADR-012-governed-retry-and-audit-anchoring.md",
    "ADR-013-organization-tenant-and-principal-model.md",
    "ADR-014-oidc-trust-and-session-model.md",
    "ADR-015-rbac-authorization-evaluation.md",
    "ADR-016-environment-registry-overlays.md",
    "ADR-017-team-approval-quorum-semantics.md",
    "ADR-018-notification-and-alert-hook-delivery.md",
    "ADR-019-metrics-cardinality-and-privacy.md",
]

# Decisions closed in phase 0.1, and the identifier each one is obliged to introduce into
# the normative documents. An ADR that does not land its identifier has not been wired in.
PHASE_01_DECISIONS = {
    "ADR-008-conservative-definition-canonicalization.md": "CAN-01",
    "ADR-009-dispatch-correlation.md": "NO_EXECUTION_CORRELATION",
    "ADR-010-approval-delivery-and-expiry.md": "I9",
    "ADR-011-argument-limits-and-idempotency.md": "ARGUMENTS_TOO_LARGE",
    "ADR-012-governed-retry-and-audit-anchoring.md": "I11",
}


def _normative_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in NORMATIVE_DOCS)


@pytest.mark.contract
@pytest.mark.parametrize("adr_name", ADRS)
def test_adr_exists(adr_name: str) -> None:
    assert (ADR_DIR / adr_name).is_file()


@pytest.mark.contract
def test_no_undeclared_adr_files() -> None:
    """A new ADR must be registered here and in the checker, not just dropped in."""
    on_disk = {p.name for p in ADR_DIR.glob("ADR-*.md")}
    assert on_disk == set(ADRS)


@pytest.mark.contract
@pytest.mark.parametrize("adr_name", ADRS)
def test_adr_has_required_structure(adr_name: str) -> None:
    text = (ADR_DIR / adr_name).read_text(encoding="utf-8")
    assert "- **Status:**" in text, "ADR must record a status"
    assert "\n## Context" in text, "ADR must state the context it decides against"
    assert "\n## Decision" in text, "ADR must state a decision"
    assert "\n## Consequences" in text, "ADR must state consequences, including negative ones"
    assert "### Negative" in text, "an ADR with no stated downside has not been thought through"
    assert "\n## Alternatives considered" in text


@pytest.mark.contract
@pytest.mark.parametrize("adr_name", ADRS)
def test_adr_is_referenced_by_a_normative_document(adr_name: str) -> None:
    """No orphaned decisions: every ADR is load-bearing somewhere."""
    assert adr_name[:7] in _normative_text()


@pytest.mark.contract
@pytest.mark.parametrize(("adr_name", "identifier"), sorted(PHASE_01_DECISIONS.items()))
def test_phase_01_decision_landed_its_identifier(adr_name: str, identifier: str) -> None:
    """Each phase-0.1 ADR must have introduced its normative identifier, not just prose."""
    assert identifier in _normative_text(), (
        f"{adr_name} was written but {identifier} never reached a normative document"
    )


@pytest.mark.contract
def test_superseded_error_code_is_confined_to_its_supersession() -> None:
    """`IDEMPOTENCY_KEY_CONFLICT` is superseded by `IDEMPOTENCY_CONFLICT` (ADR-011)."""
    allowed = {"ADR-011-argument-limits-and-idempotency.md", "MCP_TOOLS.md"}
    for path in [*NORMATIVE_DOCS, *(ADR_DIR / name for name in ADRS)]:
        text = path.read_text(encoding="utf-8")
        if "IDEMPOTENCY_KEY_CONFLICT" in text:
            assert path.name in allowed, f"{path.name} uses the superseded spelling"


@pytest.mark.contract
def test_unknown_has_no_outgoing_transition() -> None:
    """ADR-005 and ADR-009: `UNKNOWN` is terminal, and nothing in the plan claims otherwise."""
    plan = (DOCS / "BUILD_PLAN.md").read_text(encoding="utf-8")
    start = plan.index("### 5.2 Transitions")
    stop = plan.index("There are no other transitions")
    for row in plan[start:stop].splitlines():
        cells = [c.strip() for c in row.split("|")]
        if len(cells) > 3 and re.fullmatch(r"T\d\d", cells[1]):
            assert cells[2] != "`UNKNOWN`", f"{cells[1]} leaves UNKNOWN, violating invariant I7"
