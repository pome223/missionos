# Aerial WAM: capability acceptance, not rule superiority

Evaluation policy revised on 2026-09-26 following the user's clarification.
This supersedes the requirement that WAM must beat a rule/depth baseline before
its capability can be evaluated. It does not alter previous measurements,
registered cohort decisions, execution permissions or safety constraints.

## Question and information boundary

The question is whether WAM can provide useful candidate predictions and choices
for building gaps, climb and detour with the observations available to the drone,
at sufficient absolute performance for a specified operating envelope.

A route selector with complete scene geometry has privileged information and
can be very strong in an idealized simulator. Do not use winning against that
selector as an acceptance criterion or a reason to terminate WAM research.
Use simulator truth to verify outcomes and enforce independent constraints;
keep it out of the model/selector input. Rules retain their safety authority.

Evaluate under declared sensor and map availability: calibrated observation
history, pose, goal and candidate actions; an absent or incomplete prior map when
that is the intended use. Cover supplied gap, climb and detour tasks, including
clear passages and blocked ones, and held-out layouts within the declared scope.
Do not construct only cases where another method fails. Record any supplied route
templates and manual scene-specific work; do not claim arbitrary route generation.

## Acceptance dimensions

Before a new cohort, freeze the operating envelope, cases, attempts, endpoints,
numeric acceptance bounds and compute/time budget. Numeric bounds have **not yet
been established by this policy change**. Derive them from the intended speed,
clearance, acceptable hover time and task duration; do not choose them after seeing
model outputs. No new acceptance pass or failure is assigned to historical data.

| Dimension | Required record |
|---|---|
| Arrival | Goal dwell and required landing/disarm; all attempted cases in the denominator |
| Safety | Observed contacts, clearance breaches, constraint vetoes and safe aborts, separately |
| Decision usefulness | WAM's selected action and admissibility before the independent gate; unnecessary stops/detours |
| Forecast usefulness | Action- and time-aligned predictions against observed consequences; image MSE alone is insufficient |
| Responsiveness | End-to-end observation-to-decision latency, observation age, hover duration and timeouts |
| Practical effort/cost | Required mapping and route authoring, model compute and inference cost |

Report both the governed pipeline outcome and the model's proposal quality.
A vetoed WAM proposal followed by a Rules-selected fallback is not a successful
WAM route choice. Safe abort can be an appropriate safety outcome while still
counting as no arrival. Rules may constrain a usable WAM without WAM replacing or
outperforming Rules. Prediction, approval, constraints, dispatch and verification
remain distinct facts.

An observation-matched simple method may be retained as a diagnostic reference,
with its inputs and manual preparation explicit. Superiority is not required.
A case in which depth already succeeds can still test WAM capability; it cannot
establish additional arrivals over that method. Keep these two questions separate.

## Stop and continuation criteria

- Stop an individual run on violated execution constraints, invalid/stale input,
  infrastructure failure or exhausted attempt/budget limits. Preserve the record
  and distinguish infrastructure failure from model failure.
- Judge capability against the predeclared absolute bounds above. If the model
  misses them, record the specific failure before deciding whether a bounded
  revision or post-training targets it. Do not tune indefinitely on evaluation cases.
- A rule/depth method succeeding, or having zero additional-arrival headroom,
  **is not a WAM capability failure or a research stop condition**.
- The earlier frozen R2 study remains closed under its original improvement
  question. New capability evaluation is a separately specified cohort, not a
  rewritten R2 result. This document does not start GPU work or relax runtime gates.

The [2026-09-26 pilot protocol](aerial-wam-capability-pilot-20260926.md) fixes the
first small static-scene cohort and its absolute bounds before new model outputs.
It uses the existing ANWM route experiment; the [Gateway checkpoint
lifecycle](px4-depth-gateway.md) remains a separate depth-based implementation.
Neither a frozen protocol nor existing flight evidence is a new acceptance result.

The [fresh pilot results and replay](../assets/aerial-wam-capability-20260926/README.md)
record 3/3 governed arrivals within the predeclared bounds, 2/3 admissible raw
model choices, and 1/3 requested maneuvers executed from the model's top choice.
This is absolute capability evidence in known static scenes, not a superiority
result or WAM integration into the Gateway reobservation adapter.
