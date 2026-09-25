# PX4 and TurtleBot3 navigation predictions with Jev

The native PX4 route-deviation, mission-designer, task-recovery and TurtleBot3
recovery-checkpoint adapters can invoke an operator-supplied navigation predictor
before Mission Assurance.
They use the shared Mission Incident graph and its configured Jev/LLM judge.
No compatible navigation model checkpoint is included: the existing stacking
ACWM and ExtraTrees models have different state/action contracts.

## Runtime boundary

```text
runtime observation -> Recovery candidate -> Rules feasibility
  -> navigation WAM HTTP request -> evidence admission -> Jev / Assurance
  -> human approval or previously approved bounded policy
  -> fresh observation and Rules revalidation -> Executor -> Verifier
```

The graph's `navigation_backend` keyword is supplied by trusted backend code.
Operator mission text and the diagnostic HTTP route cannot select it. Existing
caller-supplied generic Prediction evidence remains a separate integration path;
it cannot override a required navigation provider.

The PX4 play/delivery advisory paths also declare their backend. Their existing
observation projections lack a source acquisition timestamp, so `required` mode
blocks that advisory judgment until timestamped observations are supplied.
Those advisory paths do not dispatch a WAM-selected recovery action.

Each request binds the current observation, execution/task identity, exact
Recovery action and parameters, compiled candidate, prediction horizon, model
digest, active policy digest, and backend contracts. `hold` is an alternative
forecast when the candidate is another action. Forecasting hold does not prove
that a hold executor is available or dispatch it. Assurance still selects only
the existing bounded mission response; it cannot invent a new path.

Prediction is `model_inferred` evidence. Transport invocation evidence records
request/response hashes, timing, and contract validation. It does not prove that
the remote server ran the declared weights, establish prediction accuracy, or
confer approval, feasibility, execution, or completion authority.

## Configuration

Both switches default to `off`:

```dotenv
MISSIONOS_NAVIGATION_WAM_MODE=off
MISSIONOS_NAVIGATION_WAM_CONFIG=
MISSIONOS_JEV_MODE=off
```

| WAM mode | Behavior |
| --- | --- |
| `off` | No manifest read or predictor request; preserve existing judgment. |
| `shadow` | Call the configured predictor and record evidence separately; do not add its forecasts to the judge's prompt. Failures do not change the incumbent decision. |
| `required` | Admit current, matching forecasts into the judge's prompt. Missing, malformed, stale or mismatched evidence stops the graph before judgment. |

Jev's independent modes remain `off`, `shadow`, and `primary`. In `shadow`, the
configured Assurance LLM retains the decision. In `primary`, Jev supplies the
bounded response and provider failures escalate without fallback. Its confidence
is not a calibrated mission-success probability. See
[the integrated graph contract](integrated-assurance-graph.md).

`scripts/run_agent_graph_gateway.py` also accepts `--navigation-wam-mode` and
`--navigation-wam-config`. Precedence is CLI, process environment, state-directory
`.env`, then default. Relative manifest paths use the state directory. Invalid
modes or missing active-mode files stop the launcher before secret lookup.
Restart the Gateway and affected simulator runtime after configuration changes.
The launcher retains its separate `--enable-live-sitl` opt-in.

Use `TYPESAFE_API_KEY` through the existing protected process environment or the
launcher's Secret Manager integration. Do not place keys in the WAM manifest,
source tree, URLs, or recorded evidence.

The operator-owned manifest has this shape (replace descriptive digest values
with lowercase SHA-256 strings before use):

```json
{
  "schema_version": "missionos_navigation_wam_config.v1",
  "backends": {
    "px4": {
      "endpoint": "http://127.0.0.1:18880/predict",
      "provider_kind": "learned_model",
      "binding": {
        "model_id": "your-px4-navigation-model",
        "model_sha256": "SHA256_OF_MODEL",
        "mission_contract": "missionos.navigation.px4.v1",
        "policy_sha256": "SHA256_OF_ACTIVE_RECOVERY_POLICY",
        "environment_contract": "px4_gazebo_sitl.v1",
        "input_schema": "missionos_navigation_prediction_input.v1"
      },
      "horizon_seconds": 5,
      "max_age_seconds": 30,
      "timeout_seconds": 5
    }
  }
}
```

For a `nav2` entry use `missionos.navigation.nav2.v1` and
`ros2_nav2_turtlebot3_sim.v1`; the input schema is the same. A manifest may contain
either backend or both. `policy_sha256` is `prediction_digest(active_policy)`
from `missionos_core.prediction`: the complete PX4 recovery policy, or the TB3
proposal's autonomy envelope. A different model/policy/environment requires a
new compatible manifest and judgment. A manifest is an operator assertion of
compatibility, not evidence that a model has been validated for navigation.

