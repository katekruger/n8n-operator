#!/usr/bin/env python3
"""Extract one version's section from CHANGELOG.md, for use as GitHub Release notes.

A release's notes should be the changelog entry a human already wrote and reviewed —
never separately hand-typed at release time, where the two are free to drift.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

_HEADING_RE = re.compile(r"^## \[([^\]]+)\][^\n]*$", re.MULTILINE)


def _normalize(version: str) -> str:
    return version.replace("-", "")


def extract(text: str, version: str) -> str:
    headings = list(_HEADING_RE.finditer(text))
    target_normalized = _normalize(version)
    for index, match in enumerate(headings):
        if _normalize(match.group(1)) != target_normalized:
            continue
        start = match.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        return text[start:end].strip() + "\n"
    available = [m.group(1) for m in headings]
    raise SystemExit(f"no CHANGELOG.md section for version {version!r} (found: {available})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="The git tag being released, e.g. v1.0.0rc2.")
    args = parser.parse_args()
    tag_version = args.tag[1:] if args.tag.startswith("v") else args.tag
    sys.stdout.write(extract(CHANGELOG.read_text(), tag_version))
    return 0


if __name__ == "__main__":
    sys.exit(main())
