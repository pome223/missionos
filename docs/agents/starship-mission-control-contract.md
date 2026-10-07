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

## Current baseline and next qualification

The [implemented decision envelope](starship-mission-director.md) is a bounded
development slice, not completion of this contract. It has finite decision
points and one hold reassessment; continuous monitoring and in-flight human
reapproval remain incomplete. Return selection checks the grant but has no
state-dependent feasibility gate. Partial retained-payload return and combined
hold/reassessment/return-response failures remain unqualified.

The [surface-allocation experiment](../examples/starship-return-allocation.md)
improves the tested 25/26-retained returns without increasing RCS authority.
Its initial domain sweep stops on the zero-retained pose failure. A reviewed
active-only sweep passes 17 of 18 tested retained counts and stops on the
15-retained reserve failure; counts 17–24 remain unexecuted. The allocator
remains unavailable to MissionOS grants; this is not full return qualification.
The fixed-return fallback is unchanged. These retained-policy results cannot
qualify that fallback; step 2 must check both choices and no-response behavior.

The opt-in reproducible launch boundary is:

```sh
python scripts/run_starship_return_qualification.py --approve-simulation \
  --retained 25 --output-dir output/return-qualification-example
```

`result.json` separates stored-record checks from the provisional speed, pose,
rate and reserve gate. It does not certify recovery. Use a fresh output directory.
For a short paired allocation test, first produce a legacy reference with
`--legacy-allocation`, then pass its single-run `study.json` to
`scripts/run_starship_entry_allocation_comparison.py --approve-simulation
--run-record ... --output-dir ...`. That checkpoint experiment uses 0.1 s steps,
not the production variable-step schedule. No API key or model call is required.

After merging the current development baseline, the work order is:

1. Diagnose the 25-retained-payload failure and qualify at least one Ship return
   policy across 0–26 retained payloads in a frozen, fuel-sufficient operating
   domain. Include attitude, rates, reserve and time-window margins; payload
   count alone cannot establish feasibility. Keep booster water-entry
   robustness as a separate qualification, including the failed fuel probes.
2. Gate both AI-selected return policies and fallbacks on independent numerical
   feasibility results. If no policy qualifies, report that gap rather than
   labeling an unverified fallback safe.
3. Rerun all five declared conditions on the resulting source and add compound
   response-failure cases. Preserve failed outcomes and source-bound records.

Merging this opt-in simulator baseline does not certify recovery or satisfy the
completion criteria. [Recorded outcomes](../examples/starship-mission-decisions.md)
and [booster entry limits](../examples/starship-splashdown.md) retain their scope.
