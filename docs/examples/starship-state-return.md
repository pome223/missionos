# A return tool MissionOS can use

MissionOS controls deployment and selects permitted actions inside a scope
approved before flight. A separate simulator executes the commands, and independent
checks compare them with later observations. The shared Ship return tool handles
retained inventories from 0 to 26 in a finite set of modeled flights.

The controller prepares entry bank and flap trim from the current mass, geometry
and flow. Roll, flap motion and thrust remain finite and are integrated in six
degrees of freedom. The physical coefficients and original contact limits remain
unchanged: 5 m/s, 5 degrees tilt, 0.02 rad/s rotation and 28 t reserve.

![Inventory and selected fuel-probe results](../assets/starship-state-return-qualification/qualification-census.png)

The current rerun passes 39/39 cases: 27 inventories and 12 selected initial-fuel
perturbations. Maximum contact speed is 4.691 m/s, maximum tilt 2.755 degrees,
and minimum remaining fuel 45.231 t, matching the previous census.
It does not qualify a wide range of orbits. Each execution must match the recorded
coast trajectory for its retained inventory, including time, position, velocity,
attitude and fuel. Both AI choices and fallbacks receive this check, again before
deorbit. Small matching tolerances account for sampled data; they are not evidence
of robustness to physical disturbances.

The current fixture evaluation covers all five conditions; normal flight runs
through the real Gateway approval and worker boundary. Record checks pass for all
five, with the following Ship outcomes:

| Condition | Released | Ship result |
|---|---:|---|
| Normal | 26 | 4.608 m/s contact, all four modeled contact limits met |
| Release fault | 0 | 3.473 m/s contact with retained inventory |
| Fuel shortage | 0 | Return prohibited; 30 s observation; **return unresolved** |
| Tower unavailable | 26 | 4.608 m/s contact |
| Operations notice | 1 | 3.514 m/s contact with 25 bodies retained |

All five now declare the same separate booster water-entry goal and meet its
modeled conditions. These nominal results do not qualify booster robustness.

A follow-up checks whether a permitted hold causes return admission to fail.
The two existing fixtures resume after 5 s, before the next release slot, so
neither changes release timing. A delayed-response probe actually holds for
25.25 s and delays the remaining 25 releases by 10.75 s. It releases all 26,
passes both return checks and contacts at 4.60835 m/s. The largest checked
position difference is 1.921 m (limit 12 m); at deorbit it is 0.174 m.
The existing tolerances and guidance remain unchanged. These three cases do
not cover every hold duration or response history. The 30 s bound is an expiry
that stops deployment, not permission to resume at the deadline.

[Hold/resume evidence](../assets/starship-state-return-qualification/hold-resume-summary.json)
records the actual timing, matching residuals and later observations. These
forced holds are tested separately from the ordinary normal-flight comparison,
which requires no hold; its false result is retained.

A passed record or comparison check is not a successful whole mission. After
entry is committed, a terminal fuel or propulsion violation is logged and the
existing controller continues as unqualified best effort. The final outcome is
kept even if it is an impact. The late engine-loss probe records a two-engine
terminal phase and a 22.735 m/s impact. It verifies failure preservation, not
a successful recovery.

The previous evaluation at `6185ef92` contains 19 actual Jev inference receipts
and no DeepSeek calls. Jev chose additional observations and deployment changes.
Its four available returns kept the initial return policy; the fuel-shortage
case had only inhibition available. These runs do not show an AI-induced change
of return policy. Their booster goals also differed and must not be combined
into a single recovery-success count. Script parity is not an AI-value gate.

Managed planning checks the qualified source and numerical environment before
approval. Use `pip install -e '.[spaceflight-qualified]'` for the recorded backend.
A changed covered source requires requalification. Model contact is separate from
thermal protection, structural survival, water survival, reuse and SpaceX fidelity.
Booster robustness and high-cadence fleet workload remain future work.

[Current qualification](../assets/starship-state-return-qualification/qualification.json)
· [Review corrections and current checks](../assets/starship-state-return-qualification/review-fixes-summary.json)
· [Historical Jev decisions](https://github.com/pome223/missionos/blob/6185ef92/docs/assets/starship-state-return-qualification/steps345-summary.json)
· [Implementation contract](../agents/starship-mission-control-contract.md)
