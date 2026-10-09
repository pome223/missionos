# MissionOS: responding to changes in orbit

This follow-up lets MissionOS reconsider a return plan when information changes,
without waiting for the next periodic check. It covers the orbital interval
between payload release and the start of return. The earlier [M1 flights](starship-m1-operations.md) remain a separate set of
results.

A corrected recovery notice, an expired notice, an observed health change or an
approaching return deadline can trigger reassessment. The simulated spacecraft
continues flying while Jev decides or a numerical tool runs. New information
invalidates an outdated response. An urgent health problem inhibits return
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

Detailed scope and verification requirements:
[orbital event supervision contract](../agents/starship-orbit-events.md).
