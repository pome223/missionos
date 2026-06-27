# CLI Parity E2E Evidence

Store local parity commands, fixture outputs, and release-check notes here while
preparing the repository for publication.

Do not commit private task databases, generated output trees, screenshots, or
runtime logs without publication review. Prefer small redacted text evidence and
deterministic fixtures.

## Fixture Gateway Smoke

From the repository root:

```bash
export PYTHONPATH=packages/missionos-cli/src:packages/missionos-gateway/src
PYTHON=${PYTHON:-python}
URL=http://127.0.0.1:18881
PID=/tmp/missionos_fixture_gateway_18881.pid
LOG=/tmp/missionos_fixture_gateway_18881.log
STATE=/tmp/missionos_fixture_cli_18881_state.json
MAP=/tmp/missionos_fixture_map_18881.html

$PY -m missionos_cli --gateway-url "$URL" --state-path "$STATE" \
  gateway start --pid-path "$PID" --log-path "$LOG"
$PY -m missionos_cli --gateway-url "$URL" --state-path "$STATE" status
$PY -m missionos_cli --gateway-url "$URL" --state-path "$STATE" \
  say "Plan a fixture mission"
$PY -m missionos_cli --gateway-url "$URL" --state-path "$STATE" \
  job-status --task-id task_fixture_delivery
$PY -m missionos_cli --gateway-url "$URL" --state-path "$STATE" \
  map --task-id task_fixture_delivery --snapshot --no-open --output "$MAP"
$PY -m missionos_cli --gateway-url "$URL" --state-path "$STATE" \
  gateway stop --pid-path "$PID"
```

This smoke proves that the extracted CLI can start the extracted fixture Gateway
and exercise real HTTP routes. It does not prove production SITL, delivery
completion, or physical execution.
