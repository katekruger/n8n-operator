#!/usr/bin/env bash
# Tear down the isolated live-n8n compatibility harness (docs/LIVE_N8N_TESTING.md).
#
# Scoped entirely to this project's own compose file and named volume — `docker
# compose down` only ever touches resources it created under this project name
# (n8n-operator-live-test, set via `name:` in docker-compose.yml), never another
# container, volume, or image on the host.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/docker/live-n8n/docker-compose.yml"

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker is required." >&2
  exit 1
fi

echo "=== Stopping and removing the isolated n8n instance and its volume ==="
docker compose -f "$COMPOSE_FILE" down --volumes

echo "Done. No other containers, volumes, or images were touched."
