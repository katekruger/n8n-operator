#!/usr/bin/env bash
# Inspect every file inside the built wheel and sdist for anything that must never
# ship: a real .env file, a SQLite database, a private key, or an SSH/netrc/pgpass
# credential file. Run after `uv build`, before any artifact is uploaded to a GitHub
# Release or published to PyPI (BUILD_PLAN section 12 phase 9, release-readiness
# Phase 7).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${DIST_DIR:-$REPO_ROOT/dist}"

WHEEL=""
SDIST=""
if [[ -d "$DIST_DIR" ]]; then
  WHEEL="$(find "$DIST_DIR" -maxdepth 1 -name '*.whl' -print -quit)"
  SDIST="$(find "$DIST_DIR" -maxdepth 1 -name '*.tar.gz' -print -quit)"
fi

if [[ -z "$WHEEL" || -z "$SDIST" ]]; then
  echo "error: expected both a .whl and a .tar.gz in $DIST_DIR; run 'uv build' first" >&2
  exit 1
fi

# Patterns that must never appear as a shipped file's *name*, anywhere in either
# archive. Deliberately narrow, basename-anchored patterns for actual credential/data
# files — not substrings like "secret" or "credentials", which this codebase's own
# docs and test fixtures legitimately use in filenames (ADR-006 is literally about
# credential handling; tests/fixtures/registry/r6_literal_secret.yaml is a fixture
# about detecting one). Gitleaks (secret-scan.yml) already scans file *contents*
# across full history; this check is narrower and different on purpose — did a stray
# build-time artifact (a real .env, a local SQLite DB, a private key) get packaged.
FORBIDDEN_PATTERNS=(
  '(^|/)\.env$'
  '(^|/)\.env\.'
  '\.db$'
  '\.sqlite3?$'
  '(^|/)id_rsa'
  '\.pem$'
  '(^|/)\.netrc$'
  '(^|/)\.pgpass$'
)

# Filenames that would otherwise match a forbidden pattern above but are known-safe,
# committed, placeholder-only templates — never a real value.
ALLOWED_EXCEPTIONS='\.env\.example$'

check_listing() {
  local archive="$1"
  local listing="$2"
  local pattern
  local filtered
  filtered="$(grep -Ev "$ALLOWED_EXCEPTIONS" <<<"$listing")"
  for pattern in "${FORBIDDEN_PATTERNS[@]}"; do
    if grep -qEi "$pattern" <<<"$filtered"; then
      echo "error: $archive contains a file matching forbidden pattern '$pattern':" >&2
      grep -Ei "$pattern" <<<"$filtered" >&2
      exit 1
    fi
  done
}

echo "Inspecting $WHEEL"
# Bare names, one per line — matching tar -tzf's format below. `python3 -m zipfile -l`
# prints a padded Name/Modified/Size table instead, which would silently defeat every
# end-anchored pattern above (nothing in a real archive entry ever ends the line).
WHEEL_LISTING="$(python3 -c '
import sys, zipfile
for name in zipfile.ZipFile(sys.argv[1]).namelist():
    print(name)
' "$WHEEL")"
check_listing "$WHEEL" "$WHEEL_LISTING"
echo "  $(wc -l <<<"$WHEEL_LISTING" | tr -d ' ') entries, none forbidden"

echo "Inspecting $SDIST"
SDIST_LISTING="$(tar -tzf "$SDIST")"
check_listing "$SDIST" "$SDIST_LISTING"
echo "  $(wc -l <<<"$SDIST_LISTING" | tr -d ' ') entries, none forbidden"

echo "artifact inspection passed: no forbidden files in $(basename "$WHEEL") or $(basename "$SDIST")"
