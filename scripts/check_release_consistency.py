#!/usr/bin/env python3
"""Verify that everything claiming a release version actually agrees on it.

Checks
------
R1  ``pyproject.toml``'s ``version`` and ``src/n8n_operator/__init__.py``'s
    ``__version__`` are identical (PEP 440 form, e.g. ``1.0.0rc2``).
R2  ``CHANGELOG.md`` has a heading for that version. The changelog spells
    pre-releases with a hyphen (``## [1.0.0-rc2] - YYYY-MM-DD``, Keep a Changelog
    style) where the PEP 440 form has none (``1.0.0rc2``) — this check normalizes
    both to the same bare digits-and-letters form before comparing, rather than
    requiring the two spellings to match literally.
R3  If ``--tag`` is given (the release workflow passes ``github.ref_name``), the tag
    — with a leading ``v`` stripped, e.g. ``v1.0.0rc2`` -> ``1.0.0rc2`` — matches the
    same normalized version. Omit ``--tag`` for a pre-tag local/CI check.

Exit code 0 means consistent; 1 means at least one contradiction, printed to stdout.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
INIT_PY = REPO_ROOT / "src" / "n8n_operator" / "__init__.py"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

_INIT_VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)
_CHANGELOG_HEADING_RE = re.compile(r"^## \[([^\]]+)\]", re.MULTILINE)


def _normalize(version: str) -> str:
    """Strips hyphens so ``1.0.0-rc2`` (CHANGELOG/tag spelling) and ``1.0.0rc2``
    (PEP 440 spelling) compare equal."""
    return version.replace("-", "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tag",
        default=None,
        help="The git tag being released (e.g. v1.0.0rc2). Omit for a pre-tag check.",
    )
    args = parser.parse_args()

    errors: list[str] = []

    pyproject_version = tomllib.loads(PYPROJECT.read_text())["project"]["version"]

    init_match = _INIT_VERSION_RE.search(INIT_PY.read_text())
    if init_match is None:
        errors.append(f'{INIT_PY}: no __version__ = "..." assignment found')
        init_version = None
    else:
        init_version = init_match.group(1)
        if _normalize(init_version) != _normalize(pyproject_version):
            errors.append(
                f"pyproject.toml version {pyproject_version!r} != "
                f"n8n_operator.__version__ {init_version!r}"
            )

    changelog_headings = _CHANGELOG_HEADING_RE.findall(CHANGELOG.read_text())
    normalized_pyproject = _normalize(pyproject_version)
    if not any(_normalize(h) == normalized_pyproject for h in changelog_headings):
        errors.append(
            f"CHANGELOG.md has no heading matching version {pyproject_version!r} "
            f"(found: {changelog_headings[:5]}{'...' if len(changelog_headings) > 5 else ''})"
        )

    if args.tag is not None:
        tag_version = args.tag[1:] if args.tag.startswith("v") else args.tag
        if _normalize(tag_version) != normalized_pyproject:
            errors.append(
                f"tag {args.tag!r} (version {tag_version!r}) != "
                f"pyproject.toml version {pyproject_version!r}"
            )

    if errors:
        print("Release consistency: FAILED")
        for error in errors:
            print(f"  - {error}")
        return 1

    print("Release consistency: OK")
    print(f"  version:    {pyproject_version}")
    print(f"  __init__:   {init_version}")
    print(f"  tag:        {args.tag or '(not checked)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
