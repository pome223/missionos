# Mission-wide Starship decision envelope

This opt-in development slice connects mission decisions across deployment,
return and booster recovery. Numerical tools belong to MissionOS. They are
neither replaced by language models nor treated as the project's adversary.
Normal monitoring starts before a deployment interlock. Existing scenarios and
their narrow approvals retain their original behavior.

## Authority and contract

The new `sixdof_managed_*` scenarios require a new displayed plan, `/approve`,
then `/run`. Approval binds `mission_envelope`, source/profile hashes, one of
five synthetic conditions and a same-start fixed-timeline comparison. Within
this envelope **MissionOS decides**; routine operations require no further
human command. Internally the model returns a decision candidate, the worker
independently checks it, the sequencer/controller applies it, and the verifier
checks subsequent observations. A provider answer is never approval or execution.

| Decision point | Approved choices |
|---|---|
| Deployment start, without an alarm | Continue, bounded hold, stop remaining releases, collect mechanism status |
| Deployment monitoring after the first attempt | Same four choices |
| After a mechanism diagnostic | Continue or stop |
| Return selection before the fixed deorbit time | Fixed terminal guidance or the existing retained-payload guidance |
| Early booster return | Decide capture or divert, subject to independent arrival qualification |

Limits: five assessments per flight, at most five Jev and five conditional
DeepSeek calls in live mode, one mechanism-status request, total hold budget
30 simulated seconds, 75 simulated seconds per response. A pending model never
freezes the plant; integration is paced while awaiting the host broker.
The scheduled return cannot be delayed. Release feasibility, fuel floor and
qualified capture authority remain independent dispatch conditions. A notice
cannot change these limits. The configured 28 t release fuel floor is a
development-profile constraint, not a certified state-dependent return reserve.
Initial deployment cannot start while its decision is pending; orbital dynamics
continue. Later monitoring runs alongside the already authorized sequence.
The stored verifier checks observation release inventories against independently
validated separation events and rejects release before the start decision.

Out-of-scope, expired and invalid decisions record a reason and apply the
preapproved fallback: disable further deployment, retained-payload return mode,
or divert. Hold expiry disables remaining releases. An escalation request can
be recorded, but this first slice does not yet accept a new human grant during
flight. It uses the preapproved no-response behavior. This limitation must not
be described as a working asynchronous human handoff.

## Observations and tools

The strict provider boundary contains simulation time, phase, release tracker,
ACK, sequencer, quantized fuel gauge, return deadline, tower status, four tool
fields and a bounded operations notice. It excludes scenario name, future fault
schedule, true inertia/mass, physical force histories and terminal outcomes.
The release tracker and mechanism status are explicit synthetic sensors. Fuel
has reproducible bounded synthetic noise and 100 kg quantization; the release
check subtracts the 100 kg error bound. This is not a calibrated sensor model.
The development GNC still uses ideal plant state for its local controller.

`collect_status` requests a separate actuator/latch-status channel and holds
deployment for at least two simulated seconds. The simulator supplies a later
mechanism report, and MissionOS reassesses. It is not a hardware camera or a
measured SpaceX mechanism. Tiny attitude/inertia identification is not a
prerequisite for this mission-management implementation.

Jev directly chooses among bounded decisions or requests DeepSeek reasoning.
Neither API is called in fixture mode. Fixtures exercise governance/execution
and form a transparent scripted-policy comparator; they are not AI evidence.
Provider keys remain in the Gateway. The simulation subprocess receives a
credential-free environment. Requests and replies bind run/request, observation
and envelope hashes. Fresh dispatch-time observations are checked again.

## Comparison and completion gate

Five synthetic conditions are frozen: normal; accepted release without physical
separation; propellant loss; unavailable tower; a payload-operations suspension
notice. Each runs a fixed timeline and MissionOS from the same initial state,
profile and fault sequence. Both share physical release constraints and the
same unqualified-capture restriction. The fixed timeline does not interpret
the notice or switch terminal return guidance. This is not a claim that a
well-designed conventional flight manager could not do both.

Record validity (`verification.passed`) is separate from
`comparison.comparison_accepted`. The completion gate is:

- Normal: exactly equal recorded terminal metrics, no hold/diagnostic/stop.
- Abnormal: orbit result and scheduled return time preserved; measured contact
  speed no higher than baseline by more than 0.01 m/s; deployment count no
  lower, except for the explicit suspension notice, where at most one release
  is allowed. The booster destination remains the approved divert site.
