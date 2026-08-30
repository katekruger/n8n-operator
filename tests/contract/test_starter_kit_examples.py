"""Stage 10 completion gate: every configuration and command the GTM onboarding docs
reference is real, not aspirational — the starter-kit registry loads cleanly through
the actual loader (never a subprocess), contains no live-looking secrets, every
``n8n-operator <group> ...`` command named in the new onboarding docs uses a group the
CLI actually registers, and every workflow ID cross-referenced between
``GTM_STARTER_KITS.md`` and the registry YAML exists on both sides.
"""

from __future__ import annotations

import re
from pathlib import Path

from n8n_operator.registry.loader import load_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
STARTER_KIT_REGISTRY = REPO_ROOT / "examples/registry/starter-kits/gtm-starter-kits.yaml"

# The real top-level command groups registered in cli/main.py, plus the one
# app.command() (not add_typer) entry point.
_REAL_CLI_GROUPS = {
    "db",
    "registry",
    "serve",
    "operations",
    "audit",
    "identity",
    "environment",
    "notifications",
    "metrics",
    "anchor",
    "health",
}

_ONBOARDING_DOCS = [
    REPO_ROOT / "docs/GTM_STARTER_KITS.md",
    REPO_ROOT / "docs/OPERATOR_GUIDE.md",
    REPO_ROOT / "docs/APPROVER_GUIDE.md",
    REPO_ROOT / "docs/TROUBLESHOOTING.md",
    REPO_ROOT / "docs/LEAST_PRIVILEGE.md",
    REPO_ROOT / "docs/MCP_CLIENT_RECIPES.md",
]

_BASH_FENCE = re.compile(r"```bash\n(.*?)```", re.DOTALL)
_CLI_INVOCATION = re.compile(r"\bn8n-operator\s+([a-z][a-z0-9_-]*)")


def test_starter_kit_registry_loads_with_zero_violations() -> None:
    load_registry(STARTER_KIT_REGISTRY, server_max_argument_bytes=262_144)


def test_starter_kit_registry_contains_no_live_looking_secrets() -> None:
    text = STARTER_KIT_REGISTRY.read_text()
    assert "sk-" not in text
    for line in text.splitlines():
        if line.strip().startswith("secret_ref:"):
            assert "env:" in line or "keyring:" in line, line


def test_onboarding_docs_only_reference_real_cli_command_groups() -> None:
    for doc in _ONBOARDING_DOCS:
        text = doc.read_text()
        for block in _BASH_FENCE.findall(text):
            for match in _CLI_INVOCATION.finditer(block):
                group = match.group(1)
                assert group in _REAL_CLI_GROUPS, (
                    f"{doc.name} references unknown command group {group!r} "
                    f"(real groups: {sorted(_REAL_CLI_GROUPS)})"
                )


_WORKFLOW_ID_PATTERN = re.compile(r"`([a-z][a-z0-9_]*\.[a-z][a-z0-9_]*)`")


def test_gtm_starter_kits_doc_and_registry_agree_on_workflow_ids() -> None:
    loaded = load_registry(STARTER_KIT_REGISTRY, server_max_argument_bytes=262_144)
    registry_ids = {entry.id for entry in loaded.entries}
    doc_text = (REPO_ROOT / "docs/GTM_STARTER_KITS.md").read_text()

    # Every registry entry is mentioned by ID at least once in the doc — catches a
    # newly-added workflow nobody documented.
    for workflow_id in registry_ids:
        assert workflow_id in doc_text, f"{workflow_id} is in the registry but not documented"

    # Every doc-referenced, registry-ID-shaped (namespace.name) backtick span that
    # matches an entry in this same registry's own namespaces is a real workflow ID,
    # not a typo — catches an orphaned or renamed doc reference.
    doc_ids = set(_WORKFLOW_ID_PATTERN.findall(doc_text))
    known_namespaces = {workflow_id.split(".", 1)[0] for workflow_id in registry_ids}
    for candidate in doc_ids:
        namespace = candidate.split(".", 1)[0]
        if namespace in known_namespaces:
            assert candidate in registry_ids, (
                f"{candidate!r} looks like a starter-kit workflow ID but is not in "
                f"{STARTER_KIT_REGISTRY.name}"
            )
