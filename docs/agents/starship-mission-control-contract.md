# MissionOS flight-control contract

## Goal and authority

The human approves a mission and its delegated operating bounds before flight.
Within those bounds, MissionOS's AI owns mission-level monitoring, evidence
collection, decisions and replanning across deployment, Ship return and booster
recovery. Local guidance and numerical estimators are tools; the AI does not
replace the fast attitude or engine-control loops.

The operational purpose is sustainable high-cadence flight: operators approve
delegated bounds and handle exceptions while MissionOS manages routine missions.
The [high-cadence operating requirements](starship-high-cadence-operations.md)
use three launch opportunities in 24 simulated hours as a project workload
assumption, not a claim about SpaceX's achieved rate. Single-flight recovery
qualification remains the first prerequisite; fleet execution is not implemented.

```text
LLM judges. Human approves. Rules constrain.
Executor acts. Verifier checks. Repair loops.
```

Normal operation must be observed as well as faults. The AI may choose a hold,
diagnostic, deployment reduction, return policy or capture/diversion decision
without a fresh human command when the approved phase-specific bounds allow it.
Holds consume time and may consume fuel; they are not inherently safe.

An independent execution check must enforce authority, fresh-state applicability,
time/resource limits and physical feasibility before a change that advances or
replans the mission. The same feasibility requirement applies to no-response
fallbacks. A valid model response and an allowed policy name are insufficient.
Unknown feasibility must not be reported as a qualified safe recovery.

Requests outside the grant require new human approval. The preflight contract
must specify the response deadline and what happens without a reply. A referral
record is not delivery to a human, and approval is not execution.

## Completion criteria

- Preserve normal mission outcomes without unnecessary interventions.
- Address the declared abnormal conditions through actual checked operations
  and confirm their effects in later observations.
- Exercise invalid responses, missing responses and relevant combined faults;
  a model/provider failure must not turn a recoverable approved flight into an
  unchecked return plan.
- Qualify every enabled choice and fallback for its stated operating domain.
  Record inputs, uncertainty, margins, deadlines and the independent verdict;
  apply the check again to fresh state at execution.
- Keep release completion, model entry/contact, vehicle recovery and record
  verification separate. A consistent collision record is not mission success.

Fixed-timeline and scripted runs check regressions and account for outcomes.
Beating a script is **not** an acceptance criterion for AI mission control:
the simulator's author can encode the intended answer in that script. Numerical
reasoning belongs in MissionOS's toolset. Report real model decisions and their
observed effects without treating script parity as evidence against their value.
Claims about unique AI advantage or reduced human workload need separate evidence.

## Current bounded return evidence and execution contract

Step 3 is a finite launch-to-contact census, not qualification of an orbital box.
`trimmed_state_terminal_v4` covers 0–26 retained inventories and selected initial
fuel offsets of ±1000 kg at 0/14/15/16/25/26. Its four original contact limits are
5 m/s, 5 degrees, 0.02 rad/s and 28 t reserve. Physical coefficients, geometry,
actuator force and the integrated state are not changed to pass those limits.

Step 4 uses qualification schema v2 and mission envelope v6. Every case contributes
its actual state at deorbit and its final 92 seconds of saved orbital-coast samples.
The table retains inventory, initial-fuel perturbation, study hash, time, inertial
position/velocity, full quaternion, angular velocity and fuel. Runtime admission
must match a corridor for the current inventory; it cannot borrow another count,
extrapolate time, or use the former 150–350 km / 600 km assumed orbital box.

Two-second sample interpolation has explicit numerical matching tolerances:
12 m position, 0.05 m/s velocity, 0.1 degree full attitude, 0.0002 rad/s angular
velocity and 100.1 kg observed fuel (including the existing 100 kg gauge bound).
The maximum bracketing gap is 2.01 s. These are numerical matching tolerances,
not independently flown physical perturbations or a proof between samples.
They are fixed in both implementations and recorded in the qualification asset.
The producer and an independent scalar checker reproduce the selected case,
sample times, residuals, fuel margin and admission. Inputs are bound to separately
saved pre-command integrated state. Source/profile/backend registration, three
available landing engines, attitude/rate checks, empirical consumption plus 28 t
reserve and 1 t margin, and an analytic deorbit-fuel correction also remain required.

