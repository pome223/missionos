# Orbital event supervision after M1

This follow-up covers only post-deployment orbital return planning. The human
approves a finite authority envelope; Jev decides within it; an optional
DeepSeek decision is subject to the same deadline; the credential-free executor
checks and acts; a scalar verifier reconstructs the event and execution record.
Ascent, deployment supervision, arbitrary-site targeting and fleet scheduling
are outside this change.

## Observed events

The finite plant loop samples the delivered recovery notice and observed engine
availability/body rate at each orbital control step, including during model and
forecast waits. No unpublished notice timestamp is exposed to the actor.

Events are notice content changes, notice expiry, observed health changes, and
one-shot candidate deadlines. The deadline lead is the approved forecast batch
budget (600 s) plus model deadline (35 s) plus a 60 s development margin. This
margin is an engineering assumption, not a SpaceX/NASA flight rule. Periodic
monitoring remains a backstop. Events coalesce by observation generation.

Requests bind current notice, generation and drained event IDs. New facts while
waiting invalidate the pending result and force fresh assessment. A deadline
event does not cancel an already-running forecast; it is serviced as soon as
that bounded forecast finishes. Response and event latency are recorded.

Observed unavailable engines or coast body rate above 0.02 rad/s immediately
inhibit return without waiting for a model. The latch cannot be cleared by AI.
Without a qualified recovery alternative, the finite trace ends **unresolved**;
this is not a safe landing claim. Current health must also match the forecast
origin at every selection and dispatch.

## Scenario and authority

The new catalog cases are `sixdof_m1_event_normal`,
`sixdof_m1_event_tradeoff`, and `sixdof_m1_event_timeout`. They retain M1's two
frozen return opportunities, two fixed synthetic 125 km study areas, contact
limits and original 6000 s delay limit. Their explicit envelope allows at most
16 Jev calls, 2 conditional DeepSeek calls, 20 forecasts, 2 prediction workers
and 4 revisions. The 39-run qualification and numerical physics are unchanged.

A notice at T+1450 s is not revealed in advance. In the normal case it confirms
unchanged conditions. In the tradeoff and timeout cases it corrects the uncertain
estimate for **post-contact servicing**, from 600–1200 s to 7200–14400 s in the
east; the western estimate remains 1200–2400 s. Both areas retain their separate,
explicit operating windows. A service estimate cannot grant clearance.

This creates an operational comparison: earlier eastern contact and less
waiting consumption versus later western contact and a shorter estimated
service delay. It is not a simulation of uncertain sea safety or physical
recovery crews. The service intervals are synthetic, not estimated SpaceX data,
and actual servicing completion is not simulated or claimed.

Normal updates must preserve nominal return. The tradeoff case requires an
executed live AI selection after the correction, with **both** numerical return
candidates admitted at that decision. Either choice may pass; choosing a
different action from fallback is not itself a success condition. The timeout
case loses responses persistently from the correction onward and must use the
same independent admission for its preapproved fallback. Both options' measured
contact/time/resources and the selected path must be reported separately from
the uncertain service estimate. Rules parity is not an AI-value rejection gate.

## Verification and limits

The independent event audit replays the observed notice/health/deadline stream,
checks continuous orbital coverage, missing/forged events, notice and generation
binding, exact event draining, late responses and urgent dispatch inhibition.
Existing scalar checks still cover authority, fresh state, finite resource
budgets, contact geometry, recorded execution and actual recovery area/time.

The areas were calibrated from prior predictions. There is no continuous search
for an arbitrary external recovery site. The half-step forecast displacement
of about 77 km is **not bounded by** the 20 km footprint padding. Requiring all
four trial predictions (including the finer grid) to lie inside the unchanged
area is a sampled check, not a converged numerical uncertainty bound. Numerical
convergence remains unresolved. No new result should be described as real-site
accuracy, physical splashdown success, or held-out operational reliability.

## Runtime evidence

The three HTTP runs passed record verification and their declared case gates.

| Flight | Executed plan | Observed Jev / DeepSeek calls | Contact |
| --- | --- | --- | --- |
| Normal | nominal | 5 / 0 | 4.608 m/s; 2.375°; 62.394 t |
| Corrected servicing forecast | nominal | 5 / 0 | 4.608 m/s; 2.375°; 62.394 t |
| Persistent outage after correction | nominal | 0 / 0 | 4.608 m/s; 2.375°; 62.394 t |

Both live flights detected the T+1450 s notice after 0.2 simulated seconds and
requested reassessment at the same observed step. Both received two admissible
returns after the update and selected nominal return. The corrected flight did
not produce a different selected return or final state from the outage fallback.
This result is retained without another live attempt or prompt tuning.

Normal final state and contact exactly match the historical M1 normal flight.
All three release 26 generic rigid payloads and use zero inflight human commands.
The outage uses no model calls; its checked fallback preserves nominal return.
These are test-operator approvals, not authenticated human identity.

A separate scripted next-orbit counterfactual is pending to exercise the long
coast and alternative dispatch. It is not an AI-selected alternative or part of
the three live/outage case gates.

Starship regression: 3383 passed, 5 skipped. Focused M1/event checks: 61 passed.

Run through `scripts/start_starship_gateway.py` and
`scripts/smoke_starship_chat_gateway.py` with the selected catalog case. Use
the `spaceflight-qualified` extra, a fresh state/output directory and explicit
simulation approval. Live keys are read through Secret Manager in the host only.
No automatic retries or undisclosed prompt tuning are included in the result.
