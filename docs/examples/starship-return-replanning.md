# Return prediction for mission replanning


Execution and prediction now use the same Ship return controller. A fresh
39-flight regression reproduced **97,703 samples and all recorded commands,
events and outcomes exactly**. Four fixed prediction candidates, including
one following-orbit opportunity, meet the modeled contact limits.

The following-orbit results make a real planning constraint visible: waiting
changes where the vehicle reaches the surface. MissionOS still needs to check
approved recovery areas, availability, waiting resources and uncertain observations
before it can choose and execute such a return. The AI replanning loop is not yet
connected.

| Request delay | Contact speed | Tilt | Propellant | Contact latitude / longitude | CPU process wall time |
|---|---:|---:|---:|---|---:|
| Original time | 4.60826 m/s | 2.37486° | 62.39431 t | 4.88280° / 165.27819° | 45.44 s |
| +60 s | 4.61028 m/s | 2.38119° | 62.39420 t | 6.01916° / 167.51984° | 45.59 s |
| +5,451.31 s (one period) | 4.63322 m/s | 2.42069° | 62.42976 t | 5.35468° / 143.03096° | 84.15 s |
| +5,511.31 s | 4.63249 m/s | 2.41880° | 62.49358 t | 7.28258° / 146.88590° | 85.31 s |

Each forecast used one CPU worker; part of the batch overlapped the contract
tests. These are measured process times, not a live-response guarantee. The
candidate times were frozen from the initial orbital period before any forecast;
there was no search, retry, model call or change to contact limits. No recovery
region was placed around the resulting coordinates to create a success claim.

The [opportunity screen](../assets/starship-return-replanning-m1/opportunity-screen.json)
and [shared-controller regression](../assets/starship-return-replanning-m1/shared-controller-regression.json)
record source identities, checks and limits. The registered return certificate
was rebuilt for the new code but retains its original narrow coast corridors;
it does not authorize these following-orbit candidates. The terminal-engine-loss
case still records a 22.73 m/s impact, and fuel shortage remains unresolved.

## Earlier timing screen

Delaying the modeled Ship's return by 60 seconds still reaches low-speed
contact, but moves the contact point by **about 278 km**. A return planner must
check the recovery footprint as well as speed, attitude and fuel.

This is the first feasibility screen for a mission-control loop that responds
to an unavailable recovery area, waits, reassesses and executes a revised return.
It is not that completed loop: both flights use a fixed preflight timing change,
with no AI inference or in-flight replanning.

| Measurement | Original return time | Return request delayed 60 s |
|---|---:|---:|
| Payloads released | 26 | 26 |
| Contact speed | 4.60826 m/s | 4.61028 m/s |
| Tilt at contact | 2.37486° | 2.38119° |
| Body angular rate | 0.00303143 rad/s | 0.00302323 rad/s |
| Remaining propellant | 62.39431 t | 62.39420 t |
| Contact latitude / longitude | 4.88280° / 165.27819° | 6.01916° / 167.51984° |
| Contact time | T+4929.85643 s | T+4970.59148 s |

Both meet the original four modeled contact limits: 5 m/s, 5 degrees,
0.02 rad/s and 28 t reserve. All 1,194 saved samples before the original return
request, all 26 release events, and the booster separation state match exactly.
Only the configured orbit-coast duration differs. The contact shift is a
spherical great-circle estimate (mean radius 6,371,008.8 m); contact occurs
40.73505 s later, not 60 s later.

These results do not establish an available recovery area, actual water entry,
recovery/reuse, waiting endurance or SpaceX fidelity. No terrain/sea lookup or
recovery-resource availability was used to approve these contact locations.
Power, thermal endurance and boil-off remain unmodeled. This original timing
screen changed neither qualification tolerances nor production guidance. Its
source hashes remain historical; the shared-controller work below requires new
qualification for the changed source.

The first probe invocation passed in-memory tuples to JSON-only verifiers, so
both processes exited 2. The original failed verification files are preserved.
Reading the already-saved JSON through the independent record and return-policy
checkers passes for both flights; no dynamics were rerun. The probe now reads
its serialized artifact before verifying. The published audit records the old
verdicts and the exact executed probe hash rather than relabelling those flights
as executions of the corrected probe.

The [saved audit](../assets/starship-return-replanning-m1/timing-screen.json)
contains checks, hashes and explicit claim limits. The
[milestone contract](../agents/starship-return-replanning-m1.md) defines the
remaining work: prediction from current observed state, recovery-area and
waiting-resource admission, repeated AI decisions and three end-to-end cases.
The milestone remains incomplete.

Recheck existing flight records without launching another simulation:

```sh
python scripts/verify_starship_return_delay.py \
  --nominal output/return-delay/nominal \
  --delayed output/return-delay/delay60 \
  --output output/return-delay/audit.json
```

The audit refuses to overwrite a previous verdict. The full flight records are
retained locally; only the reviewed compact audit is published.

## Next step: predict both returns from the same coast state

An offline candidate tool now starts from the saved Ship state at T+2190.7 s,
after all 26 payloads were released. It predicts a return request at T+2280.7 s
or T+2340.7 s, without repeating launch. Both predictions reproduce the contact
locations, times and four contact-limit measurements in the table above exactly.

| Reproduction check | Original time | +60 seconds |
|---|---:|---:|
| Physical samples matched exactly | 1,377 / 1,377 | 1,397 / 1,397 |
| Final state and finite actuators | Exact match | Exact match |
| Return/ignition/terminal event times | Exact match | Exact match |
| Process wall time, two concurrent CPU workers | 48.36 s | 48.83 s |

This makes the next operational question measurable: which candidate contact
location and time fits an available, approved recovery area? The present tool
does not yet select that area or authorize a return. Its input is a saved complete
plant state, not a noisy sensor estimate. Identical results from the same model
establish reproduction, not physical accuracy. A future live connection must
also account for the flight advancing during these roughly 48-second calculations.

Two input checks initially failed before integration because a three-vector
norm was used for a four-component quaternion. After that fix, two predictions
matched the final state but failed the unchanged 95% sample-coverage check:
the forecast omitted the original attitude-driven logging condition. Fixing
that logging condition and running the same pair once more produced the results
above. All failed records remain preserved. Four return predictions were
integrated in total; no new full flight or hosted inference was run.

The [candidate audit](../assets/starship-return-replanning-m1/candidate-screen.json)
binds the results to their executed source files. The
[milestone contract](../agents/starship-return-replanning-m1.md#second-gate-saved-state-return-predictions)
defines the tool's restricted scope and commands. M1 still needs observed-state
uncertainty, recovery-area/time constraints, bounded waiting, repeated AI
decisions and the three end-to-end cases.

Execution and prediction now call one return controller, replacing the copied
phase logic used in the original screen. The next technical screen evaluates
four fixed times, including a following-orbit opportunity, within a development
ceiling of 6,000 seconds beyond the original return plan. Recovery-area access
and waiting resources must be established before such a delay can be selected.
See the [integration decisions](../agents/starship-return-replanning-m1.md#decisions-before-operational-integration-review-of-pr-127).
