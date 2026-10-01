# Step 2: onboard urban decisions

Step 2 asks whether native visual action proposals and future predictions reach
usable performance and participate in a verified ship-to-urban delivery and
return. Acceptance is based on absolute mission requirements, not superiority
over an ordinary planner. In an idealized, mapped scene, a rule can already be
very strong; beating it is neither required nor the purpose of this stage.

This scope supersedes the earlier comparative interpretation, following the
user's 2026-09-25 clarification. Historical comparisons remain evidence of their
own experiments, but no longer gate Step 2 or further bounded integration work.
Do not construct harder scenes merely to make the rules lose. A future request
to establish comparative superiority would be a separate experiment.

The [initial uncertainty filter](ship-onboard-uncertainty.md) is a separate,
model-free camera/PX4 integration. It preserves the initial-hold authority
boundary and records matched stopping-rule comparisons. Its one-obstacle world
does not transfer the two-dynamic-route CPU results or qualify native WAM.

## Acceptance gates

1. Receive continuous RGB from a sensor physically attached to the simulated
   aircraft. Correct image observations with contemporaneous PX4 ego state.
2. Use only observed images, ego state, mapped geometry and approved route
   alternatives in selectors. Scenario labels, actor scripts and future truth
   are verifier-only inputs.
3. Bind the online proposal to exact input images and the approved plan. A model
   never approves or uploads its own route. Stale or malformed input leaves the
   aircraft in hold. A direct route additionally requires fresh clearance.
4. Freeze model identity, prompts, output semantics, intended operating envelope
   and absolute performance requirements before qualification trials.
   A general VLM serving a discrete action or forecast interface must remain
   labelled as such; it is not a flight-trained VLA or action-conditioned WAM.
5. Demonstrate the native VLA input/proposal/permit/motion chain and native WAM
   input/candidate-conditioned prediction/mission-decision chain in the flight
   records. A WAM-supported decision may agree with a rule. No action divergence
   or extra successful delivery relative to a rule is required. Merely logging
   an unused prediction does not demonstrate this integration.
6. Record actual route motion, delivery, recovery, contacts, elapsed time,
   prediction error and end-to-end inference delay. Reopen saved images and raw
   flight evidence to verify the absolute requirements.
7. Report completed missions against all attempts, including rejected proposals,
   fallbacks and failures. A successful rule fallback is a mission recovery,
   not a successfully executed native-model path. Do not claim moving-ship,
   10-aircraft, real-parcel or physical flight validation.

## Absolute qualification protocol

Before new model qualification runs, record a numeric target and measurement
method for each applicable requirement: delivery-and-return completion rate and
trial count, obstacle/clearance or other mission-relevant prediction error,
forecast horizon, input age at use, total decision latency, and mission time
budget. Choose these from the intended mission and current execution limits,
not from the best rule's score or from the candidate's observed test results.
Report the bounded trial count without inferring field reliability.

Existing execution constraints remain in force: no unauthorized dispatch,
fresh direct-route clearance, parcel/contact/stability verification, and stable
deck recovery. The AeroVLA path currently requires input age within two seconds.
The local VLM experiment has a separate 20-second hold-time bound; it is not an
automatically valid latency target for every native WAM. A longer WAM inference
can be considered only with an explicit hold or asynchronous-use contract and
fresh validation at use. Do not extend a deadline retrospectively to count a
stale prediction as usable.

WAM qualification should assess the information consumed by mission decisions,
such as predicted obstacle location or clearance at a declared horizon. Whole
image fidelity and a repeat-last-image comparator may remain historical
diagnostics; losing that comparison alone does not establish mission
unsuitability. Conversely, a visually plausible frame does not establish usable
prediction accuracy. Learned-model integration can proceed without first
demonstrating an advantage over fixed, velocity or stopping-aware rules.

## Current observation envelope

The onboard sensor is 640 by 360, forward-facing, with a 60 degree horizontal
field of view. A known red box crosses a known plane 100 m ahead of the entry
hold. Its widest contiguous horizontal image band is projected using PX4
position and attitude, including the band's vertical bearing. The central band
breaks equal-width ties. This allows an exposed upper band to supply the width
when a foreground building covers a lower edge. It does not infer an entirely
hidden object or establish general occlusion recovery. This is calibrated
synthetic perception, not general object detection.
Gazebo obstacle positions are used only for independent projection-error and
collision checks. Actor time is wall time, and selector elapsed time is also wall
time; the separate sensor simulation timestamp must advance.

The PX4 heading estimate differed from rendered camera heading during initial
development. A static, surveyed cyan marker at east -14 m and north one metre
before the obstacle face corrects the yaw bias. The marker is visible map
information, not an obstacle-state oracle. Reverification checks image/validation
pose clock alignment within 0.3 s and position error within a declared 2 m
envelope. The marker must remain visible; missing calibration fails closed.

The current surveyed-marker yaw-correction envelope is ±0.15 rad. It was
expanded from ±0.08 rad after a 1 km transit stopped before an urban decision
at approximately 0.0818 rad of observed bias. A separately frozen 525-case
pinhole rendering grid across camera yaw, estimator bias, ego translation and
obstacle position had maximum decoded error 0.188 m (declared limit 0.3 m).
All 44 saved observations from the six compact flights remained numerically
unchanged. The runtime 2 m projection-validation limit and image/pose freshness
checks remain unchanged. This is bounded synthetic calibration, not a claim
of general camera calibration or real-world obstacle-detection accuracy.

