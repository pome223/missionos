# MissionOS: changing a return plan during flight

The M1 scenario starts with a launch and 26 generic rigid payload releases.
A recovery coordinator then withdraws the planned area. MissionOS can ask its
numerical tool about a later orbital opportunity, wait under an approved budget,
read an updated recovery status and choose a revised return. The executor checks
the latest state and constraints before acting; a verifier checks what happened.

The three recorded HTTP runs reached their M1 acceptance criteria:

| Flight | Executed return | Ship contact | Jev calls |
| --- | --- | --- | --- |
| Normal | Original plan, eastern study area | 4.608 m/s; 2.375° tilt; 62.394 t fuel | 3 live |
| Recovery update | Jev selected the next orbital opportunity, western area | 4.613 m/s; 2.396° tilt; 62.434 t fuel | 10 live |
| Persistent decision outage | Checked fallback selected the later opportunity | 4.613 m/s; 2.396° tilt; 62.434 t fuel | 0; fixture outage |

All three released 26 generic rigid payloads and used zero human inflight
commands. The normal contact and final state exactly matched the fixed timeline.
The changed flight returned about 90.85 minutes later. Its measured contact was
inside the fixed western area's available interval; the original contact, when
rescored against the changed notice, would fall outside the available area/time.
That rescoring is not an additional baseline flight. DeepSeek was available but
not requested. Jev call counts alone are not an outcome metric.

[Compact execution evidence](../assets/starship-m1-operations/summary.json)
records decisions, model receipts, revisions, finite forecasts, constraints,
contact measurements and preserved development failures. It is a reviewed
summary, not the full studies required to rerun every record-verifier check.

The human approves the scope once. Jev makes the mission decisions inside that
scope, with DeepSeek available for deeper reasoning when requested. Numerical
prediction and control are tools of MissionOS. Matching a scripted policy is
not the criterion for whether AI was involved.

The three registered scenarios are:

| Scenario | What it must establish |
| --- | --- |
| `sixdof_m1_normal` | Monitoring preserves the original return without unnecessary delay. |
| `sixdof_m1_replan` | An actual model decision leads to a later return after updated recovery information, then measured contact in the approved study area/time. |
| `sixdof_m1_timeout` | A missing decision triggers independently checked continuation, with the actual result preserved. |

Plan one in the Starship chat, inspect its scope, then use `/approve` and `/run`.
The report shows decisions, plan revisions, dispatch time and observed contact.
Fixture responses are labeled separately from model inference.

These are development simulations. The two fixed study areas were calibrated
from earlier engineering forecasts; this is not proof of guidance to an
arbitrary real recovery site. Quantized navigation observations and waiting power, temperature
and propellant-loss bounds are explicit synthetic assumptions. Three perturbed
forecasts plus a finer-step forecast must all pass. These are useful checks,
not a proof covering every uncertain state. The finer-step check shifted predicted
locations by about 77 km, so precise real-site guidance is not established.
Ship contact limits remain 5 m/s, 5° tilt, 0.02 rad/s and 28 t fuel reserve.
Real water entry, recovery crews, Starship accuracy and booster recovery are
outside this milestone.

Implementation and evidence requirements: [M1 contract](../agents/starship-return-replanning-m1.md).

Next stage: [responding to changes in orbit](starship-orbit-events.md) adds event-triggered reassessment and a two-option operational tradeoff. Its new tests are reported separately from the three M1 flights above.
