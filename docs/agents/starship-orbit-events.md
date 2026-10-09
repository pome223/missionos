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
budget (600 s) plus model deadline (35 s) plus the approved `deadline_margin_s` (60 s in these cases). This
margin is an engineering assumption, not a SpaceX/NASA flight rule. Periodic
monitoring remains a backstop. Events coalesce by observation generation.

Requests bind current notice, generation and drained event IDs. New facts while
waiting invalidate the pending model response and force fresh assessment. A deadline
event does not cancel an already-running forecast; it is serviced as soon as
that bounded forecast finishes. Response and event latency are recorded.

A notice revision does not invalidate a physical forecast. Each existing
forecast must still match the fresh measured state, and its predicted contact
must satisfy the **current** area permission, time and resource checks. A benign
notice therefore preserves the forecast and the booked revision. Reforecasting
is needed for missing or mismatched physical evidence, not a changed notice ID.

Inside `decision_timeout_s + coast_dt_s` of a previously booked burn, the
approved `commitment_response` permits a `deadline_fallback` without publishing
a model request or consuming its time budget. It preserves only the same
currently admissible booking. It cannot override revoked permission, admit a
new candidate or move the burn. The record explicitly has no model response and
no later measurement; subsequent execution and observations are checked
separately. The verifier independently repeats the timing, revision and fresh
admission checks. Interrupted stale responses are not labelled applied fallbacks.

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
Every delivered notice, each revision notice and the dispatch notice are also
checked against the registered scenario at their observed action time; a self-consistent trace that silently omits an update fails. Undrained
input requires an explicit, substantiated terminal disposition even when no
return is dispatched. A health inhibit records an immediate unresolved stop.

Verification schema v2 binds the verdict to a canonical study hash, expected
case, trusted CLI/Gateway run ID, approved envelope, source map and verifier
source hash. A success-shaped verdict from another run cannot serve as evidence.
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

Runtime source `108e1871` passed record, contact and area verification in all
three HTTP flights. **Two of three declared case gates passed.** The corrected
advisory flight remains unresolved because the AI did not request the second
return forecast; it therefore did not make a choice between two admitted returns.

| Flight | Case gate | Jev attempts / valid inference receipts | Forecasts started | Return |
| --- | --- | --- | --- | --- |
| Normal | pass | 5 / 5 | 4 | nominal |
| Corrected servicing forecast | **two-option condition unmet** | 5 / 4 | 4 | nominal |
| Persistent outage after correction | pass | 0 / 0 | 8 | checked nominal fallback |

DeepSeek calls and inflight human commands are zero. All three release 26 generic
rigid payloads and reach 4.608 m/s contact, 2.375° tilt and 62.394 t propellant in
the eastern study area. The normal final state and outcome exactly match the
historical M1 normal flight.

The T+1450 s notice is detected after 0.2 simulated seconds and triggers a
request at the same observed step. Normal flight preserves its original forecast
and one booked revision across the update. In the corrected-advisory flight,
Jev chooses to retain the plan instead of calculating the second option. Its
last response is invalid; checked fallback still preserves the nominal return.
The persistent-outage flight cancels a forecast on new information and then
uses checked fallback. The failed case gate and original HTTP result are kept;
no prompt tuning or further live attempt was used to force a pass.


A later **verifier-only** correction recognizes live reaffirmations of the same
booking without requiring a new revision. The original HTTP study, verdict and
receipt are unchanged. Separate rechecks bind the original approved inputs to
the new verifier and reject differences in any execution source other than that
verifier. The verdicts remain pass / unmet / pass; the corrected flight still
has only one predicted candidate. This is recorded revalidation, not three new
flights. The evidence summary preserves both verdicts and their source hashes.

The [pre-review evidence](../assets/starship-orbit-events/pre-review-summary.json)
includes the older-source scripted next-orbit counterfactual: 90.85 minutes of
delay, 4.613 m/s contact and 2.396° tilt in the western area. It establishes a
previously executed alternative branch, not a new live AI decision or a
replacement for the current unmet two-option condition. Test-operator approvals
do not establish authenticated human identity.

The frozen runtime passed 3455 Starship regression tests (5 skipped); the focused
suite including both final verifier-only corrections passed 175 tests. Late benign notices,
late clearance revocation and urgent health inhibition use a finite fake-plant
production-loop harness, not additional HTTP physical flights.

Run through `scripts/start_starship_gateway.py` and
`scripts/smoke_starship_chat_gateway.py` with the selected catalog case. Use
the `spaceflight-qualified` extra, a fresh state/output directory and explicit
simulation approval. Live keys are read through Secret Manager in the host only.

## Review disposition

Two read-only reviews used Claude Opus 5.5 with high effort. The final review
confirmed all six first-round findings resolved and reported no code blockers.
The unmet two-option scenario remains a separate reason to keep this PR draft.

Its additional notice-binding recommendation (R2-1) was subsequently implemented
and checked by Codex: revision and dispatch notices must match the registered
input by canonical hash, with finite action times equal to observation times.
Saved HTTP records were rechecked without a new flight; all three record verdicts
and the pass / unmet / pass case results remain unchanged. Two self-consistent
forged notices in in-memory copies of the normal HTTP record were rejected.
Original artifact bytes remain unchanged. This narrow final fix was not submitted
for a third independent review.

R2-2 (a combined verifier/audit-module identity digest) is deferred: the approved
source map already binds the event audit module, and the recheck rejects any
change to it. R2-3 (more explicit termination reasons on additional failure exits)
is also deferred: unsupported undrained exits fail verification conservatively.
Neither item establishes a successful recovery outside the recorded conditions.
