# A coupled six-degree-of-freedom spacecraft model

This development simulator integrates translation and rotation together. Engine
locations and achieved gimbal angles, finite control surfaces, and finite RCS
forces determine motion. Fuel changes instantaneous mass, center of mass, and
inertia. A target attitude is a controller request; it is never assigned to the
integrated quaternion or angular velocity.

The continuous harness attempts launch, rigid stage separation, orbit, finite
payload release, and return. Released bodies propagate separately. The 3D replay
uses recorded quaternions and actuator states. Separately initialized actuator
or near-tower catch tests are labelled as separate experiments.

Start with the [MissionOS showcase and bounded public fixture](starship-mission-showcase.md).
The normal chat and browser console separate plan, approval, execution, and
verification. `verified` means record integrity; failed return or contact
objectives remain visible.

Launch-derived tower catch has not been completed. A near-tower initialized
arm-support test cannot substitute for arriving from launch. Low-speed hull
contact also does not verify landing, structure/TPS survival, or reuse.

The model uses generic development guidance and plate aerodynamics. It omits
identified SpaceX aero, structural flexibility, slosh, thermal/TPS dynamics,
and general open-system rotational mass-flux effects. Numerical analytic,
NASA rotation-rate, or optional native Basilisk comparisons cover declared
subproblems, not complete vehicle fidelity or Starlink service.

See the [state/force/verification contract](../agents/starship-sixdof-contract.md),
[primary-source inventory](../agents/starship-sixdof-research.md), and
[launch-connected catch boundary](../agents/starship-launch-connected-catch.md).
The older [3DOF reference model](starship-3d-simulation.md) remains separate.
