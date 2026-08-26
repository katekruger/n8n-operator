"""Phase 0 bootstrap checks.

These verify that the repository skeleton itself is coherent. Product behavior is not
implemented yet; the substantive suite arrives with phases 1 onward.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

PACKAGE_MODULES = [
    "n8n_operator",
    "n8n_operator.config",
    "n8n_operator.errors",
    "n8n_operator.core",
    "n8n_operator.core.handles",
    "n8n_operator.core.idempotency",
    "n8n_operator.core.models",
    "n8n_operator.core.redaction",
    "n8n_operator.core.service",
    "n8n_operator.core.state_machine",
    "n8n_operator.registry",
    "n8n_operator.registry.loader",
    "n8n_operator.registry.schema",
    "n8n_operator.registry.validation",
    "n8n_operator.n8n",
    "n8n_operator.n8n.client",
    "n8n_operator.n8n.preflight",
    "n8n_operator.n8n.types",
    "n8n_operator.storage",
    "n8n_operator.storage.models",
    "n8n_operator.storage.repository",
    "n8n_operator.storage.session",
    "n8n_operator.audit",
    "n8n_operator.audit.chain",
    "n8n_operator.audit.writer",
    "n8n_operator.mcp",
    "n8n_operator.mcp.resources",
    "n8n_operator.mcp.server",
    "n8n_operator.mcp.tools",
    "n8n_operator.mcp.transports",
    "n8n_operator.approval",
    "n8n_operator.approval.app",
    "n8n_operator.approval.routes",
    "n8n_operator.cli",
    "n8n_operator.cli.main",
    "n8n_operator.cli.commands",
]

REQUIRED_DOCS = [
    "docs/BUILD_PLAN.md",
    "docs/ARCHITECTURE.md",
    "docs/THREAT_MODEL.md",
    "docs/WORKFLOW_REGISTRY.md",
    "docs/MCP_TOOLS.md",
    "docs/adr/ADR-001-portable-mcp-core.md",
    "docs/adr/ADR-002-default-deny-registry.md",
    "docs/adr/ADR-003-operation-handles.md",
    "docs/adr/ADR-004-sqlite-to-postgres.md",
    "docs/adr/ADR-005-no-automatic-retry-v1.md",
    "docs/adr/ADR-006-server-owned-n8n-credentials.md",
    "docs/adr/ADR-007-deterministic-before-llm.md",
]


@pytest.mark.unit
def test_version_is_exposed() -> None:
    import n8n_operator

    assert n8n_operator.__version__


@pytest.mark.unit
@pytest.mark.parametrize("module_name", PACKAGE_MODULES)
def test_every_module_imports(module_name: str) -> None:
    """The skeleton must import cleanly before anything is built on it."""
    assert importlib.import_module(module_name) is not None


@pytest.mark.unit
@pytest.mark.parametrize("relative_path", REQUIRED_DOCS)
def test_required_documents_exist(relative_path: str) -> None:
    assert (REPO_ROOT / relative_path).is_file()
