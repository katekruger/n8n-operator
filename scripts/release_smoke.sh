#!/usr/bin/env bash
# Verify the artifact users receive, not the editable source checkout.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SMOKE_DIR="$(mktemp -d -t n8n-operator-release-smoke.XXXXXX)"
cleanup() { rm -rf "$SMOKE_DIR"; }
trap cleanup EXIT

WHEEL="$(find "$REPO_ROOT/dist" -maxdepth 1 -name 'n8n_operator-*.whl' -print -quit)"
if [[ -z "$WHEEL" ]]; then
  echo "error: no wheel found in dist/; run 'uv build' first" >&2
  exit 1
fi

uv venv --python 3.12 "$SMOKE_DIR/venv"
uv pip install --python "$SMOKE_DIR/venv/bin/python" "$WHEEL"

export N8N_OPERATOR_DATABASE_URL="sqlite+pysqlite:///$SMOKE_DIR/operator.db"
"$SMOKE_DIR/venv/bin/python" -c \
  'import n8n_operator; assert n8n_operator.__version__ == "1.0.0rc2"'
"$SMOKE_DIR/venv/bin/n8n-operator" --help >/dev/null
"$SMOKE_DIR/venv/bin/n8n-operator" db init
"$SMOKE_DIR/venv/bin/n8n-operator" registry validate \
  --path "$REPO_ROOT/examples/registry/workflows.example.yaml"
"$SMOKE_DIR/venv/bin/n8n-operator" audit verify

echo "release smoke passed: wheel install, import, CLI, migration, registry, audit"
