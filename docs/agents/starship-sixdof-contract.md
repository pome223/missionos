# Starship 6DOF development contract

This backend integrates translation and rotation together. It replaces neither
the frozen Flight 14 comparison nor its negative results. It is a separate,
opt-in development simulator. The old chat/3DOF route does not silently become
this backend. No new LLM/Jev model calls or authority are introduced here.

## State and force boundary

`State6DOF` contains ECI position and velocity at the instantaneous retained CoM,
scalar-first Hamilton body-to-ECI quaternion, body angular velocity, propellant,
and achieved engine/flap actuator states. Body +Z is longitudinal. Geometry has
a fixed body datum at the vehicle base. Earth-fixed axes coincide with ECI at
mission time zero; there is no astronomical epoch/IAU frame transformation.

RK4 integrates position, velocity, quaternion, angular velocity, propellant and
actuators. Gimbals rotate actual engine thrust vectors. Off-center engine and
panel forces produce moments about the current CoM. Full inertia tensors enter
Euler's rotational equation; no controller sets `q` or `omega` to its target.
Central gravity-gradient torque is evaluated at every RK stage. Translational
gravity includes J2; J2 tidal torque is not implemented.

The instantaneous-mass-property approximation omits depletion-rate loads,
internal transport momentum and nozzle jet damping. The tank has a fixed
occupied cylindrical shape with decreasing density, not a moving free surface.
This is not a generally momentum-closed open-system rocket model. Composition
and instantaneous separation checks do not validate these omitted burn terms.

Engine throttle/gimbal and control-surface lag and rate limits apply before
forces are produced. Generic RCS pairs have positions, directions, finite thrust
and Isp; they consume the same explicitly assumed reservoir. They do not apply
an ideal external control torque. Validation-only external wrench arguments
default to zero and are not used by mission guidance.

## Vehicle and mission assumptions

The [profile](../../examples/spaceflight/starship-sixdof-profile.json) freezes
the executable assumptions. Public capacities are used as assumed loading;
they are not recovered Flight 14 tank measurements. See the
[primary-source inventory](starship-sixdof-research.md) for source and confidence
boundaries. The experiment does not fit those values to the nine entry points.

Hull and flap/grid-fin forces use bidirectional plates with local velocity
including `omega × lever`. Three booster fins and four ship flaps are present,
but they are not a Mach/Reynolds-dependent aerodynamic database. Numerical
flap effectiveness is computed at the current flow, then constrained by the
actuators. A large mesh would not cure this model limitation.

Generic PD attitude control and bounded allocation drive all six degrees of
freedom. The launch starts at pad release with main engines already spooled;
hold-down mechanisms, combustion ignition and hot-staging plume overlap are
unimplemented. Phase decisions use sampled truth, not a simulated navigation
filter. Controller period and integration settings are recorded.

The continuous launch harness attempts stack ascent, rigid separation, orbit,
payload release, deorbit and hull contact. The booster continues from its saved
separation state with inherited engine failures. Its return guidance attempts
boostback, atmospheric coast and a final burn; it does not invent an additional
Falcon 9-style entry burn or report a tower catch.

