#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRIDGE_SCRIPT="${ROOT_DIR}/scripts/bridge_runtime.sh"
GATEWAY_SERVICE="${GATEWAY_SERVICE:-boiled-claw-gateway}"
GATEWAY_HEALTH_URL="${GATEWAY_HEALTH_URL:-http://127.0.0.1:18789/health}"
SYNC_FALLBACK_DEFAULT="${DEPLOY_ALLOW_SYNC_FALLBACK:-true}"

cd "${ROOT_DIR}"

load_env() {
  if [[ -f "${ROOT_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${ROOT_DIR}/.env"
    set +a
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

start_bridges() {
  if [[ ! -f "${BRIDGE_SCRIPT}" ]]; then
    echo "Bridge runtime script not found: ${BRIDGE_SCRIPT}" >&2
    return 1
  fi
  bash "${BRIDGE_SCRIPT}" start-bridges
}

show_status() {
  if [[ -f "${BRIDGE_SCRIPT}" ]]; then
    bash "${BRIDGE_SCRIPT}" status
  fi
  docker compose ps "${GATEWAY_SERVICE}"
}

maybe_start_redis() {
  local redis_url="${REDIS_URL:-}"
  if [[ -z "${redis_url}" ]]; then
    return 0
  fi
  if [[ "${redis_url}" == *"boiled-claw-redis"* ]]; then
    docker compose --profile redis up -d boiled-claw-redis
  fi
}

gateway_container_id() {
  docker compose ps -q "${GATEWAY_SERVICE}" 2>/dev/null | head -n 1 || true
}

compose_build_gateway() {
  docker compose up -d --build "${GATEWAY_SERVICE}"
}

build_log_has_no_space_error() {
  local build_log="$1"
  [[ -f "${build_log}" ]] && grep -qiE "no space left on device|enough free space" "${build_log}"
}

prune_buildkit_cache() {
  echo "Pruning BuildKit cache before retrying docker build..." >&2
  docker buildx prune -af
}

prune_unused_images() {
  echo "Pruning unused Docker images before retrying docker build..." >&2
  docker image prune -af
}

build_gateway() {
  local build_log
  build_log="$(mktemp -t boiled-claw-build.XXXXXX.log)"

  maybe_start_redis

  if ! compose_build_gateway 2>&1 | tee "${build_log}"; then
    if build_log_has_no_space_error "${build_log}"; then
      prune_buildkit_cache >&2
      echo "Retrying docker build for ${GATEWAY_SERVICE} after cache prune..." >&2
      if ! compose_build_gateway 2>&1 | tee "${build_log}"; then
        if build_log_has_no_space_error "${build_log}"; then
          prune_unused_images >&2
          echo "Retrying docker build for ${GATEWAY_SERVICE} after unused image prune..." >&2
          if ! compose_build_gateway 2>&1 | tee "${build_log}"; then
            rm -f "${build_log}"
            return 1
          fi
        else
          rm -f "${build_log}"
          return 1
        fi
      fi
    else
      rm -f "${build_log}"
      return 1
    fi
  fi

  wait_for_gateway
  rm -f "${build_log}"
}

sync_gateway_source() {
  local container_id
  container_id="$(gateway_container_id)"
  if [[ -z "${container_id}" ]]; then
    echo "Gateway container is not running; cannot sync source." >&2
    return 1
  fi

  docker exec "${container_id}" rm -rf /app/src
  docker cp "${ROOT_DIR}/src" "${container_id}:/app/src"
  docker cp "${ROOT_DIR}/README.md" "${container_id}:/app/README.md"
  docker cp "${ROOT_DIR}/pyproject.toml" "${container_id}:/app/pyproject.toml"

  docker compose restart "${GATEWAY_SERVICE}"
  wait_for_gateway
}

deploy_auto() {
  if build_gateway; then
    echo "Deployed current workspace by rebuilding ${GATEWAY_SERVICE}."
    return 0
  fi

  if [[ "${SYNC_FALLBACK_DEFAULT}" != "true" ]]; then
    echo "Build failed and sync fallback is disabled." >&2
    return 1
  fi

  echo "Build failed; falling back to hot-sync of current source into the running gateway container." >&2
  sync_gateway_source
  echo "Deployed current workspace by hot-syncing source into ${GATEWAY_SERVICE}."
}

usage() {
  cat <<'EOF'
Usage: scripts/deploy_runtime.sh [deploy|build|sync|status]

Commands:
  deploy  Start bridges, then deploy the current workspace. Tries docker build first,
          falls back to hot-sync if DEPLOY_ALLOW_SYNC_FALLBACK=true.
  build   Start bridges, then rebuild and restart the gateway from the current workspace.
  sync    Start bridges, then copy current src/ into the running gateway container and restart it.
  status  Show bridge and gateway status.
EOF
}

main() {
  load_env

  local command="${1:-deploy}"
  case "${command}" in
    deploy)
      start_bridges
      deploy_auto
      show_status
      ;;
    build)
      start_bridges
      build_gateway
      show_status
      ;;
    sync)
      start_bridges
      sync_gateway_source
      show_status
      ;;
    status)
      show_status
      ;;
    *)
      usage >&2
      exit 1
      ;;
  esac
}

main "$@"
