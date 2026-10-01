# Yokohama pre-departure map goal

The authenticated Gateway serves `/missionos/yokohama/map`. The identical
router can run alone on loopback for isolated CPU tests. TaskStore records the
selected goal, four-point urban route, return, scene hashes, runtime source
hashes and exact operator approval. Moving the marker supersedes the old task
before validating a replacement. Running tasks reject goal changes. Cancel is
an interruption request until the owned runner has finished cleanup.

## Local use

```sh
python -m pip install -e '.[dev,urban-map]'
# No model API, GPU, cloud provisioning or native VLA/WAM.
export MISSIONOS_YOKOHAMA_SITL_PYTHON="$(command -v python)"
export RUN_MISSIONOS_YOKOHAMA_DELIVERY_SIM=1
export MISSIONOS_YOKOHAMA_MAP_BACKEND=sitl
export MISSIONOS_YOKOHAMA_MAP_PORT=8897
export MISSIONOS_YOKOHAMA_MAP_DB=output/yokohama-map/tasks.db
export MISSIONOS_YOKOHAMA_MAP_OUTPUT_ROOT=output/yokohama-map/runs
python scripts/yokohama_map_server.py
```

Open `http://127.0.0.1:8897/missionos/yokohama/map`. On the full Gateway,
configure the browser origin as an allowed CORS origin and enter the Gateway
API key when prompted. The key stays in page memory. The static scene has no
private task content. The portable server rejects remote and cross-origin use.

Select source `[217.894, -115.179]` (10m east of the original pad), confirm the
route, explicitly approve and start. The default backend is the existing CPU
PX4/Gazebo image `px4io/px4-sitl-gazebo:latest`, with city models in fixture mode,
no learned fixed-camera pad advisory and no LLM judge. It retains the scripted
lead aircraft and pose Rules pad wait. The simulator receives an allowlisted
environment without model keys. It never stops another Yokohama simulator.

For fast kinematic testing, set `MISSIONOS_YOKOHAMA_MAP_BACKEND=fixture` and
`RUN_MISSIONOS_YOKOHAMA_MAP_FIXTURE=1`. The UI and evidence label this separately;
its observed delivery/return is not Gazebo or physical-flight evidence.

## Coordinate and safety contract

- Clicks supply source local X/Y metres in EPSG:6677; the server obtains source
  EPSG:6697 terrain height. No caller-supplied altitude is trusted.
- Display mapping is `[source X, source height, -source Y]`.
- Gazebo conversion remains the frozen original D1 ground +0.12m origin and
  local EPSG:6677-to-true-ENU Jacobian. A goal never changes the origin.
- PX4 local NED remains the existing observed EKF-origin conversion.
- Candidate region is within 30m of the original pad and inside the 600m crop.
  Building/bridge footprints block every altitude. The 3m landing region needs
  1m vehicle radius +3m residual clearance; the cruise route needs 4m horizontal
  clearance and sampled ground clearance >=5m at source height 18m.
- Ground variation around a candidate is <=0.3m. These conservative source
  checks are fixture qualification, not a surveyed landing-site certificate.
- Keep D1, D2 and D3 and their model boundaries; replace only DELIVERY, the
  generated Gazebo pad, payload camera and receiver coordinates. Return reverses
  the same selected urban route and uses the original stationary ship.
- The approved manifest is recomputed against current source geometry and
  source code before Docker creation. `--goal-plan` forbids native models,
  learned pad advisory, wind and recovery experiments in this initial mode.
- Completion requires decisions, pad queue, payload and SITL verifiers to pass,
  including map approval/route/receiver bindings. A release command is no receipt.

Existing chat and CLI invocations without `--goal-plan` retain the fixed route
and trained pad advisory path. No Rules/judge timing code is changed.

Recovery contract (2026-09-30)

The recovered source is newly validated; earlier 3775/3780 results do not apply.
The previous live budget is closed because its consumed-slot DB could not be recovered.
Map plans bind the Jev adapter configuration to fixture/continue. Any live mode is
rejected before planning. No credential loader is included. The unchanged DeepSeek
planner remains available on the existing chat surface; judge selection is separate.
The map has one Gateway instance only; process-local execution locking does not claim
distributed multi-Gateway exclusion. An old task action returns 409, and the browser
refreshes the canonical current task/goal.

