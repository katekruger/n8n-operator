#!/usr/bin/env bash
# n8n Operator — a five-minute, no-n8n-required demo of the operator surface.
#
# Runs entirely against a scratch SQLite database and the annotated example registry —
# no real n8n instance required for this part, since discovery, validation, and the
# governance CLI don't touch n8n at all. Prints what it's doing at every step so it
# doubles as a readable walkthrough, not just a script to run and ignore.
#
# What this does NOT demonstrate: prepare -> approve -> execute against a live n8n
# instance (that needs N8N_OPERATOR_N8N_BASE_URL/N8N_OPERATOR_N8N_API_KEY pointed at a
# real instance) or a real MCP client session (see examples/mcp-clients/). Both are
# covered in the README quickstart.
#
# Usage: scripts/demo.sh [--keep]
#   --keep   Don't delete the scratch directory on exit; print its path instead.

set -euo pipefail

KEEP=0
if [[ "${1:-}" == "--keep" ]]; then
  KEEP=1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if command -v n8n-operator >/dev/null 2>&1; then
  OPERATOR=(n8n-operator)
elif command -v uv >/dev/null 2>&1 && [[ -f "$REPO_ROOT/pyproject.toml" ]]; then
  OPERATOR=(uv run n8n-operator)
else
  echo "error: install n8n Operator from the repository checkout first:" >&2
  echo "  uv tool install ." >&2
  exit 1
fi

DEMO_DIR="$(mktemp -d -t n8n-operator-demo.XXXXXX)"
cleanup() {
  if [[ "$KEEP" -eq 1 ]]; then
    echo
    echo "Scratch directory kept at: $DEMO_DIR"
  else
    rm -rf "$DEMO_DIR"
  fi
}
trap cleanup EXIT

export N8N_OPERATOR_DATABASE_URL="sqlite+pysqlite:///$DEMO_DIR/n8n-operator.db"
REGISTRY_PATH="$DEMO_DIR/workflows.yaml"
cp "$REPO_ROOT/examples/registry/workflows.example.yaml" "$REGISTRY_PATH"

step() {
  echo
  echo "=== $* ==="
}

step "1. Initialize a scratch database"
"${OPERATOR[@]}" db init

step "2. Validate the example registry (no database involved)"
"${OPERATOR[@]}" registry validate --path "$REGISTRY_PATH"

step "3. Load it — this is the only step that writes to the database"
"${OPERATOR[@]}" registry reload --path "$REGISTRY_PATH"

step "4. List what's registered"
"${OPERATOR[@]}" registry list --path "$REGISTRY_PATH"

step "5. Inspect one workflow in detail"
"${OPERATOR[@]}" registry show crm.sync_contact --path "$REGISTRY_PATH"

step "6. Operator's own history — empty, nothing has been prepared yet"
"${OPERATOR[@]}" operations list

step "7. The audit trail — one entry so far, the registry load itself"
"${OPERATOR[@]}" audit verify
"${OPERATOR[@]}" audit export | head -30
echo "  ... (truncated; run 'n8n-operator audit export' yourself for the full record)"

echo
echo "=== Next steps ==="
cat <<'EOF'
This covered discovery, validation, and the governance/audit surface — no n8n
instance required. To see the rest:

  * prepare -> approve -> execute against a real n8n instance:
      export N8N_OPERATOR_N8N_BASE_URL=https://your-n8n-instance.example.com
      export N8N_OPERATOR_N8N_API_KEY=...
      n8n-operator serve stdio    # or 'serve http' for a remote MCP client

  * a real MCP client session (Claude Desktop, or any Streamable HTTP client):
      see examples/mcp-clients/README.md

  * what a pending approval actually looks like:
      n8n-operator operations approve <operation_id>
      n8n-operator serve approval   # the loopback web page alternative
EOF
