# High-cadence MissionOS operating requirements

## Purpose and workload assumption

Develop mission control that can sustain repeated flights with human approval
of delegated bounds and exception handling, rather than requiring a person to
choose every routine in-flight action. A compelling replay is a presentation
of evidence; completion requires measured operations and checked outcomes.

The initial workload target is **three launch opportunities over 24 simulated
hours**, across one or more vehicles. This is a user-selected project assumption,
not a verified current SpaceX launch rate, a predicted timetable, or proof that
one vehicle can fly three times daily. SpaceX's published design emphasizes
[full and rapid reusability](https://www.spacex.com/vehicles/starship) and
[high-cadence operations](https://www.spacex.com/updates/reusability).

## Required operating behavior

- Maintain continuous mission-level observations, phase-specific decision
  deadlines and bounded model-call budgets. Numerical guidance, estimation and
  feasibility checks are tools available to AI mission management.
- Manage simultaneous orbital, return and next-flight preparation activities
  against a common clock. A pending model or human response must not freeze
  vehicle dynamics or block unrelated flights. Current sequential Ship/booster
  integration does not establish this capability.
- Track vehicle identity, payload inventory, fuel, readiness and plan revision
  across flights. Require observed recovery and qualified inspection/readiness
  evidence before reuse. Low-speed contact or a reset initial state cannot
  create a reusable vehicle.
- Track shared pad, tower, fueling and communication resources and applicable
  operating windows. An abnormal flight must trigger checked changes to later
  plans where necessary. A simulated resource allocation is not real clearance.
- Bind every decision to its mission, vehicle, approved scope and fresh
  observations. Cross-flight observations, receipts and authority cannot be
  substituted. Changes beyond delegated bounds require human approval.
- Handle provider timeouts, invalid answers and combined faults through
  independently checked fallbacks. Track pending referrals and deadlines;
  asking for approval is not evidence that a human received or answered it.
- Confirm each applied change in later observations and carry its consequences
  into the fleet plan. Distinguish payload delivery, return, vehicle readiness
  and availability for the next flight.

## Operational evaluation

Freeze the workload, vehicle/resource inventory, operating windows, fault
series, observation uncertainty and qualification domain before evaluation.
Include normal operations, partial payload retention, unavailable ground
resources, conflicting or revised notices, model unavailability and overlapping
faults. Policies receive observed information, not future fault schedules.

Report these quantities separately, with a documented measured or modeled basis:

| Question | Evidence |
|---|---|
| Can the approved workload finish? | Planned vs. completed flight stages, deployment counts, return results and unmet objectives |
| Can humans supervise it sustainably? | Approval/referral counts, actual interventions, response times and measured operator work; compare with a per-step human operating procedure before claiming labor savings |
| Are decisions timely? | Deadline misses, pending-request queue length, unnecessary holds and delay propagated to subsequent flights |
| Are resources used consistently? | Occupancy intervals, conflicting allocations, fuel margins and observed readiness transitions |
| Does a failure remain contained? | Results of the affected flight and unrelated flights during provider and compound faults |
| Is the result independently checkable? | Plan revisions, observations, numerical applicability/margins, execution receipts and later effects bound to each mission |

A fixed timeline is a regression reference. Outperforming a script that encodes
the intended answer is not the acceptance criterion. AI's operational role is
to manage evidence, goals and checked actions within the delegated scope.
Claims of robust recovery, fleet availability or staffing reductions require
the relevant measured evidence; zero human commands in a fixture is insufficient.

## Implementation order and current limits

Continue the [flight-control contract](starship-mission-control-contract.md):

1. Diagnose Ship and booster failures together; qualify usable Ship return
   guidance across 0–26 retained payloads in the stated fuel-sufficient domain,
   and qualify booster recovery separately. Preserve failed cases.
2. Independently check return choices and fallbacks against fresh state,
   resources and uncertainty. Validate the five single-flight conditions and
   compound response failures on the resulting source.
3. Add the shared clock, resource/readiness ledger and repeated-flight manager.
   Then run the three-opportunity workload and measure operator involvement.

These are requirements, not implemented fleet capabilities. Reusability
inspection, concurrent fleet dynamics, real operating clearances and human
workload measurements are not provided by the current simulator.
