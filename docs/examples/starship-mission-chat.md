# Run a Starship-inspired simulation from MissionOS

The [public showcase](starship-mission-showcase.md) gives the short fixture
commands, operator-console URL, recorded videos, and claim boundaries. The
fixture planner matches a fixed catalog; it is not LLM inference.

Ask for `Starship sixdof_gimbal_step`, inspect the source/profile-bound plan,
then use `/approve`, `/run`, and `/status`. Approval does not start a worker.
`yes` or `OK` is not approval. A changed plan/source or expired/consumed grant
is rejected. The browser console exposes the same separate operations.

| Catalog choice | Scope |
| --- | --- |
| `sixdof_gimbal_step`, `sixdof_entry_perturbation`, `sixdof_flap_asymmetry` | Separately initialized 30-second numerical actuator/attitude fixtures |
| `sixdof_launch`, `sixdof_engine_out` | Continuous development launch, rigid release, and return, with an explicit synthetic engine fault in the latter |
| `sixdof_deployment_supervised` | Accepted/no-effect release fault, bounded skip proposal, Rules, and later measured sequencer changes |
| `sixdof_observation_supervised` | One additional fresh observation under the original one-use/deadline bounds |
| `sixdof_retained_return_supervised`, `sixdof_continuous_return_supervised` | Explicit development return policies with retained payload mass |
| `sixdof_booster_catch` | Near-tower initialized finite-arm/contact/support fixture, not return from launch |
| `sixdof_launch_catch` | Launch-derived return with exact-state catch continuation only if actual arrival and reserve conditions hold |
| `flight14_inspired` | Older 3DOF reference study, separate from coupled attitude dynamics |

Only implemented fixed choices may run. Inspect the displayed scenario and
limits rather than treating the names as outcome promises. A full flight can
cost substantially more CPU/wall time than the short public fixture.

A credential-free simulation worker returns saved records and a signed receipt.
The Gateway checks source binding and artifact hashes before serving verified
results. That is software integrity under the local same-user trust boundary;
it does not authenticate a human or certify the model. Source-hash checks do
not authenticate code already imported into a process.

Live planner/Jev modes are separate opt-ins. One historical deployment record
used Jev to route to the fixed bounded skip procedure; DeepSeek was not called.
The interlock already inhibited later release, and return still impacted. No
model advantage or complete mission success follows from that connection.

See the [maintainer chat contract](../agents/starship-mission-chat-contract.md)
and [deployment supervision contract](../agents/starship-flight-supervision.md).
