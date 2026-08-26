"""Documentation consistency, enforced as a test (BUILD_PLAN section 10.3).

Runs the same checker CI runs, so a doc contradiction fails the build the way a broken
test does.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.contract
def test_documentation_is_internally_consistent() -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(REPO_ROOT / "scripts" / "check_docs_consistency.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
