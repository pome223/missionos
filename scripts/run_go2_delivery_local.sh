#!/usr/bin/env bash
# CPU-only simulator setup and one operator-authorized delivery episode.
set -euo pipefail
if [[ "${RUN_MISSIONOS_GO2_DELIVERY_SIM:-}" != 1 ]]; then
  echo 'Set RUN_MISSIONOS_GO2_DELIVERY_SIM=1 to enable this simulator.' >&2
  exit 2
fi
: "${GO2_DELIVERY_OPERATOR_APPROVAL_REF:?Set the operator simulation approval reference}"
SIM_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SIM_CACHE="${GO2_DELIVERY_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/missionos-go2-delivery}"
SIM_PYTHON="${GO2_DELIVERY_PYTHON:-python3}"
SIM_OUTPUT="${GO2_DELIVERY_OUTPUT:-$(mktemp -d "${TMPDIR:-/tmp}/go2-delivery.XXXXXX")}"
mkdir -p "$SIM_CACHE"

checkout_source() {
  local name="$1" url="$2" commit="$3" sparse="$4"
  local target="$SIM_CACHE/$name"
  if [[ ! -d "$target" ]]; then
    git clone --filter=blob:none --no-checkout "$url" "$target"
    git -C "$target" sparse-checkout init --cone
    git -C "$target" sparse-checkout set "$sparse"
    git -C "$target" checkout --detach "$commit"
  fi
  if [[ "$(git -C "$target" rev-parse HEAD)" != "$commit" ]] ||
     [[ -n "$(git -C "$target" status --porcelain)" ]]; then
    echo "External source differs from the pinned clean revision: $target" >&2
    exit 2
  fi
}

checkout_source rl-sar https://github.com/fan-ziqi/rl_sar.git \
  376d42c9b128f963ab08579762d5a216a976ce39 policy/go2
checkout_source rl-sar-zoo https://github.com/fan-ziqi/rl_sar_zoo.git \
  7dd30bdc7806898950b354260655d5a7f0ce844e go2_description

if [[ ! -x "$SIM_CACHE/venv/bin/python" ]]; then
  "$SIM_PYTHON" -m venv "$SIM_CACHE/venv"
fi
"$SIM_CACHE/venv/bin/python" -m pip install -r "$SIM_ROOT/simulators/go2_delivery/requirements.txt"
export PYTHONPATH="$SIM_ROOT:$SIM_ROOT/packages/missionos-core/src${PYTHONPATH:+:$PYTHONPATH}"
cd "$SIM_ROOT"
exec "$SIM_CACHE/venv/bin/python" scripts/run_go2_delivery.py \
  --model "$SIM_CACHE/rl-sar-zoo/go2_description/mjcf/go2.xml" \
  --policy "$SIM_CACHE/rl-sar/policy/go2/robot_lab/policy.pt" \
  --output "$SIM_OUTPUT" --approval-ref "$GO2_DELIVERY_OPERATOR_APPROVAL_REF" "$@"