- All five conditions must pass; a model call or a dispatch alone is insufficient.
- Report fuel remainder, contact speed, deployment count, actual diversion
  arrival and every rejection separately. A low contact speed is not landing
  success. Failed acceptance remains failed, without tuning thresholds afterward.

This initial gate covers those selected outcomes, not general noninferiority
or safety: fuel/contact tradeoffs, noise, unseen notices, latency and controller
qualification remain unevaluated. Human comparison/workload is **unmeasured**;
zero in-flight commands in an automated test is not a measured labor saving.
Use a real per-step human comparator before claiming that saving. Numerical
policies can also investigate; this implementation does not claim otherwise.

## Physical and timing limits

The ship and launch-derived booster are integrated sequentially with independent
branch clocks, as in the existing harness. Their states are never reset. A model
does not receive other-branch future results. This is not a real-time concurrent
flight scheduler. The booster uses the declared synthetic 30 km east target and
finite control; changing the target does not establish reaching it. Current
launch-derived capture qualification is absent, so independent Rules prevent
capture and MissionOS selects divert. The new slice does not connect the
predictive catch solver or claim a launch-derived catch.

## Runtime checks

```sh
python scripts/start_starship_gateway.py --fixture-planner \
  --state-dir output/managed-fixture-state --port 18920
python scripts/smoke_starship_chat_gateway.py --port 18920 \
  --scenario sixdof_managed_normal --output-dir output/managed-normal
```

Repeat with `sixdof_managed_release_fault`, `sixdof_managed_fuel_shortage`,
`sixdof_managed_tower_unavailable`, and `sixdof_managed_operations_notice`.
Fresh output directories preserve unsuccessful attempts. The loopback smoke
uses a test operator, not authenticated human identity.

For live inference, keep `--fixture-planner` and add
`--enable-live-mission-director --project PROJECT_ID` to the launcher.
This separately opts into only the displayed per-plan provider budget.
Stop the live Gateway after the bounded run. Never publish keys, raw session
databases, local paths or unsanitized run directories.

## Recorded development results, 2026-10-06

The [public summary](../assets/starship-mission-decisions-20261006/evidence-summary.json)
stores source/profile and artifact digests with selected observations and invocation
metadata. It excludes raw sessions, credentials and workstation paths. Its five
fixture pairs and two live pairs are frozen snapshots preceding the final
pending-start and observation-inventory checks; those records are not rewritten
as final-head live inference. A separate final-head keyless Gateway smoke covers
the added boundary.

| Mode / condition | Fixed timeline | Managed | Comparison |
|---|---|---|---|
| Scripted / normal | 26 releases; 4.55 m/s contact | Identical | Pass |
| Scripted / release fault | 0 releases; 238.74 m/s impact | 0 releases; 3.22 m/s contact | Pass |
| Scripted / tower unavailable | 26 releases; 4.55 m/s contact | Identical | Pass |
| Scripted / suspension notice | 26 releases; 4.55 m/s contact | 1 release; 619.78 m/s impact | Fail |
| Scripted / fuel loss | No contact before time limit | Same | Fail |
| Live / normal | 26 releases; 4.55 m/s contact | 1 release; 619.78 m/s impact | Fail |
| Live / release fault | 0 releases; 238.74 m/s impact | 0 releases; 3.22 m/s contact | Pass |

All seven stored pairs passed record verification. This is separate from the
failed completion gate. All booster branches selected/used the declared divert
site and failed its arrival envelope. There is no launch-derived catch or
qualified recovery result. The scheduled return time remained unchanged.

Live Jev: nine attempts, eight validated inference responses; one normal-monitor
response was `invalid_response`. Its cause is not established from hashed
response metadata. The consequent `stop_deployment` was a fallback, not an
accepted Jev command. Five valid Jev decisions in the release-fault run included
collecting a distinct mechanism report and selecting return guidance. Conditional
DeepSeek was never routed. Do not present this as an observed DeepSeek flight.

The release-fault improvement also occurs with the scripted policy. Neither
model superiority nor human labor savings are established. The no-response
fallback and both return choices are development mechanisms without a safe-return
guarantee; passing a scope check does not certify feasibility or safety.

Failed tuple-representation and disk-full attempts were preserved separately.
The full-suite rerun was interrupted by disk exhaustion; use targeted tests and
CI separately, without calling that run green. Claude Opus 5.5/high launched but
reported an expired login before reviewing; no completed Claude review exists.
