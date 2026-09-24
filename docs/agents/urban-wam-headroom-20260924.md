# CPU-only urban WAM headroom gate — 2026-09-24

Status: **stopped as incomplete** after three completed flights and one preflight
process abort; eight planned cases remain unattempted. No model calls or additional
rented GPU. The protocol below was frozen before the new development flights.
This implements the development gate proposed in
[PR #113](https://github.com/pome223/missionos/pull/113#issuecomment-5810742386).
The prior three urban ANWM flights remain a separate connection experiment.

## Observed result

The first three passage variants independently reached their goal, landed and
disarmed with the frozen history-depth baseline. Their observed distances were
14.379, 14.207 and 14.227 m; minimum swept-sphere clearances were 0.798, 0.822 and
0.836 m. Current-depth and history-depth rankings matched in these three cases;
they are one flown policy and two shadow comparisons, not nine flights.

In case four, the contact probe wrote eleven positive building-contact observations
and a confirmed probe-removal record, then its process exited 134. The retained
stderr contains `terminate called without an active exception`. This is consistent
with a native process termination during exit, but no native backtrace was captured
and the exact cause is unresolved. The flight controller had not started. The
runner treated the nonzero exit as a failure and removed its isolated container;
the fixed cohort was not silently retried or resumed. Eight further cases were
not attempted. A separate pre-cohort contact-only calibration succeeded and is
not counted among the twelve development cases.

The result is **measurement incomplete**, not a learned-model failure. Three
verified baseline arrivals leave an upper bound of nine additional oracle arrivals
out of twelve. Therefore the proposed numerical no-headroom conclusion cannot be
drawn; GPU entry is withheld because the measurement stage did not complete.
No full same-start candidate matrix, held-out trial or new WAM inference was run.

[Reviewed measurements, trajectories and checks](../assets/urban-headroom-px4-20260924/README.md)
preserve this failure and the unattempted denominator. A consistency check passing
in CI does not make the development gate pass. The next engineering action is an
isolated, non-flight investigation of the contact probe's native shutdown before
considering a newly registered cohort; this cohort stays closed as incomplete.
The subsequent [shutdown investigation and repair](urban-contact-shutdown-20260924.md)
is recorded separately and does not revise this frozen result.

## Fixed cohort and information boundary

Twelve development scenes comprise four variants of each existing passage,
climb and detour family. Their primary building(s) shift east by -0.45, -0.15,
+0.15 or +0.45 metres. Background geometry, route templates and nominal start
(ENU 0, 0, 3 m) are fixed. These are deliberately simple static map-omission
cases, not a representative city benchmark or twelve held-out/randomized starts.
No obstacles are introduced after observation. All scenes use the pinned textured
Apartment mesh and matching collider from the original urban trial.

The partial map omits both passage-side buildings or the central climb/detour
obstruction. The selector receives a whitelist containing only this partial map,
fixed candidate waypoints, goal, flight limits, measured pose and actual depth
surfaces. It receives no case identity, omitted obstacle names, truth geometry,
route-admissibility mask, future images or outcome labels. The raw pose transport
also contains scene entities; these are stripped before the selector input is
written. Sensor extrinsics/calibration come from audited source SDF and measured
CameraInfo/vehicle/link poses. Original depth and pose hashes remain local.

Three fixed CPU policies are compared at each fresh observation:

- `stale_map`: shortest route not blocked by the incomplete map; diagnostic only.
- `latest_depth`: map plus occupied surfaces from the latest depth frame.
- `history_depth`: map plus occupied surfaces from all sixteen synchronized frames;
  this fixed policy is the executed baseline used for the conservative bound.

No learned semantic feature or external model is used. Valid native depth samples
are taken at four-pixel stride, transformed to world coordinates, accumulated in
0.15 m occupied voxels and inflated by the airframe radius (0.6 m), required margin
(0.25 m) and 0.13 m voxel half-diagonal allowance. Reject routes intersecting these
occupied surfaces or available map boxes; among remaining hypotheses choose the
shortest, with route-ID tie-break. Unknown/unseen space is not certified free.
There is no substitute of simulator truth for missing depth, nor an oracle fallback
when no route is proposed. This is a fixed-route depth/occupancy baseline, not a
complete general-purpose local planner. Both depth policies have the same data
available; the latest-frame ablation elects to ignore older frames.

Choice is frozen before the independent geometry filter sees it. A signed,
short-lived command binds that choice, source age, scene, retained instruction and
session. A rejected choice causes landing without route substitution. The true map
is used only for the common execution safety filter and independent verification.
Pre-filter rankings and filter intervention are reported separately. A sole surviving
route is not evidence of WAM judgment. Jev remains unused in this experiment.

## Measurements and fixed termination rules

All twelve selected baseline routes are attempted in fixed cohort order, unless an
infrastructure/measurement failure stops the cohort. No case is silently replaced
or retried. Use the same 0.2 real-time factor and 4 ms physics step as the prior
urban camera validation. Model/GPU calls permitted by this protocol: **zero**.

Before each flight, a temporary passive sphere is dropped onto a building in the
isolated simulator. A positive notification from that building's contact sensor
and subsequent probe removal must be observed before the aircraft takes off.
The flight separately records the ground-contact control, building-contact messages
and sampled swept airframe-envelope distance to conservative mesh bounds. This
sensor control plus trajectory evidence is bounded simulator evidence; silence alone
is not a universal contact-free guarantee. Neither probe nor ground truth enters
the route selector. The probe never sends aircraft commands.

The actual start must be within 0.15 m of the nominal hover, within 0.03 rad of
ENU east heading, and at speed <=0.1 m/s. Each actual start, velocity, heading and
sensor timestamp is retained. Fresh independent simulator resets are used; exact
cloning of internal PX4/physics/renderer state is **not** claimed. No paired-candidate
result is inferred merely from nearby start poses.

Freeze goal radius 0.3 m, goal dwell one simulation second, then required landing
and disarm. Route deadline is 90 simulation seconds and 450 wall seconds. Original
observation age at dispatch must be <=60 wall seconds with translation <=0.25 m
and yaw drift <=0.1 rad, fresh telemetry and OFFBOARD hover. Record model-free
preparation/observation/selection and flight wall/simulation times separately.
All source/protocol code hashes are saved before the first development case.

The proposed GPU gate requires at least **three additional safe arrivals among
these twelve cases** over the strongest applicable simple baseline. Before any
candidate matrix or rented inference is expanded, use the valid bound:

```text
oracle arrivals <= 12
strongest simple baseline arrivals >= verified executed history_depth arrivals
possible additional arrivals <= 12 - verified executed history_depth arrivals
```

Thus ten or more verified baseline arrivals make the required gain impossible in
this fixed cohort. This prospective futility check avoids flying every other route
when they cannot change the gate. It is an upper bound, **not** a completed same-start
candidate-outcome matrix, and is not an ANWM failure rate. Continue collecting the
fixed baseline cohort for reporting; do not redesign it after seeing scores.

If the bound leaves >=3 possible gains, that alone does not authorize GPU: complete
the same-start candidate outcomes and restoration checks, observability and validated
scorer requirements in the posted proposal first. This runner does not implement
that later stage or the 24-case held-out comparison. If measurement/infrastructure
fails, stop as incomplete and distinguish this from a model-quality rejection.

## Reproduction and runtime boundary

`$RUN` must be a new caller-local directory, `$ASSETS` the already verified pinned
asset directory, and `$RETAINED_INSTRUCTION_REF` the existing user instruction
reference, kept out of public evidence. No cloud configuration or credentials are used.

```sh
python scripts/px4_urban_headroom_trial.py --phase freeze --output-dir "$RUN"
RUN_PX4_URBAN_WAM_TRIAL=1 python scripts/px4_urban_headroom_trial.py \
  --phase run --output-dir "$RUN" --assets-dir "$ASSETS" \
  --approved-instruction-ref "$RETAINED_INSTRUCTION_REF"
```

The runtime boundary is actual Gazebo depth -> CPU occupancy ranking -> signed
choice -> independent Rules -> PX4 waypoint motion -> observed arrival/landing.
Run only in a fresh network-isolated simulator container; remove that owned
container after each case. No physical hardware, hosted judgment or learned-model
flight is included. Private raw captures, instruction references, HMAC keys,
local paths and session identifiers are excluded from published evidence.