HTTP is limited to loopback addresses; other endpoints require HTTPS. Redirects,
URL credentials, query strings and fragments are rejected. The socket timeout is
at most 30 seconds and response bodies are limited to 1 MiB. The client does not transmit API keys.
Private authenticated deployment can put the service behind an operator-managed
local proxy. `provider_kind: fixture` explicitly labels test transport results.

## Model service contract

The client POSTs a JSON `PredictionRequest` with `request_id`, `observation_id`,
`observed_at` (UTC epoch seconds), `binding`, `state`, and `options`. State contains
`runtime_telemetry` and `compiled_candidate`; each option contains `option_id`,
`horizon_seconds`, and exact `parameters`. No future actions, outcome labels or
post-execution observations are supplied as prediction inputs.

The response uses `missionos_core_prediction.v1`. It must echo the request ID,
observation ID, binding and canonical request SHA-256, return `status: available`
and `verification_basis: model_inferred`, and contain exactly one forecast per
requested option. Every forecast contains its matching option ID/horizon,
finite `risk_score` in `[0, 1]`, and a `future_state` object. Models that predict
only image-goal compatibility may instead use `risk_score: null` with the strict
typed `future_state.goal_compatibility` contract described in
[aerial model evaluation](aerial-wam-model-evaluation.md). Missing risk without
that typed metric is rejected; goal discrepancy is not collision probability.
The four fields
`approval_recorded`, `dispatch_authority_created`, `physical_execution_invoked`,
and `completion_claimed` must all be false. See the executable fixture service in
[`smoke_navigation_wam_jev.py`](../../scripts/smoke_navigation_wam_jev.py).

## Observation and dispatch freshness

PX4 uses its source telemetry's capture time, including when a route-deviation
sample is cached. TB3 preserves the plan-only Nav2 observation time and binds
selected path geometry, available cost/clearance features, and both costmaps'
content digests. Source snapshot timestamps remain separate from map contents;
a timestamp update alone is not a changed world state.
The Nav2 bridge supplies `global_costmap_content_sha256` and
`local_costmap_content_sha256`, covering frame, grid geometry, origin and cell
values. Older bridge responses without these fields cannot support required
prediction; restart/redeploy the updated bridge with the Gateway.
The graph's creation time cannot make missing or old sensor evidence fresh.
The prediction must remain within the configured age limit after inference and
at dispatch. Required mode refuses missing current observations, changed state,
changed candidates, policy/model drift, or altered prediction evidence.

Fresh source observations are supplied independently at native dispatch
revalidation. Metadata such as a later sample timestamp does not itself change
the modeled state. Changes in actual state/path/costmaps require a fresh
prediction and judgment; the adapter does not silently refresh an approved
proposal. This strict comparison may block a moving or noisy scenario until a
new checkpoint is judged.

Enabling `required` after a checkpoint was judged with WAM off/shadow invalidates
that checkpoint for dispatch. Conversely, switching WAM off does not remove the
prediction requirements of a checkpoint that relied on forecasts.

## E2E / Runtime Verification

From a checkout with dependencies installed:

```sh
export PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src
python scripts/smoke_navigation_wam_jev.py
python scripts/smoke_jev_modes_gateway.py
python -m pytest -q tests/contract/test_navigation_wam_jev_graph.py
```

The navigation smoke starts a real loopback HTTP predictor fixture and exercises
the production HTTP client and ADK graph for both backends. It checks WAM/Jev
mode selection, admitted evidence in the judgment prompt, rejection before
judgment, and the absence of approval/execution authority. It also starts a fresh
Gateway and calls the PX4 task-proposal route over HTTP. The valid case persists
a bounded proposal; a provider failure returns an evaluation with a blocked
verdict and no durable proposal, without calling either judge. Local verification
passed all 34 graph cases and three Gateway cases, including typed goal costs
with unassessed risk for both backends and through the PX4 HTTP proposal route.

The separate Jev Gateway smoke
restarts a temporary Gateway for each Jev mode and uses real HTTP requests.
The contract tests also enter through the actual native PX4 and TB3 adapters.
All use fixture model outputs; none measures learned WAM/Jev quality,
Gazebo/Nav2 motion, hardware behavior, or mission completion. Live ROS costmap
acquisition with the updated bridge remains untested.
