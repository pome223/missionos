# Starship trouble engine v1 contract

## Scope and provenance

Research catalog: `starship-incident-catalog.json`, schema
`missionos.starship_incident_catalog.v1`, researched 2026-10-13. Fourteen
integrated flights are catalogued; 20 incident entries are evidence-backed
paraphrases, not independent reconstruction. Empty incident lists mean no
unexpected event verified in the selected primary narrative. Planned stress,
disposition and destructive tests, FAA test-induced-damage exceptions, and
unexpected failures have separate fields. Ground-test Ship 36 is excluded.

The six default families are engine availability loss during stack ascent,
Ship ascent, or booster boostback; a signed RCS pair loss; payload release
blockage; and tower-health unavailability. `flap_jam` is synthetic-only and
explicit opt-in. History motivates capability-loss abstractions. This code does
not solve combustion, filters, plumbing, fire, structural breakup, thermal
protection, communications hardware, or a real launch termination system.

## Frozen distribution

`make_manifest(seed, allowed_families=None, max_events=1,
nominal_probability=.2)` draws with SHA256 named independent streams. Seeds are
integers in `[0, 2**63)`. The pool must contain unique registered families.
Nominal probability defaults to 0.2. Otherwise event count is uniform 1 through
`max_events`; families are selected without replacement, targets uniformly from
the registered component pool, delays uniformly from each family's interval.
All these quantities are **synthetic test design**, not inferred incident rates.
`max_events=0` creates a nominal run. Explicit fixtures carry a different
`generator` label and must never be reported as blind randomized evaluation.

| Family | Body / phase | Delay from first phase entry |
|---|---|---|
| ascent_engine_out | Ship loop / stack_ascent | 20–100 s |
| ship_engine_out | Ship / ship_ascent | 5–60 s |
| rcs_channel_loss | Ship / orbital_coast | 0–90 s |
| payload_jam | Ship / orbital_coast | 0–10 s |
| tower_unavailable | Booster / booster_boostback_slew | 0 s |
| booster_restart_failure | Booster / booster_boostback | 0 s |
| flap_jam (explicit only) | Ship / ballistic_return | 0–60 s |

The two body clocks are tracked independently. Timesteps split at trigger times;
zero-time phase transitions activate before the next command is applied. Events
outside a reached phase remain unapplied; no draw retry or forced occurrence.
Faults persist for the remainder of this trial, even if the historical incident
was temporary. Restart failure removes availability at boostback entry; it is
not an ignition-sequence or turbopump model.

## Plant boundary

`simulate_concurrent` accepts an optional `trouble_manifest`. The legacy
sequential `simulate` rejects a non-null manifest: cross-body health observations
require the shared clock, not after-the-fact Booster postprocessing.
No manifest preserves the existing default path and output schema. The
standalone CLI uses the concurrent scheduler. Only launch, fixed baseline
control, and an unqualified standalone run accept a manifest. Mixing with an
existing Mission Director, flight supervision grant, development return
qualification or registered return policy is rejected before integration.
Legacy `mission_case` faults and external deployment/return/catch configuration
are also rejected so no second fault or control source bypasses the frozen setup.
A separate [experimental trouble-control CLI](starship-trouble-control.md)
binds the test setup to its own explicit execution contract and uses actual
post-fault observations. It grants only the documented diagnostic/inhibit
actions, never nominal return qualification. The standalone runner does not
invoke that adapter.

`before_step` changes engine availability only, retaining actual throttle,
gimbal, position, attitude, rates and fuel. The physical load computation removes
unavailable thrust. Stack failures remain on the separated booster. Ship has its
own engines. An actuator jam holds subsequent command targets at its current
angle. Payload blockage prevents the existing physical separation operation,
retains payload mass/inertia and engages the existing interlock. Tower health
changes current diagnostics; this does not create a qualified catch trajectory.
A faulted ascent that never reaches orbit continues into the existing ballistic
return controller from its actual state rather than inventing an orbital state.

## Information boundary

The private manifest, seed, phase schedule and audit are harness truth and must
never be supplied to a model. `observation()` emits only current component health:
engine availability, actuator health, payload mechanism and tower readiness.
This channel is explicitly `synthetic_component_health_v1`: idealized diagnostic
availability, not noisy reconstructed SpaceX telemetry. It is an interface for
observers; the standalone runner makes no model call. The separately approved
[trouble-control adapter](starship-trouble-control.md) consumes a body-scoped
projection and records its live provider attempts separately. Those executions
do not upgrade standalone record verification into a recovery claim.

## Evidence and verification

The CLI freezes manifest, profile and all Starship runtime source hashes before
execution, requires a fresh output directory and opt-in, and retains an exception
record on failure without retries. JSON serialization precedes strict verification.
A SHA256 chain binds application receipts to the manifest; this is consistency
and tamper detection, not external execution attestation.

`verify_run` checks seeded schedule identity, phase-relative application times,
receipt chain, unapplied partition, sampled/final availability, sampled jam
angles/commands and separation inheritance. It does not rerun the physics.
A pass means **record consistency**, never successful recovery, validated Starship
physics, continuous-time actuator observability, or AI control success. A
collision can have a consistent record. Short horizons and unreached phases are
reported explicitly. Renderer fault annotations contain applied events only.

Default return qualification binds the mission and booster source files.
The final source completed the existing 39-run no-fault qualification refresh: retained 0–26 and fuel ±1000 kg at retained 0, 14, 15, 16, 25 and 26. All 97,703 saved samples and complete run fields match the prior baseline exactly except source identity. Certificate numerical values and matching tolerances are unchanged. The [portable refresh audit](../assets/starship-trouble-engine-20261013/qualification-refresh.json) also retains the earlier superseded-source and monitoring-error accounting.
Do not relabel old studies, widen matching tolerances or approve faulted return
on the strength of this default regression. The reviewed numerical environment
remains NumPy 2.4.6 / SciPy 1.17.1. Default behavior regression and injected-fault
outcomes are separate results.

## Evaluation discipline

Fault families, seeds, horizons and budgets are chosen before trial execution.
Do not retry until a desired fault, provider action or successful recovery occurs.
Rules/fallback are authority constraints, safety nets and regression checks;
parity with encoded Rules does not discredit AI. Future managed trials evaluate
whether AI noticed events by deadlines, used available evidence/tools, updated
or deliberately retained plans, passed independent bounds, and checked observed
outcomes. Unknown or unrecoverable states remain unknown or failed.
