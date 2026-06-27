#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${ROOT_DIR}/data/bridge-runtime"
HOST_PID_FILE="${RUNTIME_DIR}/host-bridge.pid"
DESKTOP_PID_FILE="${RUNTIME_DIR}/desktop-bridge.pid"
HOST_LOG_FILE="${RUNTIME_DIR}/host-bridge.log"
DESKTOP_LOG_FILE="${RUNTIME_DIR}/desktop-bridge.log"

cd "${ROOT_DIR}"

load_env() {
  if [[ -f "${ROOT_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${ROOT_DIR}/.env"
    set +a
  fi
}

port_pid() {
  local port="$1"
  lsof -tiTCP:"${port}" -sTCP:LISTEN 2>/dev/null | head -n 1 || true
}

is_pid_running() {
  local pid="${1:-}"
  [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null
}

wait_for_port() {
  local label="$1"
  local port="$2"
  local attempts="${3:-50}"

  for _ in $(seq 1 "${attempts}"); do
    if [[ -n "$(port_pid "${port}")" ]]; then
      return 0
    fi
    sleep 0.2
  done

  echo "Timed out waiting for ${label} on port ${port}" >&2
  return 1
}

wait_for_gateway() {
  for _ in $(seq 1 50); do
    if curl -fsS "http://127.0.0.1:18789/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done

  echo "Timed out waiting for gateway health endpoint" >&2
  return 1
}

start_bridge() {
  local label="$1"
  local pid_file="$2"
  local log_file="$3"
  local port="$4"
  shift 4

  mkdir -p "${RUNTIME_DIR}"

  local existing_pid
  existing_pid="$(port_pid "${port}")"
  if [[ -n "${existing_pid}" ]]; then
    echo "${existing_pid}" >"${pid_file}"
    echo "${label} already listening on port ${port} (pid ${existing_pid})"
    return 0
  fi

  nohup "$@" >"${log_file}" 2>&1 &
  local pid=$!
  echo "${pid}" >"${pid_file}"
  wait_for_port "${label}" "${port}"
  echo "Started ${label} on port ${port} (pid ${pid})"
}

stop_bridge() {
  local label="$1"
  local pid_file="$2"

  if [[ ! -f "${pid_file}" ]]; then
    echo "${label} pid file not found; skipping"
    return 0
  fi

  local pid
  pid="$(cat "${pid_file}")"
  if is_pid_running "${pid}"; then
    kill "${pid}"
    wait "${pid}" 2>/dev/null || true
    echo "Stopped ${label} (pid ${pid})"
  else
    echo "${label} pid ${pid} is not running"
  fi
  rm -f "${pid_file}"
}

restart_gateway() {
  HOST_BRIDGE_ENABLED=true \
  HOST_BRIDGE_URL="${HOST_BRIDGE_URL}" \
  DESKTOP_BRIDGE_ENABLED=true \
  DESKTOP_BRIDGE_URL="${DESKTOP_BRIDGE_URL}" \
  docker compose up -d --force-recreate "${GATEWAY_SERVICE}"

  wait_for_gateway
  echo "Gateway is healthy"
}

show_status() {
  local host_pid desktop_pid current_tab_pid
  host_pid="$(port_pid "${HOST_BRIDGE_PORT}")"
  desktop_pid="$(port_pid "${DESKTOP_BRIDGE_PORT}")"
  current_tab_pid="$(port_pid "${CURRENT_TAB_BRIDGE_PORT}")"

  echo "Host Bridge:"
  echo "  url: ${HOST_BRIDGE_URL}"
  echo "  bind: ${HOST_BRIDGE_BIND_HOST}:${HOST_BRIDGE_PORT}"
  echo "  pid: ${host_pid:-not listening}"

  echo "Desktop Bridge:"
  echo "  url: ${DESKTOP_BRIDGE_URL}"
  echo "  bind: ${DESKTOP_BRIDGE_BIND_HOST}:${DESKTOP_BRIDGE_PORT}"
  echo "  pid: ${desktop_pid:-not listening}"

  echo "Current Tab Relay:"
  echo "  enabled: ${CURRENT_TAB_BRIDGE_ENABLED}"
  echo "  url: ws://${CURRENT_TAB_BRIDGE_HOST}:${CURRENT_TAB_BRIDGE_PORT}"
  if [[ "${CURRENT_TAB_BRIDGE_ENABLED}" == "true" ]]; then
    echo "  pid: ${current_tab_pid:-not listening}"
  else
    echo "  pid: disabled"
  fi

  echo "Gateway:"
  if curl -fsS "http://127.0.0.1:18789/health" >/dev/null 2>&1; then
    echo "  health: ok"
  else
    echo "  health: unavailable"
  fi
}

start_all() {
  start_bridges_only

  restart_gateway
  show_status
}

start_bridges_only() {
  start_bridge \
    "Host Bridge" \
    "${HOST_PID_FILE}" \
    "${HOST_LOG_FILE}" \
    "${HOST_BRIDGE_PORT}" \
    env BRIDGE_ALLOW_REMOTE_BIND="${BRIDGE_ALLOW_REMOTE_BIND}" \
    "${ROOT_DIR}/.venv/bin/python" -m src.main bridge host \
    --host "${HOST_BRIDGE_BIND_HOST}" \
    --port "${HOST_BRIDGE_PORT}"

  start_bridge \
    "Desktop Bridge" \
    "${DESKTOP_PID_FILE}" \
    "${DESKTOP_LOG_FILE}" \
    "${DESKTOP_BRIDGE_PORT}" \
    env BRIDGE_ALLOW_REMOTE_BIND="${BRIDGE_ALLOW_REMOTE_BIND}" \
    "${ROOT_DIR}/.venv/bin/python" -m src.main bridge desktop \
    --host "${DESKTOP_BRIDGE_BIND_HOST}" \
    --port "${DESKTOP_BRIDGE_PORT}"

  if [[ "${CURRENT_TAB_BRIDGE_ENABLED}" == "true" ]]; then
    wait_for_port "Current Tab Relay" "${CURRENT_TAB_BRIDGE_PORT}"
  fi
}

stop_all() {
  stop_bridge "Host Bridge" "${HOST_PID_FILE}"
  stop_bridge "Desktop Bridge" "${DESKTOP_PID_FILE}"
}

main() {
  load_env

  HOST_BRIDGE_PORT="${HOST_BRIDGE_PORT:-8766}"
  DESKTOP_BRIDGE_PORT="${DESKTOP_BRIDGE_PORT:-8767}"
  HOST_BRIDGE_BIND_HOST="${HOST_BRIDGE_BIND_HOST:-0.0.0.0}"
  DESKTOP_BRIDGE_BIND_HOST="${DESKTOP_BRIDGE_BIND_HOST:-0.0.0.0}"
  HOST_BRIDGE_URL="${HOST_BRIDGE_URL:-http://host.docker.internal:${HOST_BRIDGE_PORT}/sse}"
  DESKTOP_BRIDGE_URL="${DESKTOP_BRIDGE_URL:-http://host.docker.internal:${DESKTOP_BRIDGE_PORT}/sse}"
  CURRENT_TAB_BRIDGE_ENABLED="${CURRENT_TAB_BRIDGE_ENABLED:-false}"
  CURRENT_TAB_BRIDGE_HOST="${CURRENT_TAB_BRIDGE_HOST:-127.0.0.1}"
  CURRENT_TAB_BRIDGE_PORT="${CURRENT_TAB_BRIDGE_PORT:-8768}"
  GATEWAY_SERVICE="${GATEWAY_SERVICE:-boiled-claw-gateway}"
  BRIDGE_ALLOW_REMOTE_BIND="${BRIDGE_ALLOW_REMOTE_BIND:-true}"

  local command="${1:-start}"
  case "${command}" in
    start)
      start_all
      ;;
    start-bridges)
      start_bridges_only
      show_status
      ;;
    stop)
      stop_all
      ;;
    restart)
      stop_all
      start_all
      ;;
    status)
      show_status
      ;;
    *)
      echo "Usage: $0 [start|start-bridges|stop|restart|status]" >&2
      exit 1
      ;;
  esac
}

main "$@"