The production host hook uses a fixture-specific irreversible request ledger, not a
new live budget. It reserves before adapter execution; crash/restart/task replay cannot
refund or duplicate a request. Request/config/run/plan/observation/situation digests,
fresh host mailbox/heartbeat and worker elapsed times must agree. mtime is a separate
conservative fence: old/future files are rejected; it is never used to convert clocks.
A monotonic host deadline bounds adapter completion. Failure keeps the existing
independent Rules fallback and its repeated entry check. Rules waiting does not count
toward the existing 30-second judge-only budget.

Set MISSIONOS_YOKOHAMA_ALTITUDE_DIAGNOSTICS=1 for measurement-only instrumentation.
An independent collector records actual vehicle_local_position_setpoint.z and
trajectory_setpoint.position[2], local/global position, home reference, and optional
vehicle_local_position_groundtruth. The configured stage target is a separate field.
Normal flight sample/control has no additional listener or cache call. Every listener
has a bounded subprocess and cooperative terminate/kill/reap; collector cleanup is
recorded, and trajectory/events close even on collector cleanup failure.

Capture begin/end host monotonic and Gazebo sim timestamps, per-topic intervals and
PX4 boot-microsecond timestamps are recorded. Samples are cohorts, not atomic. Dynamic
local/controller/trajectory/global topic spread, host/sim capture widths and Gazebo
pose age must each satisfy the diagnostic freshness conditions. Static home_position
is recorded as a reference and excluded from dynamic timestamp spread. Optional
groundtruth has a 0.1-second probe bound; missing groundtruth remains null. Missing or
nonfinite control target never falls back to configured height and never reports fresh.
ENU world Z and PX4 local NED down are distinct; target relative to home is computed
only from observed ref_alt, home.alt validity and actual controller z. Reset/ref
timestamps and all raw telemetry are retained. Safety thresholds, 3-second settle and
30-second hold including 0.6m vertical tolerance remain unchanged.

Recovery review refinements:
- One Gateway shares chat/map dispatch ownership through worker cleanup. This is
  an in-process reservation, not cross-process simulator scheduling.
- The judge mailbox binds a persisted pre-judge response by digest; advisory
  evidence is rechecked with the existing contract before computing situation.
- Altitude comparison requires PX4 listener source age on every required dynamic
  topic, conservatively including collection width, plus Gazebo sensor_sim_s
  freshness in the same simulation clock. Missing age remains unavailable.
- Original scene roads may omit display_z_m; the renderer then uses source Z.
- Four existing Japanese route tests explicitly inject the geocoder fixture;
  no public geocoding is needed for the offline aggregate.

## Unified altitude transport (simulation only)

New runner configs require `missionos.yokohama-altitude-transport.v1`.
Every AP stage (including payload low/climb and return sea legs), city proposal,
and connector keeps its approved world Z and maps only the wire representation:
`relative_command = PX4_observed_relative + target_world_Z - observed_world_Z`.
Static home AMSL is not a Gazebo world height. This compensates an observed
simulation reference offset; it does not diagnose or eliminate estimator bias
and is not qualified for hardware.

The ordinary worker sample captures raw PX4 local/global/home topics even
without the optional diagnostic collector. Required validity, finite values,
local/global/home consistency, source listener ages and capture duration must
pass. Dynamic PX4 sources use boot time; Gazebo stats/pose use simulation time;
sample/dispatch deadlines use worker elapsed monotonic time. Two-second bounds
apply independently to those domains. Asynchronous Gazebo stats/pose spread is
recorded and bounded; these samples are never described as simultaneous.
Static home age is exempt from dynamic age, but its reference identity is bound.

