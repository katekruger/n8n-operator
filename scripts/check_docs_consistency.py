#!/usr/bin/env python3
"""Verify that the documentation set is internally consistent.

BUILD_PLAN.md is normative (its own preamble says so). This checker enforces that the
other documents agree with it, and that the repository tree it publishes matches the
filesystem. It is run by CI and by ``tests/contract/test_docs_consistency.py``.

Checks
------
D1  Every required document exists.
D2  The twelve operation states are exactly those in BUILD_PLAN section 5.1, and every
    state name used in any document is one of them.
D3  Transitions T01-T15 are each defined exactly once, and every referenced transition
    is defined.
D4  The v1 tool inventory is consistent: the count claimed in the heading matches the
    table, MCP_TOOLS.md documents exactly those tools, and no tool is invented elsewhere.
D5  v2 and v3 inventory arithmetic holds (12 + 8 = 20, 20 + 8 = 28).
D6  Acceptance criteria AC-01..AC-25 are each defined once, and every reference resolves.
D7  Invariants I1-I8, boundary controls B1-B11, and registry rules R1-R10 are each
    defined, and every reference resolves.
D8  Every relative Markdown link between documents resolves to a real file.
D9  The repository tree published in BUILD_PLAN section 4 matches the filesystem.

Exit code 0 means consistent; 1 means at least one contradiction, printed to stdout.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS = REPO_ROOT / "docs"

BUILD_PLAN = DOCS / "BUILD_PLAN.md"
ARCHITECTURE = DOCS / "ARCHITECTURE.md"
THREAT_MODEL = DOCS / "THREAT_MODEL.md"
WORKFLOW_REGISTRY = DOCS / "WORKFLOW_REGISTRY.md"
MCP_TOOLS = DOCS / "MCP_TOOLS.md"

ADRS = [
    "ADR-001-portable-mcp-core.md",
    "ADR-002-default-deny-registry.md",
    "ADR-003-operation-handles.md",
    "ADR-004-sqlite-to-postgres.md",
    "ADR-005-no-automatic-retry-v1.md",
    "ADR-006-server-owned-n8n-credentials.md",
    "ADR-007-deterministic-before-llm.md",
]

REQUIRED_DOCS = [BUILD_PLAN, ARCHITECTURE, THREAT_MODEL, WORKFLOW_REGISTRY, MCP_TOOLS] + [
    DOCS / "adr" / name for name in ADRS
]

EXPECTED_STATES = {
    "PREPARING",
    "INVALID",
    "BLOCKED",
    "PENDING_APPROVAL",
    "APPROVED",
    "REJECTED",
    "EXPIRED",
    "CANCELED",
    "EXECUTING",
    "SUCCEEDED",
    "FAILED",
    "UNKNOWN",
}

EXPECTED_V1_TOOLS = {
    "list_workflows",
    "describe_workflow",
    "get_instance_health",
    "validate_input",
    "preflight_workflow",
    "prepare_operation",
    "get_operation",
    "execute_operation",
    "cancel_operation",
    "list_operations",
    "get_execution_result",
    "get_execution_log",
}

EXPECTED_V2_TOOLS = {
    "whoami",
    "list_environments",
    "request_approval",
    "get_approval_status",
    "retry_operation",
    "diff_workflow_definition",
    "get_metrics",
    "list_audit_events",
}

EXPECTED_V3_TOOLS = {
    "compile_workflow",
    "plan_workflow_change",
    "apply_workflow_change",
    "run_evaluation",
    "get_evaluation_report",
    "suggest_remediation",
    "list_templates",
    "instantiate_template",
}

failures: list[str] = []


def fail(check: str, message: str) -> None:
    failures.append(f"[{check}] {message}")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def section(text: str, start: str, end: str | None) -> str:
    """Return the body between two headings."""
    begin = text.index(start)
    stop = text.index(end, begin) if end else len(text)
    return text[begin:stop]


# --------------------------------------------------------------------------- D1
for doc in REQUIRED_DOCS:
    if not doc.is_file():
        fail("D1", f"missing required document: {doc.relative_to(REPO_ROOT)}")

if failures:
    print("\n".join(failures))
    sys.exit(1)

plan = read(BUILD_PLAN)
all_docs = {path: read(path) for path in REQUIRED_DOCS}

# --------------------------------------------------------------------------- D2
states_block = section(plan, "### 5.1 States", "### 5.2 Transitions")
declared_states = set(re.findall(r"^\| `([A-Z_]+)` \|", states_block, re.MULTILINE))

if declared_states != EXPECTED_STATES:
    fail(
        "D2",
        f"BUILD_PLAN 5.1 states {sorted(declared_states)} != expected {sorted(EXPECTED_STATES)}",
    )
if len(declared_states) != 12:
    fail("D2", f"expected 12 states, BUILD_PLAN 5.1 declares {len(declared_states)}")

diagram = section(plan, "```mermaid", "```\n\n### 5.4")
diagram_states = set(re.findall(r"\b([A-Z][A-Z_]{3,})\b", diagram)) - {"BUILD", "PLAN"}
if diagram_states != declared_states:
    missing = declared_states - diagram_states
    extra = diagram_states - declared_states
    fail("D2", f"state diagram mismatch: missing={sorted(missing)} extra={sorted(extra)}")

# No document may use a state name that is not declared.
state_like = re.compile(
    r"`(PREPARING|INVALID|BLOCKED|PENDING_[A-Z_]+|APPROVED|REJECTED|"
    r"EXPIRED|CANCELED|CANCELLED|EXECUTING|SUCCEEDED|SUCCESS|FAILED|UNKNOWN)`"
)
for path, text in all_docs.items():
    for used in set(state_like.findall(text)):
        if used not in declared_states:
            fail("D2", f"{path.name} uses undeclared state `{used}`")

# --------------------------------------------------------------------------- D3
transitions_block = section(plan, "### 5.2 Transitions", "There are no other transitions")
defined_transitions = re.findall(r"^\| (T\d\d) \|", transitions_block, re.MULTILINE)
if len(defined_transitions) != len(set(defined_transitions)):
    fail("D3", "duplicate transition IDs in BUILD_PLAN 5.2")
expected_transitions = {f"T{n:02d}" for n in range(1, 16)}
if set(defined_transitions) != expected_transitions:
    fail(
        "D3",
        f"transitions {sorted(set(defined_transitions))} != expected T01-T15",
    )

for path, text in all_docs.items():
    for used in set(re.findall(r"\bT(\d\d)\b(?!-)", text)):
        if f"T{used}" not in expected_transitions:
            fail("D3", f"{path.name} references undefined transition T{used}")

# --------------------------------------------------------------------------- D4
v1_block = section(plan, "### 7.1 v1 — 12 tools", "### 7.2")
v1_tools = set(re.findall(r"^\| `([a-z_]+)` \|", v1_block, re.MULTILINE))
if v1_tools != EXPECTED_V1_TOOLS:
    fail("D4", f"BUILD_PLAN 7.1 tools {sorted(v1_tools)} != expected {sorted(EXPECTED_V1_TOOLS)}")

heading_count = re.search(r"### 7\.1 v1 — (\d+) tools", plan)
if not heading_count or int(heading_count.group(1)) != len(v1_tools):
    fail("D4", "BUILD_PLAN 7.1 heading count disagrees with its own table")

mcp_text = all_docs[MCP_TOOLS]
documented = set(re.findall(r"^### 2\.\d+ `([a-z_]+)`", mcp_text, re.MULTILINE))
if documented != EXPECTED_V1_TOOLS:
    fail(
        "D4",
        f"MCP_TOOLS documents {sorted(documented)}; BUILD_PLAN 7.1 lists "
        f"{sorted(EXPECTED_V1_TOOLS)}",
    )

# --------------------------------------------------------------------------- D5
v2_block = section(plan, "### 7.2 v2", "### 7.3")
v2_tools = set(re.findall(r"^\| `([a-z_]+)` \|", v2_block, re.MULTILINE))
if v2_tools != EXPECTED_V2_TOOLS:
    fail("D5", f"BUILD_PLAN 7.2 tools {sorted(v2_tools)} != expected {sorted(EXPECTED_V2_TOOLS)}")

v3_block = section(plan, "### 7.3 v3", "\n---\n")  # a horizontal rule, not a table separator
v3_tools = set(re.findall(r"^\| `([a-z_]+)` \|", v3_block, re.MULTILINE))
if v3_tools != EXPECTED_V3_TOOLS:
    fail("D5", f"BUILD_PLAN 7.3 tools {sorted(v3_tools)} != expected {sorted(EXPECTED_V3_TOOLS)}")

v2_head = re.search(r"### 7\.2 v2 — adds (\d+) tools \((\d+) total\)", plan)
v3_head = re.search(r"### 7\.3 v3 — adds (\d+) tools \((\d+) total\)", plan)
if not v2_head or not v3_head:
    fail("D5", "v2/v3 tool-count headings not found in the expected form")
else:
    v2_added, v2_total = int(v2_head.group(1)), int(v2_head.group(2))
    v3_added, v3_total = int(v3_head.group(1)), int(v3_head.group(2))
    if v2_added != len(v2_tools) or v3_added != len(v3_tools):
        fail("D5", "v2/v3 heading counts disagree with their tables")
    if v2_total != len(v1_tools) + v2_added:
        fail("D5", f"v2 total {v2_total} != {len(v1_tools)} + {v2_added}")
    if v3_total != v2_total + v3_added:
        fail("D5", f"v3 total {v3_total} != {v2_total} + {v3_added}")

# Tools must not be invented outside the inventory.
known_tools = EXPECTED_V1_TOOLS | EXPECTED_V2_TOOLS | EXPECTED_V3_TOOLS
tool_call = re.compile(r"`([a-z][a-z_]{4,})\(\)?`")
allowed_non_tools = {"create_all", "additional_properties", "registry_snapshot"}
for path, text in all_docs.items():
    for candidate in set(tool_call.findall(text)):
        if candidate in known_tools or candidate in allowed_non_tools:
            continue
        if re.search(rf"`{candidate}`\s*(tool|MCP)", text):
            fail(
                "D4",
                f"{path.name} refers to `{candidate}` as a tool, but it is not in the inventory",
            )

# --------------------------------------------------------------------------- D6
ac_block = section(plan, "## 11. Acceptance criteria", "## 12. Progress checklist")
defined_ac = set(re.findall(r"\*\*(AC-\d\d)\*\*", ac_block))
expected_ac = {f"AC-{n:02d}" for n in range(1, 26)}
if defined_ac != expected_ac:
    fail("D6", f"acceptance criteria {sorted(defined_ac)} != expected AC-01..AC-25")

for path, text in all_docs.items():
    for used in set(re.findall(r"\b(AC-\d\d)\b", text)):
        if used not in expected_ac:
            fail("D6", f"{path.name} references undefined {used}")

# --------------------------------------------------------------------------- D7
inv_block = section(plan, "### 5.4 Invariants", "## 6. Workflow registry schema")
defined_inv = set(re.findall(r"\*\*(I\d)\*\*", inv_block))
expected_inv = {f"I{n}" for n in range(1, 9)}
if defined_inv != expected_inv:
    fail("D7", f"invariants {sorted(defined_inv)} != expected I1-I8")

b_block = section(plan, "### 9.2 Boundary controls", "### 9.3")
defined_b = set(re.findall(r"^\| (B\d+) \|", b_block, re.MULTILINE))
expected_b = {f"B{n}" for n in range(1, 12)}
if defined_b != expected_b:
    fail("D7", f"boundary controls {sorted(defined_b)} != expected B1-B11")

r_block = section(plan, "### 6.6 Load-time validation rules", "### 6.7 Snapshots")
defined_r = set(re.findall(r"^\| (R\d+) \|", r_block, re.MULTILINE))
expected_r = {f"R{n}" for n in range(1, 11)}
if defined_r != expected_r:
    fail("D7", f"registry rules {sorted(defined_r)} != expected R1-R10")

for path, text in all_docs.items():
    for used in set(re.findall(r"\b(?:invariant |invariants )(I\d+)\b", text)):
        if used not in expected_inv:
            fail("D7", f"{path.name} references undefined invariant {used}")
    for used in set(re.findall(r"\b(?:boundary |boundaries )(B\d+)\b", text)):
        if used not in expected_b:
            fail("D7", f"{path.name} references undefined boundary control {used}")
    for used in set(re.findall(r"\brule (R\d+)\b", text)):
        if used not in expected_r:
            fail("D7", f"{path.name} references undefined registry rule {used}")

# Threat IDs referenced across documents must be defined in THREAT_MODEL.
threat_text = all_docs[THREAT_MODEL]
defined_threats = set(re.findall(r"^\| (T-\d\d) \|", threat_text, re.MULTILINE))
defined_threats |= set(re.findall(r"^\| (L-\d\d) \|", threat_text, re.MULTILINE))
for path, text in all_docs.items():
    for used in set(re.findall(r"\b([TL]-\d\d)\b", text)):
        if used not in defined_threats:
            fail("D7", f"{path.name} references undefined threat {used}")

# --------------------------------------------------------------------------- D8
link_pattern = re.compile(r"\[[^\]]+\]\(([^)#:]+\.md)(?:#[^)]*)?\)")
for path, text in all_docs.items():
    for target in set(link_pattern.findall(text)):
        resolved = (path.parent / target).resolve()
        if not resolved.is_file():
            fail("D8", f"{path.name} links to missing file: {target}")

# --------------------------------------------------------------------------- D9
tree_block = section(plan, "## 4. Repository structure", "**Layering rule")
lines = tree_block.splitlines()
try:
    start = next(i for i, line in enumerate(lines) if line.strip() == "```") + 1
    stop = next(i for i, line in enumerate(lines[start:], start) if line.strip() == "```")
except StopIteration:  # pragma: no cover - structural guard
    fail("D9", "could not locate the repository tree code block")
    lines, start, stop = [], 0, 0

entry_pattern = re.compile(r"^((?:(?:│   )|(?:    ))*)(?:├── |└── )(\S+?)/?$")
declared_paths: list[tuple[Path, bool]] = []
stack: dict[int, str] = {}

for raw in lines[start:stop]:
    line = raw.split("#")[0].rstrip()
    if not line or line.startswith("n8n-operator/"):
        continue
    match = entry_pattern.match(line)
    if not match:
        continue
    depth = len(match.group(1)) // 4
    name = match.group(2)
    is_dir = raw.split("#")[0].rstrip().endswith("/")
    stack[depth] = name
    parts = [stack[d] for d in range(depth + 1)]
    declared_paths.append((REPO_ROOT.joinpath(*parts), is_dir))

if not declared_paths:
    fail("D9", "parsed no entries from the repository tree")

for path, is_dir in declared_paths:
    if is_dir and not path.is_dir():
        fail("D9", f"tree declares directory that does not exist: {path.relative_to(REPO_ROOT)}")
    elif not is_dir and not path.exists():
        fail("D9", f"tree declares path that does not exist: {path.relative_to(REPO_ROOT)}")

# The reverse direction, for the two trees the document claims to describe exhaustively.
declared_set = {path.resolve() for path, _ in declared_paths}
for root, suffix in ((REPO_ROOT / "src", ".py"), (DOCS, ".md")):
    for actual in root.rglob(f"*{suffix}"):
        if actual.resolve() not in declared_set:
            fail(
                "D9",
                f"file exists but is absent from the BUILD_PLAN tree: "
                f"{actual.relative_to(REPO_ROOT)}",
            )

# --------------------------------------------------------------------------- report
if failures:
    print(f"Documentation consistency: {len(failures)} problem(s)\n")
    print("\n".join(failures))
    sys.exit(1)

print("Documentation consistency: OK")
print(f"  states       12  ({', '.join(sorted(declared_states))})")
print(f"  transitions  {len(defined_transitions)}  (T01-T15)")
print(f"  v1 tools     {len(v1_tools)}")
print(f"  v2 tools     {len(v2_tools)} (20 total)")
print(f"  v3 tools     {len(v3_tools)} (28 total)")
print(f"  criteria     {len(defined_ac)}  (AC-01-AC-25)")
print(f"  invariants   {len(defined_inv)}  boundaries {len(defined_b)}  rules {len(defined_r)}")
print(f"  tree entries {len(declared_paths)} verified against the filesystem")
sys.exit(0)
