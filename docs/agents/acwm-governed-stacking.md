# Online ACWM through governed stacking execution

The opt-in ACWM composition reuses the stacking Mission Assurance, bounded human
preapproval, Rules, ticketed executor, and measured-outcome verifier. The adapter
supplies a neural future-video forecast before the next placement begins. Actual
DeepSeek API judgments choose `continue` or `hold`; the latter maps to the existing
14.2-second bank macro. Predictions and judgments remain evidence until Rules
validate an approved policy and issue a ticket.

## Forecast and judgment contract

`GovernedACWMStackingSession` uses the frozen `stacking.current_image_macro.v1`
contract. Its input contains only the current image, object poses and velocities,
physical properties, robot state, count, and registered placement plan. Future
motor actions and outcome labels are rejected before invoking the backend.

The forecast contains one `continue` option covering the next 14.2-second
placement. Its score is the frozen visual readout's uncalibrated classifier
output. It is not a calibrated collapse probability. The full generated video
stays with the inference evidence; DeepSeek receives the resulting structured
forecast, its scope, and the mission situation.

ACWM does not supply a bank forecast or a forecast covering the subsequent
terminal hold. The Assurance prompt states those gaps explicitly. The score
comparison record has null bank risk and null proxy point values. It does not
reuse ExtraTrees' two-branch proxy calculation, fabricate a bank forecast, or
instruct the judge to copy the reference threshold policy. The original ACWM
threshold decision is retained separately for audit.

Unavailable or invalid forecasts prevent judgment and dispatch. A failed or
invalid DeepSeek response also prevents dispatch; there is no local model or
heuristic substitution.

## Opt-in service

An independently provisioned, loopback ACWM backend must implement the validated
NPZ request and digest-bound JSON response used by `StackingACWMPredictor`.
Weights, the external simulator environment, and credentials are not distributed
with this repository. Set `DEEPSEEK_API_KEY` in the host process only.

```sh
missionos prediction serve-stacking-acwm-mission \
  --backend-url http://127.0.0.1:18765/ \
  --model-sha256 "$ACWM_DIGEST" --readout-sha256 "$READOUT_DIGEST" \
  --policy-sha256 "$VLA_DIGEST" --output "$RUN/service" \
  --llm-model deepseek-flash --seed 68000 --seed 68001 \
  --approve-simulator-mission --operator "$AUTHORIZATION_REFERENCE" \
  --port 18870
```

Approval must represent a real operator authorization of this bounded simulator
scope. Omitting the approval flag leaves execution unauthorized. The service
binds to loopback. A containerized simulator may use an explicitly configured
host bridge, never a public unauthenticated endpoint.

Use `scripts/run_stacking_mission_e2e.py` with `--allow-simulator`, an explicitly
supplied simulator module directory, the matching VLA digest, and the service
URL. The runner takes the ACWM camera image at the current pre-approach state,
restores the normal VLA camera, requests a judgment, and obtains a bound dispatch
ticket before any placement transition. Only afterward does the simulator invoke
SmolVLA. The simulator pauses during prediction and remote judgment.

The existing simulator macro stages the grasp and uses SmolVLA for lowering and
release. This is a governed stacking integration, not a demonstration of autonomous
picking from an arbitrary scene.

## Observation semantics

The verifier derives collapse and score from measured per-object drops and counts.
A selected continuation is comparable with the 14.2-second ACWM forecast. A bank
outcome can be measured and scored, but its forecast comparison is `incomparable`
because no bank forecast exists. The terminal hold after the tenth placement is
likewise measured separately and is outside the ACWM forecast horizon. An
incomparable forecast is not an unverified physical outcome.

The inherited authority checks bind mission, policy approval, state revision,
observation identity and age, state digest, decision digest, option budget, and
single-use ticket. The next decision requires the previous execution's verified
receipt. These are simulator integration checks; hardware execution is not enabled.

## Verification

```sh
PYTHONPATH=packages/missionos-core/src:packages/missionos-cli/src:. \
  python -m pytest tests/e2e/test_stacking_acwm_mission_boundary.py \
  tests/e2e/test_stacking_mission_boundary.py tests/contract/test_stacking_acwm.py -q
```

The HTTP tests use synthetic forecasts and judgments while exercising real
Prediction, Assurance, policy storage, Rules, and observation validation. They
cover both LLM choices, including a bank choice differing from the reference
threshold decision, rejection before backend invocation, and the absence of
fabricated bank evidence. Live model runs must be reported separately.

## Recorded live demonstration

The [two-case integration report](../assets/acwm-governed-stacking-20260921/REPORT.md)
records 15 actual DeepSeek judgments and terminal scores 9 and 4. Model backends
were stopped and the temporary GPU deleted after the demonstration.

In this adapter, the executor is the ticket-controlled simulator runner, not the
Gateway Executor service. `dispatch.executor_invoked = false` applies to ticket
issuance; the later verifier receipt records `simulator_execution_invoked = true`
and motor-loop invocation evidence after execution.
