#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GATEWAY_SERVICE="${GATEWAY_SERVICE:-boiled-claw-gateway}"
GATEWAY_URL="${GATEWAY_URL:-http://127.0.0.1:18789}"
GATEWAY_HEALTH_URL="${GATEWAY_HEALTH_URL:-${GATEWAY_URL}/health}"
CONTROL_UI_URL="${CONTROL_UI_URL:-${GATEWAY_URL}/chat}"

cd "${ROOT_DIR}"

ensure_env_file() {
  if [[ -f ".env" ]]; then
    return 0
  fi
  if [[ ! -f ".env.example" ]]; then
    echo "Missing .env and .env.example; cannot create local config." >&2
    return 1
  fi

  cp .env.example .env
  echo "Created .env from .env.example."
}

require_docker_compose() {
  if ! docker compose version >/dev/null 2>&1; then
    echo "docker compose is required for make quickstart." >&2
    return 1
  fi
}

wait_for_gateway() {
  for _ in $(seq 1 60); do
    if curl -fsS "${GATEWAY_HEALTH_URL}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done

  echo "Timed out waiting for gateway health endpoint at ${GATEWAY_HEALTH_URL}" >&2
  return 1
}

json_field() {
  local payload="$1"
  local field="$2"
  SMOKE_JSON="${payload}" python3 - "${field}" <<'PY'
import json
import os
import sys

payload = json.loads(os.environ["SMOKE_JSON"])
print(payload[sys.argv[1]])
PY
}

main() {
  ensure_env_file
  require_docker_compose

  echo "Starting ${GATEWAY_SERVICE}..."
  docker compose up -d --build "${GATEWAY_SERVICE}"

  echo "Waiting for Gateway health..."
  wait_for_gateway
  curl -fsS "${GATEWAY_URL}/protocol" >/dev/null

  echo "Creating quickstart smoke task..."
  local smoke_json
  smoke_json="$(
    docker compose exec -T "${GATEWAY_SERVICE}" \
      python -m src.main quickstart-smoke \
        --gateway-url "${GATEWAY_URL}" \
        --json
  )"
  local task_id
  task_id="$(json_field "${smoke_json}" task_id)"

  curl -fsS "${GATEWAY_URL}/tasks/${task_id}" >/dev/null
  curl -fsS "${GATEWAY_URL}/tasks/${task_id}/timeline?limit=20" >/dev/null

  cat <<EOF

Quickstart OK.
Control UI: ${CONTROL_UI_URL}
Task:       ${GATEWAY_URL}/tasks/${task_id}
Timeline:   ${GATEWAY_URL}/tasks/${task_id}/timeline?limit=20

This smoke does not require GOOGLE_API_KEY, Chrome extension, Host Bridge, or Desktop Bridge.
EOF
}

main "$@"