The default boostback request is 33 engines, following the planned V3 count in
the [Flight 14 report](https://www.spacex.com/launches/starship-flight-14).
Three-center-engine powered slew is a generic development assumption informed
by [earlier SpaceX hot-staging descriptions](https://www.spacex.com/updates/reusability),
not a recovered V3 program. Development landing guidance selects one to three
engines by required thrust. It does not reproduce Flight 14's reported
13-to-five-to-three sequence or inject its reported failures.

Each satellite has finite mass and inertia. Composition must match before a
release. Equal/opposite impulses at a shared application point update parent
and child CoM velocities and angular velocities. Linear and angular momentum
are checked from the resulting states. Satellite mass properties are collected
at a common location in the mother ship: payload packing, mechanism geometry
and collision-free extraction are **not** validated. Each released body then
propagates its own position and attitude. This is not Starlink commissioning,
orbit raising, link acquisition, or service verification.

The atmospheric perturbation cases start at separately declared initial states.
They are never spliced into the continuous launch to manufacture a successful
return. Engine-out and actuator perturbations are explicit synthetic faults.

## Contact and terminal outcomes

The contact solver minimizes the WGS84 ellipsoid quadratic over a closed
cylindrical hull envelope. It includes cap and side contact, not only the base
or CoM. Its signed level-set distance is not geodetic altitude. The first
endpoint-bracketed crossing is bisected without changing velocity, attitude
or angular velocity. It is not a complete swept-volume detector between endpoints.

Contact-point velocity includes rotation, Earth surface rotation, and moving
CoM correction. Appendages outside the cylinder, water impact, buoyancy,
structural load limits, capture hardware and post-contact survival are absent.
Low-speed contact alone is not a landing/catch/reuse verification. Time limits,
loss of attitude envelope and impacts remain explicit outcomes.

## Numerical evidence

The separate validator uses analytic axis-torque cases, free-body invariants,
fuel depletion/rocket equation, finite-difference mass properties, timestep
convergence and independent references:

- NASA NESC 2015 corrected atmospheric case 02: the three inertial-relative body
  angular-rate channels, 301 public samples. The reference CSV, source URL and
  upstream and extracted checksums are fixed. Translation/environment and the
  rest of NASA's cases are not thereby verified.
- Native Basilisk 2.12.0: a non-diagonal fixed inertia and an applied body force
  plus torque. It integrates one continuous initial-value problem with RKF78;
  reference samples do not reset the local solver. Shared inputs, tolerances and
  both traces are retained. This checks fixed-mass dynamics, not the Starship
  aerodynamics or mass-flux approximation.

Native Basilisk requires a separately prepared Python environment with its
official package installed. It is optional and is not supplied or invoked by
an ordinary fixture test. Omitting the native flag must remain `NOT RUN` for
that independent comparison.

```sh
python -m pip install -e '.[spaceflight-native]'
python scripts/check_starship_sixdof.py --approve-simulation \
  --native-basilisk --output-dir output/sixdof-validation-new
python scripts/run_starship_sixdof.py --approve-simulation --scenario all \
  --validation output/sixdof-validation-new/validation.json \
  --output-dir output/sixdof-missions-new
```

The native checker needs a Python environment with Basilisk installed. The
mission runner needs the repository dependencies, including NumPy. Omitting `--validation` does
not imply an independent pass. Source hashes must match the supplied validation.
Outputs use fresh directories; failed development runs must not be overwritten.

The default public fixture uses only `.[spaceflight]` (CPU NumPy/SciPy).
Native Basilisk is `NOT RUN` unless separately installed and explicitly invoked
with `--native-basilisk`. Public CI does not select the native extra.

`study.json` contains full mission evidence, `validation.json` the optional
independent comparison, `report.html` the selected replay fields and boundaries,
and `manifest.json` file hashes. Source hashes and resolved inputs identify the
run. Runtime completion of the CLI means the simulation was recorded, not that
its mission or a real spacecraft succeeded.

## Display and operational boundary

The 3D replay derives all three body axes from recorded quaternions. It does not
derive attitude from trajectory tangent or prescribe a bank for the physics.
CoM placement, achieved actuator fields and event boundaries come from saved
states. Camera, plume, materials and detailed shape are illustrative.

`six_dof_integrated=true` is a dynamics statement. It must remain distinct from
numerical comparison success, complete mission success, SpaceX vehicle
validation, physical execution, satellite service and LLM/Jev added value.
No blanket claim that all publicly known physics is already reproduced is
supported. Thermal/TPS, upper atmosphere/weather, structural dynamics, slosh,
mass-flux rotational coupling and identified aero remain explicit work items.
