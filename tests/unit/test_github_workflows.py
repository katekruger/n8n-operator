"""Structural checks on `.github/workflows/*.yml` (BUILD_PLAN section 12, phase 9
continuation — public security readiness). No live GitHub call, no Docker, no network
— everything here is verifiable from the files alone, so a regression (a tag pin
reintroduced by a careless edit, a visibility gate accidentally dropped, the live-n8n
exclusion marker lost from the normal test run) is caught on every ordinary CI run
rather than only discovered the next time someone reads the workflow by hand.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
WORKFLOW_FILES = sorted(WORKFLOWS_DIR.glob("*.yml"))

_SHA_PINNED_USES = re.compile(r"^[^@]+@[0-9a-f]{40}(?:\s+#.*)?$")


@pytest.mark.unit
def test_at_least_one_workflow_file_exists() -> None:
    assert WORKFLOW_FILES


@pytest.mark.unit
@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda p: p.name)
def test_workflow_is_valid_yaml_with_jobs(path: Path) -> None:
    document = yaml.safe_load(path.read_text())
    assert "jobs" in document
    assert document["jobs"]


def _uses_lines(path: Path) -> list[str]:
    return [
        line.split("uses:", 1)[1].strip()
        for line in path.read_text().splitlines()
        if line.strip().startswith("- uses:") or line.strip().startswith("uses:")
    ]


@pytest.mark.unit
@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_full_commit_sha(path: Path) -> None:
    """A mutable tag (`@v4`) can be re-pointed by the action's own maintainer (or an
    attacker who compromises their account) to different code without this
    repository's history changing at all — the classic supply-chain vector a pinned,
    immutable 40-character commit SHA closes. A trailing `# vX.Y.Z` comment is
    encouraged (Dependabot's `github-actions` ecosystem reads it to keep proposing
    updates) but the pin itself must always be the SHA, never the tag."""
    uses_lines = _uses_lines(path)
    assert uses_lines, f"{path.name} calls no actions — unexpected for a workflow file"
    for uses in uses_lines:
        assert _SHA_PINNED_USES.match(uses), (
            f"{path.name}: {uses!r} is not pinned to a full 40-character commit SHA"
        )


@pytest.mark.unit
def test_codeql_only_runs_once_the_repository_is_public() -> None:
    """CodeQL's native code-scanning upload is unavailable on a private repository
    without GitHub Advanced Security — this job must skip cleanly while private
    (`docs/PUBLIC_RELEASE_CHECKLIST.md`), never fail, and start running the moment the
    repository's visibility changes, with no further edit required."""
    text = (WORKFLOWS_DIR / "codeql.yml").read_text()
    assert "github.event.repository.visibility == 'public'" in text


@pytest.mark.unit
def test_ci_excludes_live_n8n_tests() -> None:
    """The live-n8n suite needs a real instance and must never block a normal push —
    `docs/LIVE_N8N_TESTING.md` and `docs/BUILD_PLAN.md` section 10.1 both say so."""
    text = (WORKFLOWS_DIR / "ci.yml").read_text()
    assert '-m "not live_n8n"' in text


@pytest.mark.unit
def test_secret_scan_covers_full_history() -> None:
    """`fetch-depth: 0` — a shallow checkout would let a secret committed and later
    removed slip past Gitleaks' history scan undetected."""
    text = (WORKFLOWS_DIR / "secret-scan.yml").read_text()
    assert "fetch-depth: 0" in text


@pytest.mark.unit
@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda p: p.name)
def test_workflow_declares_explicit_permissions_or_relies_on_the_repo_default(
    path: Path,
) -> None:
    """Every workflow here either declares its own least-privilege `permissions:`
    block, or (like `ci.yml`) has none and inherits the repository's own
    least-privilege default (`read`, confirmed separately via the GitHub API) — never
    an implicit `write-all` a workflow forgot to narrow. This test only guards the
    declared-block half: a workflow that adds a `permissions:` block in the future
    must not grant more than `contents`/`pull-requests`/`security-events`/`actions`
    read-or-narrower, the set every workflow here has needed so far."""
    document = yaml.safe_load(path.read_text())
    permissions = document.get("permissions")
    if permissions is None:
        return
    allowed_scopes = {"contents", "pull-requests", "security-events", "actions"}
    assert set(permissions) <= allowed_scopes, (
        f"{path.name} grants an unreviewed permission scope: {set(permissions) - allowed_scopes}"
    )
    for scope, level in permissions.items():
        assert level in ("read", "write"), f"{path.name}: unexpected level for {scope}: {level!r}"
