# Mission-scoped prediction contract

MissionOS Core exposes a dependency-free `missionos_core.prediction` interface.
A mission-specific lightweight model implements `MissionPredictor`; the common
registry checks its binding, observation age, option identity, output finiteness,
and forecast horizon. Core has no ML, simulator, or model-file dependency.

The initial adapter is an optional local ExtraTrees predictor for staged block
stacking. It is not a universal physical model. The model input includes exact
simulator state and physical properties; it is conditioned on a fixed VLA and
controller macro, rather than arbitrary future motor sequences.

## Authority and scope

```text
LLM judges.
Human approves.
Rules constrain.
Executor acts.
Verifier checks.
Repair loops.
```

Prediction is `model_inferred` evidence. It never grants approval, dispatch,
physical execution, or completion. This first integration is an **opt-in local
simulator lab**, invoked by `missionos prediction serve-stacking`. It is not
connected to the Gateway's mission planner, human approval service, Mission
Assurance, or hardware dispatch. No LLM is invoked in this lab. A registered
deterministic stop policy consumes forecasts; the separately enabled simulator
executor applies its selected option. These facts must remain explicit in UI
and reports; a simulator-lab decision is not human approval.

The fixture tests and bounded live integration validate this prediction boundary,
not an autonomous mission commander or a complete mission-level integration.
Gateway/Agent consumption can be added subsequently using the same authority-free
forecast contract without treating forecasts as verified feasibility.

## Binding and input

`PredictionBinding` identifies model ID/digest, mission contract, VLA/policy
digest, environment contract, and input schema. The registry rejects a different
binding even when model names match. `PredictionRequest` adds unique request and
observation IDs, observation time, current state, and options with explicit
horizons and parameters. The complete request receives a canonical SHA-256.

Observation age defaults to sixty seconds. Future timestamps and stale snapshots
are unavailable. Duplicate or invalid options, mismatched bindings, backend
exceptions, mutation of input, nonfinite values, and incomplete forecasts cannot
be promoted to available predictions. JSON snapshots isolate provider input from
the caller. These checks are contract validation, not a comprehensive detector
of distribution shift or proof that observations describe the physical world.

The stacking schema requires ten slots of object poses, velocities, dimensions,
mass, friction, center of mass, cuboid indicator, accepted reference positions,
robot state, count, and the fixed macro plan. Unknown fields (including future
outcomes) and invalid shapes/values are rejected. Inactive slots are masked by
the frozen feature construction. Dimensions use meters, mass kilograms, rotation
quaternions, and control frequency 20 Hz.

Weights are explicit, digest-pinned **trusted local pickle files**. A matching
hash establishes identity, not safe deserialization of an untrusted file.
Checkpoints and private simulator modules are not shipped or automatically
fetched. Optional dependencies and the checkpoint's library compatibility must
be supplied by the operator. Ordinary CLI startup does not import the ML backend.

## Prediction, decisions, and measurement

`OptionForecast` contains option ID, horizon in seconds, risk score, and opaque
mission-specific future state. Risk is not represented as a calibrated probability
or certified safety guarantee. The stacking adapter emits poses and per-object
drop from a regression head and risk from a separate classifier; the heads need
not be physically consistent.

The local service exposes:

- `GET /health`: model/policy/mission binding and horizon.
- `POST /decide`: current observation plus options; persists its decision before
  returning it to the executor. Repeated request IDs are rejected.
- `POST /observe`: result tied to request digest, observation, chosen option,
  outcome role, and measured horizon. Duplicate observations are rejected.

The new stacking model forecasts continuation as placement plus hold (28.4 s)
and immediate banking as 14.2 s. The actor executes placement for 14.2 s; its
immediate result is deliberately **incomparable** with the 28.4 s forecast.
An explicitly labeled saved-state placement-then-hold branch supplies the matching
28.4 s observation, after which the actor's original state is restored. This
counterfactual is not silently described as the selected physical action. At ten
blocks, the same extra hold becomes the actual terminal scoring condition.

The service derives collapse from measured maximum per-object drop exceeding
30 mm and checks consistency with the simulator label. A matching risk prediction
is recorded as TP/FP/TN/FN, never as mission completion. These are observations
provided by an opt-in trusted simulator client, not authenticated hardware truth.
The service binds to loopback and is not a production multi-user execution API.

The configured rule stops when continue risk reaches the checkpoint threshold
and bank risk is lower. If the provider is unavailable with otherwise valid
current-state inputs, the session uses the explicitly labeled current-state
fallback (tilt above five degrees or drift above three millimeters). Stale inputs
and contract mismatches are rejected, not interpreted as safe continuation.
Neither fallback nor a bank forecast guarantees that stopping prevents collapse.

## Running and validation

From a source checkout with `missionos-core`, `missionos-cli`, NumPy and the
checkpoint-compatible scikit-learn installed:

```bash
python -m missionos_cli prediction serve-stacking \
  --trusted-model "$STACK_MODEL" --model-sha256 "$STACK_MODEL_SHA256" \
  --policy-sha256 "$STACK_POLICY_SHA256" --output "$STACK_SESSION" \
  --allow-simulator-decisions
```

The output directory must be new. `ready.json` gives the dynamically allocated
loopback port. A separate simulator executor requires its own explicit opt-in:

```bash
python scripts/run_stacking_prediction_integration.py \
  --simulator-module-dir "$STACK_SIM_MODULES" --output-root "$STACK_OUTPUT" \
  --service-url "$STACK_SERVICE_URL" --policy-sha256 "$STACK_POLICY_SHA256" \
  --seed 67002 --allow-simulator
```

That executor requires the external staged simulator's `game` and `sim` modules
and a running frozen VLA RPC service in the output directory. The module adapter
must provide `PhysicalTower`, state snapshots/restoration, `gate_inputs`,
`transition`, and `future_after_wait`. It is an opt-in integration harness, not a
self-contained public simulator distribution or an arbitrary plugin importer.
The external module directory must be trusted.

Fixture and real HTTP boundary tests require no weights or simulator:

```bash
PYTHONPATH=packages/missionos-core/src:packages/missionos-cli/src:. \
  python -m pytest -q tests/contract/test_mission_prediction.py \
  tests/e2e/test_mission_prediction_http.py
```

See the [integration results](mission-prediction-stacking-integration.md) for the
bounded real-model/simulator verification. A new mission must supply its own
adapter, outcome definition, applicable controller/model binding, and held-out
comparison to a simple baseline. Interface portability alone is not predictive
generalization or evidence of score improvement.
