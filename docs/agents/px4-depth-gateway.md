# PX4 depth navigation through CLI and Gateway

This bounded navigation mode uses the normal packaged CLI, authenticated
Gateway, durable TaskStore, `approve-sitl-execution`, `execute-sitl` and
`job-status` surfaces. It has its own `px4_depth_navigation` task kind and reuses
the RGB-D/offboard urban executor. It does **not** replace the existing delivery
AUTO/horizontal runner, arbitrary coordinate missions or chat planning.

## Scope and request

`POST /px4-gazebo/depth-navigation/prepare` accepts exactly `{"scene":"climb"}`
(or `gap`/`detour`). It creates a pending task without simulator activity or
approval. The corresponding CLI is `missionos prepare-px4-depth --scene climb`.
Profiles use the unchanged `headroom_<scene>_0` geometry and routes. Reusing a
profile is an integration check, not a new headroom cohort or held-out study.
In the detour profile the supplied choices are forward/left/right, not climb.

The request binds the scene hash, route set, goal, runtime source hashes and
pinned simulator image. The selection input is the incomplete prior map plus
sixteen aligned, native RGB-D/pose frames. Truth geometry remains outside that
input and is used by the independent safety filter after selection. Unknown
space is not certified free. No model or forecast is involved.

## Authority and lifecycle

The existing approval endpoint requires `explicit_execution_approval: true`.
For this task kind it creates a source-bound depth approval rather than claiming
a delivery-scenario approval. Approval permits selection of **one** declared
route and landing in this simulator profile. It expires after ten minutes and
cannot authorize another task, changed code, changed routes or physical hardware.

The existing execute endpoint requires the stored approval ID and
`live_flight_mode: true`. Upload-only and deviation-agent modes are rejected.
Both normal live-SITL gates and the urban executor's explicit gate must be set.
Per-task and shared simulator file locks prevent concurrent replay across
Gateway workers. The task becomes running and approval is consumed before
starting the executor. Failure leaves a failed task and a consumed approval;
it never substitutes another route or automatically retries.

The original controller retains its fresh-observation, start-pose, speed, yaw,
geofence, swept-envelope, static-world and contact guards. Signed route dispatch
expires after four seconds. Contact-probe positive response, observed removal,
subscription release and process exit zero precede flight. On host-side failure,
the controller is signalled to land before owned-container cleanup. If that
recovery cannot be observed, the task cannot claim landing or completion.

The verifier reads original flight records, source bindings, contact-process
receipt, Gateway approval reference, pinned image, observed trajectory, goal
dwell, landing/disarm and cleanup. A successful executor return or ACK alone
cannot complete the task. Public/CLI results explicitly leave payload delivery
and physical execution unclaimed. The progress artifact's selected route is
not execution evidence. The normal task APIs provide durable progress and result;
this slice does not add the urban scene to the map/watch visualizer.

## Configure the Gateway

Use the branch containing this integration and restart the Gateway before
validation. Configure host-side paths; none are accepted from an HTTP request.
Pinned apartment assets may be prepared with
`python scripts/px4_urban_wam_trial.py --phase fetch-assets --assets-dir "$ASSETS"`.
The executor verifies their pinned hashes and uses image
`sha256:79968fe25aa19d51c49fbd4a863ea9380f4efe6d9afabd1c579ddeffeb8b8c93`.

```sh
export RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_EXECUTION=1
export RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_LIVE_FLIGHT=1
export RUN_PX4_URBAN_WAM_TRIAL=1
export MISSIONOS_PX4_DEPTH_ASSETS="$ASSETS"
export MISSIONOS_PX4_DEPTH_ARTIFACT_ROOT="$OUTPUT"
```

`$ASSETS` and `$OUTPUT` are caller-local directories. Each task requires a new
output directory and at least 512 MiB free space. Raw observations and dispatch
keys remain local. Three-dimensional obstacle bounds and the camera are a static
simulator setup, not a sensor-certified map of the real world.

## Verification

Run against a newly started, isolated production Gateway with real HTTP and
the packaged CLI module (`python -m missionos_cli`). The default check never enables simulator execution:

```sh
export PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src
python scripts/check_px4_depth_gateway.py --output-dir "$HTTP_RUN" --scene all
python -m pytest tests/contract/test_px4_depth_navigation.py -q
```

