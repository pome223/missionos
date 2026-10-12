# Inflight operator HTTP boundary

The host registers each run through
`get_inflight_approval_service().register_run(run_id=..., mission_id=...,
vehicle_id=..., ledger=..., snapshot=...)`. Use one private ledger per run.
Registration is not exposed over HTTP. The worker/service creates requests;
the model cannot register a run, provide an operator token or produce a grant.

`snapshot()` reads current worker evidence and returns exactly `run_active`,
`simulation_time_s`, `revision`, `scope`, `observation`, `pending_request_id`.
Observation fields follow the inflight ledger contract. Read these from the
current running worker, including its original simulation clock; never from
browser input. After the run ends unregister it. A restarted host must rebuild
registration from verified run ownership, not HTTP parameters. Pending authority
must not survive a machine reboot with a different monotonic clock.

Routes under `/missionos/starship/inflight/runs/{run_id}/missions/{mission_id}/vehicles/{vehicle_id}`:

- `GET /pending`: the worker's current request, fixed deadlines and receipt.
- `GET /requests/{request_id}`: a request bound to that run/mission/vehicle.
- `POST /requests/{request_id}/approve` or `/reject`: echo `request_sha256`
  and `observed_state_sha256` from the displayed request. A candidate-bound
  request also requires its displayed `candidate_sha256`; M3 uses this
  three-hash form. Legacy scope-only requests use the two-hash form. No other
  body fields are accepted.

All evidence and response routes require `Authorization: Bearer ...` for the
configured local operator. If the Gateway has an API key, its existing
`X-API-Key` requirement remains in addition to operator authentication. The
served local console has a narrow same-origin exception only to browser-origin
policy, not to either configured API-key or operator-token checks. Tokens are
held only in the page's memory and cleared on disconnect. No token appears in
the URL, storage, request artifact, report or model prompt.

There is no public request-creation, registration or grant-consumption route.
The response handler independently reads liveness, current revision/scope and
fresh worker observation before recording a response. The ledger repeats the
fixed deadline and request-hash checks. The worker separately consumes once,
rechecks current state and applies physical constraints before any command.
Time can advance between HTTP response and consumption; an expired or changed
state must not regain authority because the operator responded earlier.

The UI polls, displays scope/delta/state hash/original deadlines/fallback and
offers approve/reject. Receipt delivery means the HTTP client received a record;
it does not prove that an identified human saw it. This authenticates a configured
bearer principal, not an SSO identity. It does not prove a physical action or a
safe flight outcome.

## E2E / Runtime Verification

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python scripts/smoke_starship_inflight_gateway.py \
  --output-dir output/starship-inflight-http-smoke
```

Scenario: scripted local operator with fixture worker observations and fresh
temporary SQLite ledgers. Boundary: real MissionOS Gateway served on an owned
loopback socket, actual HTTP pending delivery and approve/reject requests, then
independent worker-side consumption. Assertions cover wrong mission, missing
authentication, repeated HTTP response, second consumption, expired approval,
and no-response fallback. `summary.json` records every check and explicitly
identifies the fixture and scripted operator. There are zero simulator runs,
model calls and executor commands. The smoke does not establish browser clicks,
live worker integration, person identity or successful return.

Staged verification: all 14 real-HTTP smoke assertions passed, including the
configured Gateway API-key requirement remaining active. The approval
ledger's 44 unit cases and the Gateway's HTTP/client contract cases passed.
The UI echoes the displayed request and observation hashes, plus the candidate
hash when present, and keeps the bearer out of the JSON body. Its client
contract uses a DOM double, not an observed browser session.
