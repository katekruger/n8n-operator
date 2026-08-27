"""``scripts/inspect_release_artifacts.sh`` against synthetic fixture archives — no
`uv build` needed, so this runs in the normal (fast) test suite rather than only being
exercised the next time someone actually builds a release (BUILD_PLAN section 12
phase 9 continuation, release-readiness Phase 7)."""

from __future__ import annotations

import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "inspect_release_artifacts.sh"


@pytest.mark.unit
def test_script_is_syntactically_valid_bash() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def _make_wheel(path: Path, *, extra_files: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("pkg/__init__.py", "")
        for name, content in extra_files.items():
            archive.writestr(name, content)


def _make_sdist(path: Path, *, extra_files: dict[str, str]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        import io

        def add(name: str, content: str) -> None:
            data = content.encode()
            info = tarfile.TarInfo(name=f"pkg-1.0.0/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))

        add("pyproject.toml", "[project]\nname='pkg'\n")
        for name, content in extra_files.items():
            add(name, content)


def _run(dist_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        env={"DIST_DIR": str(dist_dir), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )


@pytest.mark.unit
def test_a_clean_artifact_pair_passes(tmp_path: Path) -> None:
    _make_wheel(tmp_path / "pkg-1.0.0-py3-none-any.whl", extra_files={"pkg/models.py": ""})
    _make_sdist(tmp_path / "pkg-1.0.0.tar.gz", extra_files={"src/pkg/models.py": ""})
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "artifact inspection passed" in result.stdout


@pytest.mark.unit
def test_env_example_is_allowed(tmp_path: Path) -> None:
    """The one deliberate exception: a committed, placeholder-only template."""
    _make_wheel(tmp_path / "pkg-1.0.0-py3-none-any.whl", extra_files={})
    _make_sdist(
        tmp_path / "pkg-1.0.0.tar.gz", extra_files={".env.example": "N8N_OPERATOR_N8N_API_KEY=\n"}
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.unit
def test_a_real_env_file_is_rejected(tmp_path: Path) -> None:
    _make_wheel(tmp_path / "pkg-1.0.0-py3-none-any.whl", extra_files={})
    _make_sdist(
        tmp_path / "pkg-1.0.0.tar.gz", extra_files={".env": "N8N_OPERATOR_N8N_API_KEY=real-value\n"}
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert ".env" in result.stderr


@pytest.mark.unit
def test_a_sqlite_database_is_rejected(tmp_path: Path) -> None:
    _make_wheel(tmp_path / "pkg-1.0.0-py3-none-any.whl", extra_files={"n8n-operator.db": ""})
    _make_sdist(tmp_path / "pkg-1.0.0.tar.gz", extra_files={})
    result = _run(tmp_path)
    assert result.returncode == 1
    assert ".db" in result.stderr


@pytest.mark.unit
def test_a_private_key_is_rejected(tmp_path: Path) -> None:
    _make_wheel(tmp_path / "pkg-1.0.0-py3-none-any.whl", extra_files={})
    _make_sdist(tmp_path / "pkg-1.0.0.tar.gz", extra_files={"deploy_key.pem": "fake"})
    result = _run(tmp_path)
    assert result.returncode == 1
    assert ".pem" in result.stderr


@pytest.mark.unit
def test_missing_dist_directory_fails_clearly(tmp_path: Path) -> None:
    result = _run(tmp_path / "does-not-exist")
    assert result.returncode == 1
    assert "run 'uv build' first" in result.stderr
