# Preparing a usable return tool for MissionOS

Repeated flights need guidance that can return with a changed payload inventory.
This development step extends the bounded finite-actuator allocator to Ship
flaps. The vehicle coefficients, RCS force and actuator limits are unchanged.
The candidate remains a local opt-in experiment, unavailable to MissionOS plans.
The improved returns require selecting retained-return guidance. The existing
`fixed_return` no-response path is unchanged and can still impact with retained
payloads; this experiment does not make that fallback safe.

In paired 180-second continuations from saved 25-retained entry states, the
legacy allocation reaches about 44 degrees of attitude error; the bounded
candidate stays near 0.5 degrees. Both methods stay below 5 degrees in the
predeclared 50–600 Pa diagnostic window; the large divergence occurs afterward.
These pairs use common 0.1-second steps and do not reproduce every step of the
old flight. They are control-tool evidence, not return qualification or AI calls.

The final candidate activates the new allocator only while the retained-return
policy is active, preserving the nominal controller after all payloads release.
Fresh launch-derived runs give:

| Retained payloads | Contact speed | Tilt | Remaining fuel | Declared entry gate |
|---|---:|---:|---:|---|
| 25 | 3.34 m/s | 0.75 degrees | 45.93 t | Pass |
| 26 | 3.28 m/s | 0.72 degrees | 45.37 t | Pass |
| 0 | 4.55 m/s | 10.10 degrees | 74.21 t | Fail: tilt |

The frozen gate requires speed at most 5 m/s, tilt 5 degrees, body rate
0.02 rad/s and reserve 28 t. The zero-retained metrics equal the original
nominal trajectory, but that trajectory does not meet the pose condition.
The initial sweep stopped; counts 1–24 were not executed in that batch. An earlier global application
to the zero-retained case impacted at 8.46 m/s and was rejected.

After independent review, the active retained-payload domain was evaluated
separately, keeping the same four thresholds and the zero-retained failure.
Counts 1–14, 16, 25 and 26 pass. Count 15 contacts at 4.82 m/s but retains only
13.12 t, failing the 28 t reserve. The sweep stops there; counts 17–24 remain
unexecuted. Full 0–26 qualification remains incomplete.

The saved 15-retained record starts terminal guidance around 12.90 km, with a
fuel estimate of 99.92 t against 96.84 t onboard. The terminal segment consumes
83.72 t over 136.84 seconds. This explains the reserve failure's immediate
budget context; the underlying attitude/control cause remains unresolved.

Record checks pass for the final three runs. The first full 25/26 trials initially
failed verification because their CLI passed Python tuples to strict JSON
checkers. Reopening their persisted JSON passes both checkers without new
dynamics. The original failed verdicts remain recorded; the caller now serializes
before checking. [Source-bound summary](../assets/starship-return-allocation-20261007/summary.json).

The reviewed scope names `retained_policy_active_only_v1`. Independent record
checks require an allocation receipt matching the recorded commands in every
eligible sampled control cycle. Source hashes are included in the experiment
study and input records; they identify bytes, not processor execution attestation.

This establishes a promising retained-payload control change, not qualified
recovery across 0–26 payloads. Booster recovery, return-feasibility checks in
MissionOS, structural survival, readiness for reuse and repeated-flight operations
remain open. Numerical checks are tools for AI control; script superiority is
not this work's acceptance criterion.

See the [qualification contract](../agents/starship-mission-control-contract.md).
