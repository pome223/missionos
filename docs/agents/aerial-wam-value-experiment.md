# Aerial WAM value experiment: navigate between buildings in 3D

Status: **design, not an observed navigation benefit**. The user's objective is
flight through building gaps, climbing above obstructions and lateral avoidance
on the way to a destination. This supersedes the earlier inspection-view proposal.
The calibration audit remains valid, but acquiring a better inspection image is
not the mission endpoint.

## Intended behavior and current gap

At each decision, predict the consequences of forward, climbing and detour
candidates; select a useful admissible route; execute a short verified portion;
observe again and replan until the destination is reached. The experiment starts
in a textured urban 3D simulator with real PX4 dynamics and observed motion.
A convincing city image alone is not passage through its geometry.

The [ANWM upstream overview](https://github.com/EmbodiedCity/ANWM.code#overview)
conditions forecasts on displacement in x/y/z and yaw and describes 3D aerial
navigation. Its [limitations](https://github.com/EmbodiedCity/ANWM.code#limitations)
state that real-world results are offline evaluations. The released interface
supports a research direction; it is not evidence of our closed-loop navigation.

| Boundary | Current implementation | Required for this experiment |
| --- | --- | --- |
| Model candidate input | Generic four-component displacement/yaw; PX4 provenance accepts only left/right 5 m | Bound forward, vertical, detour and hold plans to their actual source observation |
| PX4 execution | Two lateral candidates, altitude near 3 m, one maneuver then land | Scene-specific 3D limits, timed waypoint segments and repeated decisions |
| Prediction | One target RGB per candidate and nominal temporal offset | Forecasts aligned to intermediate executed poses and times along a route |
| Candidate score | Full-frame goal RGB MSE | Validated route-progress/passability evidence with uncertainty; preserve independent geometry constraints |
| Outcome | Candidate displacement, landing/disarm; goal-image diagnostic | Destination reached through the urban scene, contacts, clearance, detours and elapsed time |

Do not merely widen the executor's limits: input preparation, provenance,
policy scope, Rules, command binding and controller validation must change
consistently. The existing lateral contract remains unchanged until that work is
implemented and verified. An endpoint image cannot certify the intervening path.

## Three initial scene families

| Scene | Candidate alternatives | Verified outcome of interest |
| --- | --- | --- |
| Building gap | Forward through the gap, lateral route around the block, hold | Airframe passes the gap without contact and continues to the destination |
| Low obstruction | Forward at current height, climb then advance, lateral detour | Altitude changes as commanded and the route clears the obstruction |
| Offset passage or dead end | Left/right multi-segment detours, climb where permitted, observe/hold | Route reaches the destination instead of repeated stopping or backtracking |

Vary gap width, building heights, layout, appearance and start orientation. Some
layouts must favor climbing, some lateral detours and some direct progress.
Include narrow-gap and altitude-limit cases where the appropriate result is a
rejection or a different route. Stage geometry-only fixtures for debugging, then
use building meshes/textures with verified collider alignment for the main test.
Do not present colored-box flights as the completed urban experiment.

Keep wholly unobserved, independently randomized alternatives as separate
information-acquisition controls. WAM cannot be required to know an arbitrary
hidden obstacle from identical inputs. For occlusion cases, record what earlier
views or geometric cues make prediction informative, and compare history-based
mapping as well. If evidence is insufficient, observe or hold.

## Connection and value are separate milestones

1. **CPU/simulator feasibility, no rented GPU:** construct one development case
   per family. Use a known geometric route to verify the scene, airframe swept
   volume, actual PX4 forward/climb/detour motion and terminal observations.
   Retain RGB-D and pose along the route. A successful hand-authored/geometric
   flight validates the environment and executor, not learned navigation.
2. **Matched candidate outcomes:** expand to twelve development starts across
   the three families. Restore the same simulator state for every candidate,
   measure restoration error and obtain actual outcome differences. Freeze route
   limits, candidate costs, success predicates and comparison policies before
   reviewing learned predictions. Treat static renders as visual screens only.
3. **Real WAM forecasts in shadow:** predict these candidate routes from their
   legitimate histories. Record every actual model call, temporal offset, pose,
   projection baseline and scorer output. Compare predictions with observed
   intermediate frames and outcomes. This may validate more than lateral actions
   but still does not establish that WAM drove the aircraft.
4. **Closed-loop WAM selection:** after the expanded contract and score are
   validated, use the selected route through Jev, existing authorization, Rules
   and Executor. Execute only the admitted prefix, capture a fresh history and
   repeat. Attribute arrival to this stage only if the decision receipts bind
   actual model outputs to the flown segments.

The first three development scenes are feasibility work, not a result already
obtained. No additional GPU, flight or urban scene was run for this design update.

## What the WAM must add

The hypothesis is that action-conditioned prediction can choose routes that
make useful future progress and avoid unnecessary stops/backtracking under
partial observation. Independent geometric constraints continue to reject
known-invalid motion. Rejection by those constraints is not WAM avoidance.

The current RGB output supplies neither metric future clearance nor calibrated
collision probability. A passability/depth/occupancy or outcome-value scorer is
missing work. Validate any such scorer against independently measured route
outcomes, with the same scorer applied to projection and model-generated views
where applicable. A goal-progress-only experiment must say so; it cannot report
its RGB cost as collision prediction. Freeze the scorer on development data and
keep future labels outside all predictor inputs.

Compare against the strongest applicable cheap methods: a current RGB-D local
planner, accumulated-history 3D occupancy planning, and geometric A*/trajectory
planning when a map is actually supplied. All methods get the same available
observations, destination, action set, control budget and Rules. Do not remove
available maps, target pose or good depth to manufacture a WAM advantage.

A simulator ground-truth map is an auditor/oracle input unless explicitly part
of the mission. If a Rules filter uses it, apply and report the identical filter
for every method; report filtered and pre-filter choices separately. If a fully
mapped geometric planner already solves the task, retain that as a successful
control and do not claim extra navigation value from WAM there.

Before repeated GPU runs or training, require executable alternatives with a
recoverable outcome gap over the strongest cheap baseline. Connection work may
proceed even without such a gap, but it must remain labeled connection work.
Failure to find a gap should lead to another navigation condition, not silently
to a different mission such as inspection photography.

## Terminal metrics and timing

The primary endpoint is **destination arrival without observed contact or a
flight-envelope violation**, followed by landing/disarm when required by the
scenario. Freeze goal radius, dwell time, deadline and terminal requirements
before collection. Record contact-sensor coverage and independently check the
observed airframe swept volume against scene colliders; absent telemetry does
not mean contact-free. Verify minimum clearance along the path, not camera-center
clearance or just the endpoint.

Report arrival rate, contacts, minimum clearance, travelled distance, backtracks,
interventions, inference wall time and motion simulation time. All timeouts,
abstentions and safety-filter rejections remain in the results. After development,
freeze the policies and compare twenty-four held-out starts, eight per family,
with identical initial states and report paired uncertainty. These counts define
a pilot, not a promise of statistical significance or general city navigation.

The recorded model currently takes about 41 wall seconds for two endpoint
forecasts. More waypoints/candidates need a measured compute budget, not linear
runtime assumptions. Begin with explicit hold-and-plan operation in a static
scene. Count hold/inference cost. Continuous moving-flight prediction requires
a separately demonstrated response time, observation freshness and control
fallback; slowing simulator physics does not establish real-time deployment.

Post-training is an option only after an observable, recoverable navigation
failure is reproduced. Separate normalization/timing mistakes, missing scoring,
missing observations and actual model error before deciding what to train.
Training data and held-out outcome labels remain separate.

## Retained calibration audit

`scripts/audit_px4_anwm_value.py` revalidates the old paired captures. The target
box lay outside the raw camera frustum in all 32 history frames; a scene-specific
green-color rule found zero matching history/forecast pixels. The available
pose baseline already selected the better arm. These facts diagnose that artificial
scene; they do not rule out WAM value in urban passage, climbing or avoidance.

```sh
PYTHONPATH=.:packages/missionos-core/src python scripts/audit_px4_anwm_value.py \
  --left-root "$LEFT_CAPTURE_ROOT" --right-root "$RIGHT_CAPTURE_ROOT" \
  --output "$LOCAL_VALUE_AUDIT_JSON"
```

The audit dispatches no flight and invokes no new model or Jev call. Human/policy
authorization, Rules, Executor and Verifier keep their separate roles throughout
the proposed navigation work. Raw captures and authorization records stay local.