The same command with `--live` requires all three explicit environment gates
above. It uses one fresh simulator per scene, stops on the first failure, and
checks that replay is rejected after completion:

```sh
python scripts/check_px4_depth_gateway.py --output-dir "$LIVE_RUN" --scene all --live
```

The frozen twelve-case R2 report is unchanged. This integration does not execute
WAM/GPU/training work. Separate WAM capability evaluation uses
[absolute acceptance bounds](aerial-wam-capability-evaluation.md), without requiring
superiority over the depth method or an ideal full-map planner.

## Planned stop and reobservation

`POST /px4-gazebo/depth-navigation/prepare` also accepts
`{"scene":"gap","reobserve":true}`. The CLI equivalent is
`missionos prepare-px4-depth --scene gap --reobserve`. Other scenes, nonboolean
flags and client-supplied route/asset fields are rejected. Preparation still
creates a pending `px4_depth_navigation` task without flying or approving it.
The existing `execute-sitl` and `job-status` commands use that same task ID.

The opt-in request binds the original gap scene, supplied approach/resume routes,
checkpoint `(3,0,3)`, protocol, pinned simulator image and controller/adapter/verifier
source hashes. A scripted static obstacle is inserted after eastward departure
passes 1 m. Only the planner's partial map and measured RGB-D enter selection;
the common safety gate independently sees the declared scene truth.

Approval permits one approach, one planned checkpoint, one new observation and
at most one supplied resume decision, followed by landing. The old single-route
approval scope cannot authorize this option. Both stages use separate one-shot
signed dispatch files with four-second expiry, five-second maximum observation
age, stationary-pose checks and the same approval reference. Resume images must
postdate both the observed stop and obstacle appearance. This authority does not
permit repeated replanning, another task, changed code/routes or hardware.

`px4_depth_navigation_lifecycle.entries` is an ordered durable record on the
original task. It distinguishes route selection from observed controller
consumption, safety-gate outcome and route dispatch. Checkpoint, landing and
disarm records contain observed telemetry. `job-status` exposes the current phase
and verified replan/abort result. Source verification, raw RGB-D re-scoring,
trajectory/dwell/contact checks and container cleanup precede completion.

A verified safe abort has `result_status=safe_aborted`, task `status=failed`,
`destination_reached=false` and `landing_and_disarm_observed=true`. An exception
or missing evidence instead leaves unknown terminal fields and a failed task;
neither executor success nor an ACK is completion evidence. Both consume the
approval and disallow replay. Host failure uses the existing signal-to-land and
owned-container cleanup boundary. No automatic reflight is performed.

```sh
python scripts/check_px4_depth_gateway.py --output-dir "$HTTP_RUN" --scene gap --reobserve
# All three documented server-side SITL opt-ins and pinned assets are required:
python scripts/check_px4_depth_gateway.py --output-dir "$LIVE_RUN" --scene gap --reobserve --live
python -m pytest tests/contract/test_px4_reobserve_navigation.py -q
```

The first command uses a newly started real loopback Gateway and packaged CLI,
testing authentication, preparation, approval and opt-in refusal without Docker.
The second additionally exercises the controller, sensors and raw verifier in
one simulator flight. Each requires a fresh output directory. This mode is a
fixed supplied-route integration, not a general recovery planner, a WAM result,
or continuous emergency avoidance. No WAM/Jev/GPU/training calls occur.

## Observed integration checks (2026-09-24–25)

All three profiles completed through CLI → authenticated Gateway → depth selection → PX4/Gazebo → same-task verification. A result-schema merge-order defect was then corrected; original records remain unchanged. A fresh climb run after Gateway restart completed with the corrected schema. The four flights all reached the goal, landed/disarmed and removed their containers. No model/GPU/training calls occurred. See the [reviewed results](../assets/px4-depth-gateway-20260924/README.md) and section 19 of the [Japanese technical report](aerial-wam-px4-technical-report-20260922.md).

One subsequent `gap --reobserve` integration flight also completed through a new
Gateway and the same task: initial forward choice, observed checkpoint stop,
fresh depth, left detour, goal dwell, landing/disarm, independent verification and
container removal. Approval replay was rejected. The full suite passed 3276 tests.
See the separate [Gateway reobservation evidence and replay](../assets/px4-reobserve-gateway-20260925/README.md).
