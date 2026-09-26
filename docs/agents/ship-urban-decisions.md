# Ship urban decision experiment

This is Step 2's scenario and model-free comparator slice. It is opt-in local
PX4/Gazebo execution, not VLA/WAM integration. Keep the [Step 1 authority and
evidence boundaries](ship-delivery.md).

## Observation and policy contract

`src/runtime/ship_urban_decision.py` is a standard-library module copied into the
container as `urban_policy.py`. `choose_urban_action` accepts only a policy name,
airspeed, and at least three timestamped obstacle-x observations spanning at
least one second with no gap above 1.5 seconds. Unknown fields, future clearance
times, non-finite values, reversed clocks, and the oracle policy are rejected.
Case identity and the scripted trajectory speed are not policy inputs.

The actor uses the case's hidden script parameters; the selector uses actual
Gazebo pose history. This is privileged state access. It is not sensor-matched
VLA/WAM evaluation, and reports must retain
`learned_model_comparison_admitted=false`.

`short_clear` moves the obstacle east at 2 m/s; `long_block` moves at 0.15 m/s.
The actor starts only after observed urban-entry hold, so prior flight duration
does not change the obstacle's phase. Motion is wall-clock scripted through
Gazebo's model-pose service and capped at x=16 m, beyond the 13 m clearance gate. Updates end at that point;
three consecutive service failures block the run. Retry events are logged.
Only the obstacle is moved by this service. The drone and parcel are never
repositioned by the experiment.

The velocity rule estimates time until obstacle x reaches 13 m, then compares
it with 170 m / airspeed plus an 8 s turn allowance. Fixed alternatives use
the same observation period, geometry, executor and clearance gate. The analytic
screen's timing is a design estimate, not measured flight duration.

## World and route contract

Urban mode requires offshore distance >=80 m, inland distance >=200 m, and cruise
altitude 25–35 m. It raises the six corridor buildings to 45 m and adds a
collision-enabled 16 x 8 x 60 m obstacle at coast+70 m north. This is a synthetic
obstruction, not a physically validated crane or another vehicle model.

The outbound AUTO mission ends in indefinite loiter 30 m before the coastline.
After observing the obstacle, the worker chooses one of two prebuilt missions:

- Wait: remain in hold until observed obstacle x>=13 m, then follow the direct
  corridor through coast+130 m to the delivery point.
- Detour: fly east to x=85 m, north to coast+130 m, then rejoin the corridor.

Each mission has its own item objects and zero-based sequence. PX4 upload ACK
and Navigator validation precede AUTO activation. The executor observes the
actual AUTO nav state and makes at most five mode requests while Commander
processes asynchronous validation updates. CLI exit status is not a mode ACK.
Both alternatives finish in
the existing 3 m delivery hold; the independent delivery gate controls return.
Parcel-detach publication may be retried at five-second intervals, at most
three requests total, only while fresh positions still show the parcel attached.
A publication or retry is not delivery evidence; the host must independently
verify separation, pad contact and stability before granting return.
All cases use a common return detour so a still-present obstacle cannot be
ignored during recovery. The comparison interval ends at delivery hover and
excludes this common return.

## Outcome verification

The original verifier still checks the same vehicle, parcel and ship, actual
parcel delivery, reserve and stable deck recovery. Urban verification additionally
requires ordered decision/dispatch events, policy inputs matching recorded
observations, a reproducible policy output, observed obstacle motion, all six
building positions, and observed lateral flight for a detour. Missing, stale,
or non-finite position observations cannot establish clearance. Direct departure
requires observed clearance.

Gazebo contact sensors are event-only. Startup records their real publishers;
the subscriber processes must remain alive. Contact messages are latched so a
brief contact cannot disappear between telemetry samples. No-message intervals
are not presented as fresh empty-contact measurements.

Independently, swept line segments between successive vehicle/obstacle position
samples must avoid the collision boxes inflated by 0.75 m. This is sampled
geometric separation, with linear interpolation between observations, not a
formal continuous-physics safety proof. Keep contact observations and geometric
verification as separate report fields.

## Runtime comparison

Run four fresh, sequential experiments with otherwise identical configuration:

| Case | First run | Matched alternative |
| --- | --- | --- |
| short_clear | constant_velocity | always_detour |
| long_block | constant_velocity | always_wait |

Use new output directories and explicitly pass `--approve-sitl`. The compact
scenario in `examples/fixture_missions/ship_delivery/urban-compact.json` uses
100 m offshore / 200 m inland. Report that reduction; do not call it a new
validation of the complete 1 km route.

`ship-delivery urban-compare --run-dir ...` reverifies all four telemetry/event
streams and canonical invocation evidence before aggregation. It requires unique
run identities, complete action-pair coverage, matched scenario/world/image/
worker/helper code, and successful delivery/recovery. Modified world files,
unsuccessful runs, and changed invocation logs block comparison.

Elapsed seconds use the worker's monotonic wall clock, not Gazebo simulation
time. Simulator load and control overhead are included; these are not airframe
performance measurements.

Report per-case urban elapsed seconds, selected action, best observed action,
lateral movement, contacts, geometric clearance, and the remaining gap over the
velocity rule. The best observed candidate is an offline evaluation reference;
it is never an execution policy. Two cases with one run per action do not support
statistical, held-out, visual-perception or learned-model superiority claims.

New public demo/tests use the analytic screen or fixtures. Docker and models
remain opt-in. Generated raw evidence remains outside the repository.
