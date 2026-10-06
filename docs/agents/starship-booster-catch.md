# Booster catch: operational basis and evidence boundary

This is a local, opt-in six-degree-of-freedom catch experiment. It is not
SpaceX flight software, a reconstruction of a particular flight, or a validated
structural model. A catch approach, arm contact, sustained simulated support,
actual hardware capture, and readiness for reuse are separate facts.

## Primary sources checked on 2026-10-04

The SpaceX pages below were read in a rendered browser because their text-only
responses did not contain the article body.

- [Flight 5](https://www.spacex.com/launches/starship-flight-5): separation,
  boostback, coast and landing burn preceded capture by the tower arms. Many
  vehicle and pad conditions had to be satisfied before attempting capture.
- [Flight 6](https://www.spacex.com/launches/starship-flight-6): automatic checks
  of critical tower hardware aborted the attempt during boostback. The booster
  then executed a preplanned divert, landing burn and offshore splashdown.
- [Flight 7](https://www.spacex.com/launches/starship-flight-7): 12 of 13 planned
  engines restarted for boostback; all 13 restarted for landing, including the
  engine that had not restarted earlier. Engine availability is therefore a
  burn-specific observation, not necessarily a permanent failure flag.
- [SpaceX updates](https://www.spacex.com/updates), Flight 8 account dated
  2025-05-22: the three center engines provided terminal maneuvering after the
  initial landing-burn engine starts.
- [V3 update](https://www.spacex.com/updates#starship-v3), 2026-05-12: V3 has
  three larger, repositioned grid fins with new catch points. Pad 2 has shorter
  arms and electromechanical main actuators. Old-generation four-fin geometry
  must not silently become a V3 engineering specification.

The exact support-point coordinates, arm dimensions and contact properties,
accepted approach errors, controller gains and sensor uncertainties were not
established from those sources. The local two-point support model is explicitly
a surrogate; it is not a claim about the number or layout of V3 catch points.

## Authority and operational behavior

Human approval permits only the configured local simulation. Rules additionally
require current vehicle and tower readiness. Approval does not override a failed
health check. A failed prerequisite must prevent capture eligibility; an early
divert request and an observed safe offshore outcome are distinct records.

The published Flight 6 outcome supports an automatic tower-health abort and
preplanned divert. The current official flight pages contain postflight accounts
and do not establish a numerical manual-GO deadline. Any local approval expiry
or commit time is consequently a declared simulator rule, not a recovered
SpaceX deadline.

Low-level engine, attitude and arm motion belongs to deterministic control.
No LLM proposal can change a rigid-body state, weld the booster to the tower,
set velocities to zero, grant its own catch authority, or certify capture.

## Mechanical assumptions that must remain exposed

| Input | Status |
|---|---|
| Support-point locations and two support surfaces | Local geometric surrogate |
| Arm position, travel limit, speed and health | Configured local actuator model |
| Normal stiffness/damping, tangential damping and friction | Unidentified contact model |
| Stroke, load, support-point speed and body-rate bounds | Local acceptance bounds; not material ratings |
| Terminal tilt and position/velocity control settings | Generic guidance; not SpaceX gains |
| Consecutive support duration and engine-thrust threshold | Local settling criterion |
| Readiness sampling and freshness deadline | Local operational contract |

The free body must be integrated through contact under finite forces and
moments. Each point's world position is `r + R(p - com)` and its inertial velocity
is `v + R(omega cross (p - com) - com_rate)`. Relative velocity also subtracts
the rotating tower and arm motion. Omitting `com_rate` or the angular term can
turn a harmful point impact into an apparently gentle center-of-mass arrival.

A support point becomes eligible for top contact only while it is above the
support plane and inside the corresponding arm footprint. Leaving the footprint
removes that eligibility. Closing an arm after the point has already passed
below the plane must not create a catch from underneath. Normal loads are
unilateral spring/damper forces; tangential damping is bounded by configured
Coulomb friction. An excessive calculated load is retained as a failure, not
clipped to a success threshold.

The terminal fixtures start at simulation time 600 s. The nominal fixture
starts three metres above the support plane with a 1 m/s descent and 30 tonnes
of propellant. Its minimum-prefix generic engine allocator initializes two
engines at the locally calculated hover thrust. This is not the published
three-center-engine SpaceX terminal sequence or an observed approach state. The lateral-offset case
adds 12 m north; the fast-descent case starts at -15 m/s. These are deliberately
declared local fault injections, not observations of an actual flight. Arm
states and contact frames are saved at steps of at most 10 ms. The local
readiness observation remains valid for at most 50 ms of simulation time.
The producer rejects requests exceeding 99,999 integration steps or whose step
cannot advance the floating-point simulation clock. This also applies to direct
API calls; the CLI checks its initialized-clock budget before creating output.

## Evidence and claim boundaries

The independent verifier accepts honestly recorded failure as valid evidence.
It separately recomputes support-point kinematics, support footprints, contact
loads and moments, readiness, consecutive settling, and basic state continuity.
It does not import the producer's contact implementation or rerun its integrator.
The terminal campaign has its own state, initial/final binding and ground-contact
checks. Full mission trajectory validation remains a separate boundary.

`passed` means that the saved catch evidence is internally consistent with the
approved configuration. `simulated_catch_supported` additionally means sustained
support under this configured surrogate was observed. `catch_verified`,
`physical_execution`, and `mission_completed` remain false: neither field
certifies hardware, SpaceX fidelity, launch-connected recovery, or reuse.

A terminal-initialized campaign exercises contact mechanics from a declared
near-tower state. It is not an ascent, boostback or return demonstration. A full
return attempt inherits the actual separation state; if it misses the tower,
its negative result must remain visible alongside any successful terminal case.
Do not replace it with the initialized trajectory or aggregate their claims.

Unmodeled structure, thermal damage, plume interaction, flexible-body response,
mechanical failure, sensor faults outside the declared scenarios, post-catch
safing and refurbishment remain outside the verified claim.

## Verification entry point

`verify_catch(run, profile, catch_config)` consumes the persisted JSON case and
the separately bound vehicle and catch configurations. It rejects nonfinite or
cyclic data, configuration changes, stale readiness, inconsistent Rules,
unsupported claims, incorrect contact geometry/loads, fabricated settling,
state resets, and contradictory sample/event/terminal bindings. Its finite-load
continuity bounds are necessary checks, not a full independent force-integrator
replay or proof of numerical stability.

The contract suite includes a hand-derived rotating support point with a moving
centroid, a real producer-to-verifier terminal invocation, four honest negative
cases, and modified-evidence rejection tests:

```sh
PYTHONPATH=. python -m pytest tests/contract/test_starship_booster_catch_verifier.py -q
```

The initial implementation passed 60 tests. Runtime verification of the
MissionOS approval/worker/artifact boundary is additional evidence and must be
reported separately from these mechanics contracts.