AP and connector upload scripts cannot send authored world Z directly: they
require runtime-mapped sidecars. Missing/stale inputs fail before upload.
The uploader receipt must match mapped items; each auto-mission command rechecks
a fresh sample, unchanged reference/reset counters and at most 0.05m offset
change. The first grounded pre-arm upload may be remapped once after arming
finalizes home, while still landed and before auto-mission; later reference
changes fail closed. This adds no settling/hold delay and changes no safety
tolerance. Uploads and rebindings remain explicit evidence.

City permits bind candidate mapping and immutable connector world templates.
Connectors remap from their actual dispatch observation after the city segment,
rather than reuse an older estimator offset. Verification independently checks
mapping, world templates, receipts and dispatch freshness/order. Historical
configs without this schema retain their original verifier claims and evidence;
they are not retroactively declared to have used unified transport.

The parcel receiver, collision checks, 3s settling, 30s measured holds, 0.6m
vertical tolerance and return landing checks all remain in the original world
frame. A planned mapped mission value is distinct from a measured controller
setpoint. Offline replays are not evidence of an improved real simulation flight.


## Prepare protocol before freezing height

New configs also require `missionos.mavlink-upload-preparation.v1`. A unique
transaction binds run, world, segment and the shared Linux VM monotonic worker
epoch. The preparation subprocess starts/reuses MAVLink and finishes the clear
phase without sending mission count/items. Its receive timeout is bounded by
the remaining existing three-second clear deadline. Missing clear ACK is
explicitly `unconfirmed`, retaining the earlier upload continuation policy;
NACK is rejected. It is never reported as an acknowledged clear.

The worker continues ordinary bounded observations/Rules during preparation,
then captures altitude after readiness. City proposals prepare before their
fresh authorization/heading/height/Rules observation. AP and connectors compile
their immutable world templates after readiness. Each send uses a nonce-bound
mapping sidecar; stale nonce, capture predating preparation, mapping digest and
send-time age violations fail before mission count. There is no clear/session
restart in that send phase and no remap/reupload loop.

Receipts bind preparation, transaction, mapping, request sequence and the
decoded float32/int32 MAVLink wire values. Mission ACK=0 plus changed PX4
mission identity/count remains mandatory. A separate current observation,
unchanged origin/reset/50mm offset bound, city expiry and pad Rules must still
pass immediately before auto mode. ACK is neither dispatch permission nor
delivery evidence. Historical snapshots without the preparation schema retain
their original verification; they are not relabelled as the new sequence.

No real Jev call or flight is part of this code validation. The lost prior
live ledger remains closed and fixture/continue remains the configured judge.
A future live test needs a separately approved new budget/DB identity and
reviewed memory-only credential loader: one CPU delivery, at most two total
HTTP sends including failure, USD0.01 total, 32KiB per input, no retry or
redirect. Slots must be atomically reserved before sending and never refunded,
including crash/restart or task recreation. Provider invocation/usage/latency
and missing credentials/fallback must be distinct from fixture evidence.
Current recovered code deliberately rejects live mode; no new budget or
credential access has been created here.

## Public demonstration and preserved live evidence

The default public demo uses CPU fixtures and never calls an external provider.
The recorded successful delivery used one real Jev decision in a separate,
reviewed and bounded private execution. Its grant is consumed. Private ledgers,
credentials and operator approval identifiers are excluded from this branch.

Public runtime sets `LIVE_ENABLED = False`. Selecting live mode fails before
credential loading, ledger use or HTTP. Environment changes cannot enable it.
Mock-only tests inject provider responses and explicitly patch the source flag;
no production grant registration or initializer is supplied. Missing or consumed
ledgers continue to fail closed. A future live execution requires its own
operator authorization, independently reviewed grant and credential handling.

The preserved common-ledger contract binds one delivery to its full approved
plan before runner creation, and reserves at most two unique request/observation
slots atomically before HTTP. Slots are never refunded on failure, interruption,
restart or task recreation. Thirty-second additional-wait accounting, independent
Rules rechecks, and the authority split are unchanged. No live budget is revived
by publication. The public branch does not copy or alter the original ledger.

See `docs/examples/yokohama-map-delivery/REPORT.md` for the simulator result and
camera-export limits. This publication changes live availability only; its
fixture runtime and altitude conversion remain those validated by the tests.
