#!/usr/bin/env bash
# Start the isolated live-n8n compatibility harness (docs/LIVE_N8N_TESTING.md).
#
# Automates everything n8n exposes a supported, non-UI interface for: bringing up a
# pinned, isolated instance and importing + activating the synthetic test workflow via
# the n8n CLI (which operates directly against the instance's own database and needs no
# API key or logged-in session). n8n has no documented REST/CLI endpoint for first-run
# owner-account setup or API-key creation — both are UI-only — so this script stops
# short of those and prints the one manual step required to finish.
#
# Rerunnable: `docker compose up -d` is idempotent, and re-importing the same workflow
# JSON updates the existing entry (matched by id) rather than duplicating it.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/docker/live-n8n/docker-compose.yml"
WORKFLOW_FILE="$REPO_ROOT/examples/registry/synthetic_test_workflow.json"
CONTAINER_NAME="n8n-operator-live-test"
IMPORT_PATH_IN_CONTAINER="/tmp/synthetic_test_workflow.json"

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker is required (or a drop-in: colima, podman with the docker CLI shim)." >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "error: 'docker compose' (the plugin, not the standalone docker-compose) is required." >&2
  exit 1
fi

echo "=== Starting the isolated n8n instance ==="
docker compose -f "$COMPOSE_FILE" up -d

echo
echo "=== Waiting for readiness (GET /healthz) ==="
READY=0
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:5678/healthz" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 2
done
if [[ "$READY" -ne 1 ]]; then
  echo "error: n8n did not become healthy within the timeout. Recent logs:" >&2
  docker compose -f "$COMPOSE_FILE" logs --tail=80 n8n >&2
  exit 1
fi
echo "n8n is healthy at http://127.0.0.1:5678"

echo
echo "=== Importing and activating the synthetic test workflow ==="
docker cp "$WORKFLOW_FILE" "$CONTAINER_NAME:$IMPORT_PATH_IN_CONTAINER"
IMPORT_OUTPUT="$(docker exec "$CONTAINER_NAME" n8n import:workflow --input="$IMPORT_PATH_IN_CONTAINER" 2>&1)" || {
  echo "error: workflow import failed. Output:" >&2
  echo "$IMPORT_OUTPUT" >&2
  exit 1
}
echo "$IMPORT_OUTPUT"

WORKFLOW_ID="$(docker exec "$CONTAINER_NAME" n8n list:workflow 2>/dev/null \
  | grep "n8n Operator — synthetic test workflow" | head -1 | cut -d'|' -f1 | tr -d ' ')" || true

if [[ -z "${WORKFLOW_ID:-}" ]]; then
  echo "warning: could not automatically determine the imported workflow's ID." >&2
  echo "Run 'docker exec $CONTAINER_NAME n8n list:workflow' yourself to find it." >&2
else
  echo "Imported workflow ID: $WORKFLOW_ID"
  docker exec "$CONTAINER_NAME" n8n update:workflow --id="$WORKFLOW_ID" --active=true
  echo "Activated workflow $WORKFLOW_ID."
fi

cat <<EOF

=== One manual step remains ===
n8n has no documented REST or CLI path to create the first owner account or an API
key — both are UI-only. Finish setup, then export the four variables the live suite
needs:

  1. Open http://127.0.0.1:5678 and complete the one-time owner account setup.
  2. Go to Settings > n8n API > Create an API Key.
  3. Then run:

       export N8N_LIVE_BASE_URL=http://127.0.0.1:5678
       export N8N_LIVE_API_KEY=<the key you just created>
       export N8N_LIVE_WORKFLOW_ID=${WORKFLOW_ID:-<from 'docker exec $CONTAINER_NAME n8n list:workflow'>}
       export N8N_LIVE_WEBHOOK_PATH=/webhook/operator-smoke-test
       uv run pytest -m live_n8n -v

Tear down with scripts/live_n8n_down.sh when finished — it removes only this
project's own container and volume, nothing else on the host.
EOF
