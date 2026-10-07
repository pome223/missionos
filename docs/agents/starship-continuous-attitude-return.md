# Continuous attitude reference for retained-payload return

The candidate policies keep the analytic terminal-preparation calculation
from [the retained-return policy](starship-retained-payload-return.md) and
separately change the commanded roll reference. `fixed_v1` and
`mass_state_terminal_v1` remain historical comparators. A new approved plan
must bind a new policy identifier and its implementation sources.

The first candidate, `mass_state_terminal_v2`, is **rejected as an operational
replacement**: transporting the frame for the entire entry changed the bank
history and turned the 26-payload baseline's 3.523 m/s contact into a 670.918 m/s
impact. Its implementation and failure evidence remain available. Mathematical
target continuity alone did not establish useful vehicle control.

## Defect and bounded correction

The previous entry-frame construction projected geographic `-north` onto the
plane perpendicular to the desired thrust axis. When that axis crosses
`-north`, the transverse projection vanishes and changes sign. A fallback axis
prevents division by zero but does not provide a continuous roll target. The
landing controller also replaced the entry reference with `+north`, adding an
unnecessary roll request during the terminal thrust-axis change.

The rejected v2 policy chooses the original entry frame once, then parallel-transports
its transverse axes. For consecutive commanded thrust directions, the helper
applies the shortest rotation taking the previous direction onto the new one
to the entire previous target frame. This gives zero additional twist about
that direction. The same transported frame continues into the landing phase.
The legitimate terminal change in thrust direction remains a command change;
it is not smoothed away or described as continuous physical motion.

At an exactly antipodal direction change the shortest rotation is ambiguous.
The helper deterministically rotates around the previous target's transverse
`+X` axis and records `antipodal_fallback=true`. This case is not claimed to be
a smooth command. Quaternion signs are kept consistent for saved diagnostics,
without changing the physical orientation represented by a quaternion.

Only a commanded reference changes. The rigid-body state, PD control, gimbal
and flap limits, physical RCS pairs, fuel, inertia, aerodynamics, deorbit target
and return request time continue through the existing executor. The helper
does not assign body attitude or manufacture torque. It starts only after the
retained-return policy has activated; failure to reach that activation leaves
the legacy return reference unchanged.

This is a generic engineering construction. It is not a recovered SpaceX
attitude-control algorithm or proof of a flight-qualified return envelope.

## Conditioned geographic reference: v3

`mass_state_terminal_v3` instead preserves the original geographic target
exactly while its transverse projection is well conditioned. It bridges only
the problematic region, then reacquires the original geographic target:

1. Normalize the geographic reference, project it perpendicular to the
   requested thrust direction and measure the projection norm.
2. A norm below 0.1 enters a parallel-transport bridge. This threshold limits
   the unguarded projection amplification to ten; it is a predeclared
   conditioning choice, not a fitted altitude or flight coefficient.
3. Once the norm is at least 0.1, compute the signed roll error around the new
   thrust direction and limit roll realignment to `rate * elapsed_time`.
4. The rate is the existing controller's maximum requested angular
   acceleration divided by its attitude frequency: 0.03 / 0.28 =
   0.107142857 rad/s for this profile. This is a generic reference governor,
   not a measured SpaceX constraint or an achieved body-rate guarantee.
5. After reacquisition, resume the exact original target. At the first landing
   target request, force this bounded reacquisition so the `-north` to `+north`
   reference change does not add an instantaneous roll command. An event at
   unchanged physical time has zero roll-realignment time budget.

The underlying physical actuators and controller remain unchanged. This
revision addresses the rejected candidate's loss of geographic trim without
tuning mass, aerodynamic coefficients, gains, or the terminal-preparation
formula against contact outcomes.

## Verification scope

Unit checks drive the desired thrust axis through the old singular reference
and require the new target orientation to change only by the imposed axis
increment. Additional checks cover constant axes, minimal rotation, near and
exact antipodal requests, reversible paths, finite input and no mutation on
rejected input. These checks establish target-frame behavior independently of
whether a simulated vehicle can follow it.

`scripts/study_starship_attitude_return.py` predeclares 13, 26 and 39 attached
payloads for each explicitly selected fixed policy version. Each payload
retains the profile's 1,700 kg mass and finite inertia. The payload count is
the sole profile change; no coefficient, gain, timing or flip-altitude search
is performed. These represent 22.1, 44.2 and 66.3 tonnes in the local mass
model, not demonstrated Starship loading configurations. Both policies use
the same synthetic accepted deployment command without separation, leaving
all payloads attached. The standalone supervisor holds the inhibited sequencer
without any Jev or LLM provider invocation.

Each case records its exact profile, source hashes, full trajectory, policy
events and independent saved-output/terminal-budget checks. The study refuses
to call the evidence frozen if any simulation source changes during execution.
Integrity verification remains separate from contact speed and physical
outcome: a faithfully recorded impact is still a failed return. The reported
maximum target rate uses consecutive saved samples within each phase; it is
not a bound on unrecorded integration steps or commanded phase transitions.

The base trajectory verifier checks contact-point kinematics. The retained
return verifier independently recomputes the unchanged terminal budget and
requires the explicitly expected policy identifier. Neither verifier treats
the target helper's prediction or diagnostic as proof of body tracking,
thermal survival, structural integrity or safe return. Provider performance
and model added value are outside this experiment.

## Runtime invocation

```sh
python scripts/study_starship_attitude_return.py --approve-simulation --workers 3 --policies mass_state_terminal_v3 --output-dir output/starship-attitude-return-v3
```

This is an opt-in numerical robustness study, separate from the MissionOS HTTP
approval path. It writes to a fresh output directory and preserves each failed
or successful case. `physical_execution=false` and
`starship_vehicle_validated=false` remain mandatory.

The first v1/v2 study's harness supplied tuple-containing internal dictionaries
directly to JSON-contract verifiers; both verifiers correctly rejected them.
The saved JSON artifacts were subsequently loaded and independently verified,
with all six passing the integrity checks. The original failed harness
verification remains alongside the corrected saved-JSON verification. The
runner now reloads saved JSON before checking it, and a short real-simulation
test covers that boundary. This correction does not change any trajectory or
convert the physical failures into successful returns.
