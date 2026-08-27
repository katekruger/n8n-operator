"""Release version/tag/changelog consistency, enforced as a test (BUILD_PLAN section
12 phase 9 continuation, release-readiness Phase 7).

Runs the same checker `.github/workflows/release.yml` runs against a real tag, plus
the negative case (a tag that does not match), so a broken release-consistency check
fails the normal build rather than only being discovered mid-release.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check_release_consistency.py"
EXTRACT_SCRIPT = REPO_ROOT / "scripts" / "extract_changelog_section.py"


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args], capture_output=True, text=True, check=False
    )


@pytest.mark.contract
def test_current_release_state_is_self_consistent() -> None:
    result = _run(CHECK_SCRIPT)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.contract
def test_current_release_state_matches_its_own_pyproject_version_as_a_tag() -> None:
    import tomllib

    version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]
    result = _run(CHECK_SCRIPT, "--tag", f"v{version}")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.contract
def test_a_mismatched_tag_is_rejected() -> None:
    result = _run(CHECK_SCRIPT, "--tag", "v0.0.1-definitely-not-the-real-version")
    assert result.returncode == 1
    assert "!=" in result.stdout


@pytest.mark.contract
def test_changelog_section_extraction_matches_the_current_version() -> None:
    import tomllib

    version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]
    result = _run(EXTRACT_SCRIPT, "--tag", f"v{version}")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() != ""


@pytest.mark.contract
def test_changelog_section_extraction_rejects_an_unknown_version() -> None:
    result = _run(EXTRACT_SCRIPT, "--tag", "v0.0.1-definitely-not-the-real-version")
    assert result.returncode != 0
    assert "no CHANGELOG.md section" in result.stderr
