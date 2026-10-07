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

## Current qualified simulation domain

Steps3–5 are now implemented for this bounded model profile:

1. `trimmed_state_terminal_v4` uses common model-based bank/trim preparation,
   finite surface prepositioning and state-based terminal timing for 0–26 retained
   inventories. The frozen census has39passing flights:27inventories and12initial
   fuel ±1000kg probes for inventories0/14/15/16/25/26. All four original limits
   remain5m/s,5deg,.02rad/s and28t. No aerodynamic coefficient, geometry, RCS
   force or physical state is fitted/reset to make a flight pass.
2. Both model decisions and fallback return choices pass fresh authority and
   quantitative model-domain admission. Before deorbit the executor checks again.
   Registration binds physical source/profile/backend to the qualified data.
   Fuel has a100kg observation bound; required fuel combines the tested consumption
   envelope,28treserve,1tmargin and analytic deorbit correction. Orbital state,
   coast attitude/rate and three landing engines are checked. Terminal execution
   separately checks source-qualified terminal consumption/reserve/propulsion.
3. The latest five conditions were run in fixture and actual-Jev mode. Each live
   run uses HTTP plan/approve/run, a consumed test-operator grant, a separate key-free
   worker, signed receipt/artifact hashes and later observations. There are19Jev
   inference receipts and0conditional DeepSeek calls. Numerical reasoning remains
   a tool for MissionOS; parity with the fixture is not an AI-value rejection.

The admission guard is a tested-model-domain check, **not** a per-state full
trajectory rollout or a formal reachable-set guarantee for every combination.
Outside its profile/backend/state domain, return is unknown. A checked30scoast
uses a worst-case12-RCS-jet fuel bound plus1tcoastreserve. Its completion means
an unresolved mission, not recovered hardware or guaranteed indefinite survival.
If neither return nor coast is admitted, execution is inhibited without a safe
recovery claim. A terminal-domain failure stops the simulator rather than silently
commanding an unchecked burn; no physical fallback guarantee is claimed.

Independent verification recomputes scalar fuel/orbit/pose margins, binds inputs
and model parameters to integrated pre-command states, checks both accepted and
fallback decisions, execution-time receipts and the terminal margin, and verifies
later observations. Raw native numeric values cross a canonical JSON boundary
before the same strict checker; its numeric/type constraints are not weakened.
Static trim witnesses are checked by a separate scalar plate implementation.
Stored consistency is not processor/hardware execution attestation.

The normal fixture and live flight preserve the fixed-timeline outcome. Fuel
shortage deliberately stops deployment and returns no contact/recovery claim.
Two compound hold/reassessment/return response failures forfeit deployment but
retain qualified modeled contact. Their normal-comparison result stays false;
return recovery is not whole mission success.

The qualification asset is
`docs/assets/starship-state-return-qualification/qualification.json`.
`build_starship_return_qualification.py` refuses missing/failed/mismatched cases,
changed sources and invalid independently checked JSON. Model-policy registration
requires complete current-source data; ordinary verification still refuses undeclared
experimental scope. Older v3/failed endpoint/global-zero records remain historical.
The current use-witness checker assumes the declared three-hull/four-flap layout.

Runtime commands:

```sh
python scripts/run_starship_return_qualification.py --approve-simulation --wind-trim \
  --retained 15 --output-dir output/state-return-example
python scripts/run_starship_managed_mission.py --approve-simulation \
  --case operations_notice --splashdown --director-mode fixture \
  --output-dir output/mission-control-example
```

Use fresh directories and the optional `spaceflight` dependencies; the qualified
backend versions are in the asset. Do not overwrite failures or automatically
rerun a failed bounded trial. SDK credentials belong only to the host broker.

[Current results](../examples/starship-state-return.md) and
[decision/source summary](../assets/starship-state-return-qualification/steps345-summary.json)
keep the first failed integration, old negative controllers, nominal water entry
and limits distinct. Booster water-entry robustness and true recovery/reuse,
continuous monitoring, in-flight human reapproval and multi-flight readiness/shared
resources/human workload remain outside completed steps3–5.
