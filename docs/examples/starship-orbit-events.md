# MissionOS: responding to changes in orbit

This follow-up lets MissionOS reconsider a return plan when information changes,
without waiting for the next periodic check. It covers the orbital interval
between payload release and the start of return. The earlier [M1 flights](starship-m1-operations.md) remain a separate set of
results.

A corrected recovery notice, an expired notice, an observed health change or an
approaching return deadline can trigger reassessment. The simulated spacecraft
continues flying while Jev decides or a numerical tool runs. New information
invalidates an outdated model response. A harmless update preserves a return
that still satisfies the latest checks, including just before its scheduled burn.
A withdrawn permission still blocks it. An urgent health problem inhibits return
without waiting for AI; if no qualified alternative exists, the recorded outcome
remains unresolved.

The human approves the available choices and limits before flight. Inside that
scope, Jev decides whether to preserve or change the plan, with DeepSeek available
when Jev requests further reasoning. Numerical tools check trajectories and
resources; independent checks constrain execution and verify the subsequent
observations. Normal operation does not require a person to approve each step.

## Two usable returns, different operating costs

The scenario retains M1's two return opportunities: an earlier return to the
eastern study area and a later return to the western area. Both areas have
explicit operating permissions. A separate advisory estimates how long
servicing might take **after contact**. During the flight, that estimate can be
corrected while both operating permissions remain valid.

MissionOS can compare earlier contact and lower waiting consumption with later
contact and a shorter estimated servicing delay. The revised-notice test must
show both numerical return candidates passing their checks when the AI selects
one. A prediction about servicing cannot grant permission to use an area.

The servicing estimates are synthetic assumptions. Recovery crews and servicing
completion are not simulated, so an estimated completion time is not an observed
recovery result. The study areas are fixed engineering targets; this work does
not demonstrate guidance to an arbitrary real recovery site.

## Three separate tests

| Scenario | What it must establish |
| --- | --- |
| `sixdof_m1_event_normal` | A notice confirming unchanged conditions triggers reassessment while preserving the original return. |
| `sixdof_m1_event_tradeoff` | A corrected advisory reaches the AI promptly; its selection has two admissible return options and is followed through to measured contact. |
| `sixdof_m1_event_timeout` | Persistent missing responses after the correction invoke the preapproved, independently checked fallback, with its actual outcome recorded. |

The [execution summary](../assets/starship-orbit-events/summary.json) records
these new tests separately from M1. It distinguishes observed events, AI
calls, executed choices, fallback actions and measured outcomes. Matching a
scripted fallback does not mean AI was absent; whether AI acted and whether its
choice changed the outcome are separate questions.

## Recorded result

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

Detailed scope and verification requirements:
[orbital event supervision contract](../agents/starship-orbit-events.md).
