"""Contract: the dependency graph points inward only (ADR-001, BUILD_PLAN section 4).

    cli, mcp, approval  ->  core  ->  registry, storage, audit, n8n

``core`` must not import ``mcp``, ``cli``, ``approval``, ``fastapi``, ``typer``, or the
MCP SDK — this is what keeps the domain layer testable without a protocol, a terminal, or
an HTTP framework in the loop, and what lets the MCP adapter be rewritten wholesale
without touching governance logic.

Static AST inspection, not a runtime import: importing ``core`` submodules while later
phases are still stubs would be fragile for reasons unrelated to what this test actually
checks, and a forbidden import should fail this test whether or not the importing module
is otherwise complete enough to run.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "n8n_operator"
CORE = SRC / "core"

FORBIDDEN_FOR_CORE = {
    "n8n_operator.mcp",
    "n8n_operator.cli",
    "n8n_operator.approval",
    "fastapi",
    "typer",
    "mcp",  # the MCP SDK itself, not this package's own n8n_operator.mcp
}

# Capability modules (registry, storage, audit, n8n) must not depend on each other or on
# core — each owns exactly one external concern (ARCHITECTURE.md section 2.1).
CAPABILITY_PACKAGES = ("registry", "storage", "audit", "n8n")


def _imported_module_names(path: Path) -> set[str]:
    """Every module named in an ``import`` or ``from ... import`` statement in ``path``,
    as dotted strings. ``from . import x`` (relative imports) are ignored — they can only
    ever refer to something inside the same package, never to an adapter or a forbidden
    third-party module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # relative import; cannot reach outside n8n_operator
            if node.module:
                names.add(node.module)
    return names


def _violates(imported: str, forbidden: str) -> bool:
    """``imported`` violates ``forbidden`` if it *is* that module or a submodule of it."""
    return imported == forbidden or imported.startswith(forbidden + ".")


def _core_python_files() -> list[Path]:
    return sorted(CORE.rglob("*.py"))


@pytest.mark.contract
@pytest.mark.parametrize("path", _core_python_files(), ids=lambda p: str(p.relative_to(SRC)))
def test_core_imports_nothing_from_an_adapter_or_a_protocol_framework(path: Path) -> None:
    imported = _imported_module_names(path)
    violations = {
        name for name in imported for forbidden in FORBIDDEN_FOR_CORE if _violates(name, forbidden)
    }
    assert not violations, f"{path.relative_to(SRC)} imports forbidden module(s): {violations}"


@pytest.mark.contract
def test_at_least_one_core_module_exists_for_this_test_to_be_meaningful() -> None:
    """A forward-looking regression guard is only as good as there being something to
    check — this fails loudly if ``core/`` were ever emptied out entirely, rather than
    silently passing on zero parametrized cases."""
    assert _core_python_files(), "core/ has no .py files — the layering test above is vacuous"


@pytest.mark.contract
@pytest.mark.parametrize("package_name", CAPABILITY_PACKAGES)
def test_capability_packages_do_not_import_core_or_each_other(package_name: str) -> None:
    package_dir = SRC / package_name
    other_capabilities = {
        f"n8n_operator.{name}" for name in CAPABILITY_PACKAGES if name != package_name
    }
    forbidden = other_capabilities | {"n8n_operator.core"}
    for path in sorted(package_dir.rglob("*.py")):
        imported = _imported_module_names(path)
        violations = {name for name in imported for target in forbidden if _violates(name, target)}
        assert not violations, f"{path.relative_to(SRC)} imports {violations}"


@pytest.mark.contract
def test_adapters_do_not_import_each_other() -> None:
    """``mcp/``, ``cli/``, and ``approval/`` all sit at the same layer over ``core`` —
    none should reach sideways into another adapter (ARCHITECTURE.md section 2.1)."""
    adapters = ("mcp", "cli", "approval")
    for package_name in adapters:
        package_dir = SRC / package_name
        forbidden = {f"n8n_operator.{name}" for name in adapters if name != package_name}
        for path in sorted(package_dir.rglob("*.py")):
            imported = _imported_module_names(path)
            violations = {
                name for name in imported for target in forbidden if _violates(name, target)
            }
            assert not violations, f"{path.relative_to(SRC)} imports {violations}"