A return choice is rechecked at dispatch and immediately before deorbit, including
no-response fallbacks. `inhibit_return` means no return burn and a checked 30 s
observation window, with **no later retry decision**. The run ends as
`return_inhibited_unresolved`. If even that coast resource bound is unavailable,
`halt_unresolved_return` records that the simulation has no qualified continuation.
Neither endpoint is a completed return or recovered vehicle.

After deorbit, a terminal fuel/propulsion violation cannot cancel entry. The
preapproved response is to record `terminal_return_domain_violation` and continue
the existing finite controller as `unqualified_best_effort_terminal_guidance`.
The simulator preserves the final contact or horizon outcome. Verification checks
that the failure flag, event and later result agree; it must not reinterpret a
well-formed failure record as qualified return. `terminal_engine_out` is an explicit
opt-in synthetic late-failure probe, outside the nominal qualification census.

Step 5 separates record checks, scenario checks and terminal mission results.
The fuel-shortage comparison checks inhibition and a 30 s observation only; its
`ship_return_qualified` is false and the Ship outcome remains unresolved.
The normal/release-fault/tower-unavailable/operations-notice cases can establish
modeled Ship contact, not whole-mission success. Booster diversion and splashdown
are different goals and must be recorded separately. Compound response failures
can preserve Ship contact while losing the deployment objective.

The 19 actual Jev receipts belong to the historical `6185ef92` evaluation (envelope
v5); they are not relabelled as inference on this revision. In those live runs,
Jev selected diagnostics and deployment changes. Four return selections retained
the initial `state_return` plan; the fuel-shortage selection had only inhibition
available. They do not demonstrate an AI-induced return-policy improvement.
Numerical tools are part of MissionOS; scripted parity is not an AI-value gate.

### Environment and requalification

Use `pip install -e '.[spaceflight-qualified]'` for the recorded numerical backend
(NumPy 2.4.6 / SciPy 1.17.1). The broader `spaceflight` extra remains useful for
unqualified development. Managed planning and pre-execution checks reject missing
qualification, changed source/profile or a backend mismatch with explicit reasons,
before launching a worker. A dependency mismatch must not silently turn a mission
into a coast-only run. Old approval envelopes cannot acquire v6 authority.

The qualification asset is
`docs/assets/starship-state-return-qualification/qualification.json`.
Its 15 source hashes cover the dynamics, guidance, admission and independent
checkers. Editing any covered file invalidates registration. The current builder
requires all 39 trials from the new exact source; it does not rewrite old hashes
or accept a mixture of runs. This costs 39 full CPU simulations even for a guard-only
edit. A future reviewed compatibility mechanism could reduce that cost; none is
assumed here. Failed and historical source-bound records remain preserved.

Runtime commands:

```sh
python scripts/run_starship_return_qualification.py --approve-simulation --wind-trim \
  --retained 15 --output-dir output/state-return-example
python scripts/run_starship_managed_mission.py --approve-simulation \
  --case operations_notice --splashdown --director-mode fixture \
  --output-dir output/mission-control-example
```

Use fresh directories and `spaceflight-qualified` for managed execution; the
qualified backend versions are in the asset. Do not overwrite failures or automatically
rerun a failed bounded trial. SDK credentials belong only to the host broker.

[Current results](../examples/starship-state-return.md) and
[current verification summary](../assets/starship-state-return-qualification/review-fixes-summary.json)
keep the first failed integration, old negative controllers, nominal water entry
and limits distinct. Booster water-entry robustness and true recovery/reuse,
continuous monitoring, in-flight human reapproval and multi-flight readiness/shared
resources/human workload remain outside this bounded revision. Fuel-shortage
recovery remains unresolved; five completed comparison checks do not close that gap.