Obstacle transport updates run in a separate process. Synchronous Python
transport calls in the sensor process otherwise delayed callbacks. Aircraft
positions are never assigned by the actor process. PX4 flies every mission leg.

The entire simulation, hardware-free, requires `--approve-sitl`. Models remain
off unless a model policy is explicitly selected. Runtime receipts and raw RGB
are local evidence; publish only reviewed summaries and fixture data.

## Local learned-model boundary

`onboard_vlm_action` and `onboard_vlm_forecast` opt into the installed loopback
Ollama `gemma4:26b` model, pinned by digest in `ship_onboard_model.py`. No download,
hosted model, credential or cloud billing is used. The frozen comparison uses
`onboard_stopping` versus `onboard_vlm_forecast`, with one run per policy in
`short_clear`, `long_block` and `brake_stop`. Cruise speed is fixed at 12 m/s for
the model prompt's route-cost contract. The model gets images and image-derived
position history; it never gets the case name, script, future trace or truth
positions. Inference is bounded to 20 wall seconds while PX4 stays in hold.

The two model policies intentionally consume different fields. The action
policy consumes `action`; the forecast policy selects from
`predicted_clearance_seconds`. Natural-language reasoning never silently
overrides either field. Log inconsistent fields as a model error. Both choices
are approved alternatives, and direct flight still requires observed clearance.

The action/forecast interface is a **general VLM experiment**. It is not an
action-conditioned video predictor or a flight-trained VLA. Keep `vla_invoked`
and `wam_invoked` false. `onboard-compare` produces a normalized
`runtime_invocation_evidence.v1` from the already captured HTTP request/response
hashes and invocation times. It does not reinvoke the model during verification.

## Reproduction and evidence

Run from the repository with its installed CLI and dependencies:

```sh
missionos ship-delivery run-sitl \
  --scenario examples/fixture_missions/ship_delivery/urban-compact.json \
  --urban-case brake_stop --urban-policy onboard_stopping \
  --approve-sitl --output-dir /tmp/onboard-rule-new
```

Use a new directory for every attempt. Keep all failures. The complete comparison
requires six `--run-dir` arguments to `missionos ship-delivery onboard-compare`.
The comparator reopens RGB files, raw PX4 evidence, configuration, plan, world,
deployed worker code and model artifacts. It requires the original implementation
hashes, rejects mixed implementations/worlds and rebinds relocated stdout/stderr
to the copied bytes without rewriting the original receipt.

Generate the portable report with `scripts/report_ship_onboard.py`, supplying the
same six `--run-dir` arguments and an `--output-dir`. Supply every attempt,
including failed ones, with repeated `--attempt-dir` arguments so the completion
denominator remains visible. Full raw evidence belongs in
a task-owned private archive; the report uses reviewed, selected fields only.

The 2026-09-25 V2 batch used runtime revision `39ba695`: seven attempts produced
six completed, independently reverified flights. One attempt failed during
climb before urban sensing or model invocation, with repeated IMU timestamps
and stalled PX4 position updates. Its cause remains unresolved. The remaining
matrix and one bounded infrastructure retry used unchanged runtime sources.
The [report](../examples/ship-onboard-report/REPORT-ja.md) compares completed
flights conditionally and retains all attempts. Relevant contract tests passed
219/219; the actual `run-sitl` and `onboard-compare` CLI entrypoints exercised
PX4/Gazebo, loopback model calls, saved-input validation, delivery and recovery.
Browser checks covered all three replay cases, play/pause, image/time sliders,
and 1440-, 736-, and 320-pixel layouts without console errors.

## Native model admission remains open

The [stationary native ANWM mode](ship-anwm-static.md) adds an opt-in
candidate-view/decision path with absolute position and response-time limits.
Its stationary use does not qualify a moving-object temporal forecast.
The [same-flight integration](ship-native-integration.md) explicitly combines
the native VLA maneuver with newly captured WAM history and a verified route.

The local VLM comparison does not close native VLA/WAM integration. The checked
upstream candidates have materially different contracts:

- [ANWM](https://github.com/EmbodiedCity/ANWM.code) generates future aerial RGB
  conditioned on 4-DoF candidate actions and depth. The existing MissionOS ANWM
  audit used sixteen real context frames at 0.25-second simulation intervals,
  a released checkpoint and CUDA inference. This experiment's five irregular
  wall-time RGB samples are not a compatible replacement. Its goal-image score
  must not be relabelled as collision risk or time-to-clear.
  The [released checkpoint](https://huggingface.co/EmbodiedCity/ANWM/tree/main)
  is 18.1 GB, also larger than the local free space at the access check.
- [WorldVLN](https://github.com/EmbodiedCity/WorldVLN.code) uses a stateful visual
  history and returns 6-DoF delta actions. Its released service defaults to
  16-frame segments at 16 fps. The [weight repository](https://huggingface.co/EmbodiedCity/WorldVLN/tree/main)
  lists 36.9 GB. This Mac had about 7 GB free during the access check; no checkpoint
  was downloaded and no GPU VM was provisioned.

Before activating either, establish a usable model execution environment,
capture its actual required sensor history, validate units/frame/timing and
feedback semantics, and qualify the returned prediction for the chosen mission
decision against the absolute requirements above, including real inference
delay. A rule-superiority or oracle-headroom gate is not required. Do not mark
Step 2 complete from a generic VLM proxy or from model invocation alone.
