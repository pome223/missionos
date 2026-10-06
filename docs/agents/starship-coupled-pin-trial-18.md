# Historical coupled-pin trial 18: negative result and limits

This is a sanitized historical summary, not a publicly bundled raw execution
receipt or a command to rerun the campaign. Private saved contexts, raw step
corpora, approval stores, source snapshots, and ignored local runners are not
included. The reviewed [public media](../assets/starship-mission-20261006/index.html)
is derived display evidence; the [public quickstart](../examples/starship-mission-showcase.md)
uses a different, initialized gimbal fixture.

## Observed numerical outcome

The fixed development policy continued a launch-derived booster context for
60 simulated seconds and 600 macrosteps. It inherited actual physical state,
conditioned frame/tracker, fin latch, and causal actuator command. Position,
velocity, attitude, rate, and fuel were not assigned from a reference curve.
After the fixed translation horizon, it used the original target and existing
coordinated HOLD feedback, without resetting controller history.

Termination was duration exhaustion, not contact or catch. Hull clearance
remained 43.3923 m. No saved same-time sample met all eight arrival gates. The
best recorded sample met three. Final observed values were:

| Condition | Final value | Unchanged limit |
| --- | --- | --- |
| Pin-center horizontal position | E -219.915 m, N -79.535 m | 0.2 m radius |
| Both pin clearances | 4.16655 m, 2.76031 m | 3.0–3.8 m |
| Each pin horizontal speed | approximately 12 m/s | 0.4 m/s |
| Each pin vertical speed | -0.24655, +0.18249 m/s | -3.0 to -0.5 m/s |
| Tilt | 6.95393 degrees | 0.81969 degrees |
| Body-X/East angle | 6.73265 degrees | 0.81969 degrees |
| Body rate | 0.0383747 rad/s | 0.01 rad/s |
| Fuel | 3,568.607 kg | at least 6,917.870 kg |

The first saved reserve deficit was at elapsed 55.6 s. Final fuel was also
below the approximately 6.82 t dry-mass reserve lower bound. Fuel cannot
increase under this plant, so this suffix cannot qualify through duration
extension; no catch was invoked. This is not global Starship infeasibility.

## Causal and verification scope

Actual per-step fuel deltas by recorded navigation mode were 45.769667 t over
418 tracking steps and 14.648662 t over 182 HOLD steps. Separately binned
first/last-sample intervals have different boundary accounting and must not
be pooled. Finite-wrench allocation was deferred on 313/600 steps, including
every first-ten-second step. Requested/predicted wrench residuals are not
measured achieved force. Near-position passage still had substantial speed.
The commanded masks used the balanced pair (3,8) most often; no all-13 fallback
was used. Final Body-X/East error mainly reflected tilt, not proven roll
instability or global uncontrollability.

A historical independent stored check passed known-policy arithmetic/source
binding for 600 commands and 600 recorded transitions. Earlier rounding/input-
replay rejections remained separate; no tolerance/gain/gate change or new
physical integration repaired the record. That check did not independently
replay RK stages, nonlinear contact/hull integration, or the fin aerodynamic
Jacobian. Later source fixes have public fixtures and do not reissue that
historical PASS under a new hash or create physical/adoption authority.

## Public source and future work

The current source contains isolated development guidance, finite command
receipts, pure record checkers, and bounded evidence storage. Unknown byte
accounting closes new writes/queries until explicit read-only reconciliation;
partial evidence is retained and primary exceptions are preserved. These are
software contracts, not return qualification. Ignored historical/future runner
variants are not shipped or covered by public CI.

The [completion contract](starship-completion-campaign.md) requires a different
implemented joint finite-state guidance route, exact reintegration, actual
simultaneous eight-gate arrival plus reserve, and separately declared exact-
state catch. All four milestones remain unfinished. Physical execution,
SpaceX engineering fidelity, Starlink service, and LLM advantage are unclaimed.
