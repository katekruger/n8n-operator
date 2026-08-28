#!/usr/bin/env bash
# Start the isolated live-n8n compatibility harness (docs/LIVE_N8N_TESTING.md).
#
# Automates everything n8n exposes a supported, non-UI interface for: bringing up a
# pinned, isolated instance, importing the synthetic test workflow, and reliably
# activating its webhook trigger — all via the n8n CLI, no API key or logged-in
# session required for any of it. n8n has no documented REST/CLI endpoint for
# first-run owner-account setup or API-key creation — both are UI-only — so this
# script stops short of those and prints the one manual step required to finish.
#
# Getting real webhook registration working took real trial and error against a live
# 2.35.7 instance (see docs/LIVE_N8N_TESTING.md for the full story): the legacy
# `update:workflow --active=true` and its replacement `publish:workflow` both write to
# the database but need a container restart to actually take effect in the running
# process (n8n builds its webhook routing table once, at boot). The confirmed working
# sequence is import -> `publish:workflow` -> restart, done here in that order — no
# UI interaction needed for activation itself.
#
# Rerunnable: `docker compose up -d` is idempotent, and re-importing the same workflow
# JSON updates the existing entry (matched by id) rather than duplicating it — but note
# a re-import deactivates the workflow as a side effect, which is exactly why
# `publish:workflow` + restart runs again unconditionally after every import here.

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

wait_for_healthy() {
  local ready=0
  for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:5678/healthz" >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 2
  done
  if [[ "$ready" -ne 1 ]]; then
    echo "error: n8n did not become healthy within the timeout. Recent logs:" >&2
    docker compose -f "$COMPOSE_FILE" logs --tail=80 n8n >&2
    exit 1
  fi
}

echo "=== Starting the isolated n8n instance ==="
docker compose -f "$COMPOSE_FILE" up -d

echo
echo "=== Waiting for readiness (GET /healthz) ==="
wait_for_healthy
echo "n8n is healthy at http://127.0.0.1:5678"

echo
echo "=== Importing the synthetic test workflow ==="
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
  echo "error: could not automatically determine the imported workflow's ID." >&2
  echo "Run 'docker exec $CONTAINER_NAME n8n list:workflow' yourself to find it, then:" >&2
  echo "  docker exec $CONTAINER_NAME n8n publish:workflow --id=<id>" >&2
  echo "  docker restart $CONTAINER_NAME" >&2
  exit 1
fi
echo "Imported workflow ID: $WORKFLOW_ID"

echo
echo "=== Publishing the workflow and restarting to register its webhook ==="
# A re-import (above) deactivates the workflow as a side effect, and neither
# publish:workflow nor the legacy update:workflow actually registers the webhook
# trigger in the *running* process — n8n builds its webhook routing table once, at
# boot, so a restart afterward is required every time. Confirmed against a real
# 2.35.7 instance.
docker exec "$CONTAINER_NAME" n8n publish:workflow --id="$WORKFLOW_ID"
docker restart "$CONTAINER_NAME" >/dev/null
wait_for_healthy
echo "Workflow $WORKFLOW_ID published and its webhook registered."

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
       export N8N_LIVE_WORKFLOW_ID=$WORKFLOW_ID
       export N8N_LIVE_WEBHOOK_PATH=/webhook/operator-smoke-test
       uv run pytest -m live_n8n -v

Tear down with scripts/live_n8n_down.sh when finished — it removes only this
project's own container and volume, nothing else on the host.
EOF
