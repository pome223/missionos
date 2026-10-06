# Saved-state diagnosis of missed capture — 2026-10-05

The fifteen existing records (five scheduled boostback cutoffs, three allocation
methods) all still miss handoff and support. This change diagnoses them without
another recovery integration or provider call. Vehicle, aerodynamic, controller,
fuel and catch coefficients are unchanged. The diagnostic is not a flight-time
MissionOS decision, a feasibility certificate, a new controller or adoption.

The input sets are the original conventional allocation and the subsequent
coast-plus-landing/coast-only allocation pair. All originate at the same saved
separation. Only the two latter methods have exactly equal full landing-request
states. The conventional method reaches a different landing state in each case;
aligning its landing request to elapsed zero does not create a same-start causal
experiment. Inputs, code snapshots and failed terminal records remain retained.

## What the records establish

1. At T+191.2 cutoff, both candidate paths request landing at T+478.95, with
   **156.24° tilt** and 66.08 t of fuel. Their ideal-alignment preview reports
   stopping in 4.82 s, with 14.05 t beyond its mass-adjusted capture reserve.
   The actual paths contact the surface in 3.78 / 3.76 s, leaving
   **32.52 / 32.71 t** and travelling at 567.59 / 574.55 m/s. During landing,
   main thrust points partly downward. At the saved T+480.85 state (1.90 s after
   the request), its tower-up contribution is -54.14 / -54.41 m/s², excluding
   aero, RCS and gravity. This is not a fuel-exhaustion-only case.
   The ideal stopping point is still approximately **4.523 km** from the tower
   in the frozen CG-frame estimate; the +14.05 t is not fuel for that transfer.
2. At T+186.2 cutoff, the candidates share the T+462.95 request state,
   52.37° tilt, 3.601 km support-midpoint horizontal error and 70.78 t fuel.
   Even the ideal stopping preview leaves **0.533 t less** than the support
   reserve evaluated at the predicted remaining mass. `burn_fuel_feasible=true`
   means the approximate preview arrests speed, not that capture fuel is
   preserved. Actual saved reserve failures first occur at T+485.15 / 483.15;
   both paths subsequently exhaust fuel. They contact at 33.16 / 41.49 m/s.
   The corresponding ideal stopping point is approximately **1.879 km** away.
3. At T+181.2 cutoff, conventional landing starts at T+457.85 with 62.61°
   tilt; the candidates start at T+458.45 with 64.56°. Conventional allocation
   downstream of the candidate coast records almost horizontal main thrust at
   T+462.15, with a slightly negative up component (-1.563 m/s², -1.426% of
   the scalar thrust sum). This differs from the substantially downward thrust
   in the T+191.2 case. Its terminal speed is 439.95 m/s, versus 43.46 m/s for the original
   conventional coast and landing. Full initial states differ, so the difference
   does not isolate tilt, time, fin allocation or another single cause.
4. No saved landing state in any of the fifteen records satisfies all eight
   handoff bounds together. No saved landing state meets the horizontal-position
   bound. These are sampled findings, not a proof of continuous-time inability
   or that any possible guidance must fail.

The stopping preview itself assumes ideal thrust opposed to the velocity, with
frozen measured drag area and aggregate finite spool. Actual attitude, finite
gimbal/torque response and support geometry are resolved only in execution.
Thus its positive speed-arrest flag cannot certify an executable capture path.
This is a concrete model/contract mismatch to investigate, not proof that
changing only that preview will yield capture.

Public 6DOF powered-descent work explicitly treats changing mass properties and
attitude dynamics together: [NASA SCvx with time-varying mass properties](https://ntrs.nasa.gov/citations/20230017074),
checked 2026-10-05. That paper concerns a notional lunar lander, not identified
Super Heavy coefficients or unpublished SpaceX control. The existing generic
guidance basis is in [the launch-connected contract](starship-launch-connected-catch.md).

## Diagnostic semantics

`starship_capture_diagnostics.py` calls the existing independent recovery
checker's material-point reconstruction. It does not call the producer's
`_tower_observation`, guidance, control allocation or integrator. This reuse is
not a second independent checker of that verifier's geometry.

Eight signed margins, in their own units, are calculated at each single state:
support-midpoint horizontal position; both pin heights; both pin vertical
velocities; both pin horizontal speeds; body-Z tilt; body-X east alignment;
body-rate magnitude; propellant minus mass-dependent capture reserve. Positive
or zero is inside the bound. No weighted score combines unlike units, and no
individual best margins from different times are combined into capture.

The main-engine force projection independently uses each persisted throttle,
availability, BODY `Ry(gimbal_y) Rx(gimbal_x)` direction and actual quaternion.
An empty reservoir produces zero thrust despite a residual spool state. Its
tower-up force divided by current mass excludes RCS, aero, gravity and rotating
frame terms; it is a main-thrust contribution, not net vertical deceleration.

Exact stage-request event states are merged with landing checkpoints and the
terminal state. A repeated timestamp must carry the same physical state. The
event phase label describes the request, not measured engine activation. The
requested engine count and actual spool/force are separate fields. Checkpoint
indices and exact event markers preserve the origin of every displayed point.
Gate failure times are first **saved** failures with the previous saved time,
where present. A trajectory is not interpolated into the 0.8 m height band.
The replay retains every diagnostic point; it stops each run at its own terminal
time and compares positions on a common scale. CG speed is never pin speed.

An incomplete preview's consumed fuel is not required stopping fuel. Its ideal
post-stop fuel/reserve/surplus fields remain null. For a completed preview,
reserve is recalculated using dry mass plus predicted remaining fuel, rather
than reusing the heavier landing-entry state's reserve.

The fuel surplus concerns **speed arrest alone**, minus a support reserve at
that hypothetical remaining mass. It excludes transfer to the tower and the
longer profile of the executed landing law (`clear/6` descent target plus
horizontal correction). The ideal preview consumption is not the executed
law's required fuel and is not a proved lower bound: frozen drag, attitude and
different guidance prevent that inference. No gravitational-loss amount or
single failure cause is inferred from the different elapsed times.

The approximate stop location is a CG endpoint, not a pin endpoint. The
preview's displacement is in the CG's initial local ENU, so its east/north and
vertical displacement are mapped through the WGS84 CG axes into request-time
tower ENU. It is not added directly to the pin midpoint. Frame rotation and
curvature during the preview and future attitude/centroid motion are omitted;
the approximate endpoint cannot pass a handoff gate or certify feasibility.
