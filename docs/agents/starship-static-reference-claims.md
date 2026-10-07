# Static reference checks and execution admission

The fixed-pose screen and force-coupled reference generator are isolated
development diagnostics. They do not enable the mission catalog, integrate an
actual vehicle state, certify a return path, or admit a catch continuation.

## Arithmetic and retained provenance

`verify_fixed_terminal_reference(...)["passed"]` describes the supplied
reference's arithmetic/contract integrity. It is also exposed as
`arithmetic_passed`. Public self-contained fixtures may pass this check without
retained flight provenance. `past_source_anchors_bound` and
`source_bound_arithmetic_passed` remain false in that case.

Use `verify_source_bound_terminal_reference` when retained past anchors are
required: it fails without both the original run and prior checkpoint. Even its
positive result is source-bound arithmetic, not imported-code authentication,
trajectory feasibility, execution permission, or a physical outcome.

The repaired screen exposes the same arithmetic/provenance distinction.
`candidate_plant_call_allowed` is always false in current generation and
verification, including a non-excluded reference. Old diagnostic records may
contain an optimistic value; never use that historical field as authority. The
current checker rejects a supplied positive value. A separately recorded,
bounded development invocation may be authorized independently of this screen.

## Quantities the repaired screen does not independently establish

The nominal force, fuel and axis-rate fields are held-fin, frozen-COM prediction
labels. The checker verifies their copied labels, shapes and some aggregate
relationships. It does not replay their nominal aerodynamic evaluations or
axis derivatives: `nominal_force_and_fuel_independently_replayed` and
`nominal_rate_derivatives_independently_replayed` remain false. Do not present
these values as independently validated fuel or attitude authority.

The positive hull enclosing-sphere check is conservative. If it cannot prove
nonintersection, or a producer reports a negative exact-hull endpoint that the
checker cannot reproduce, the result is unverified geometry. It is not a
verified hull exclusion or global vehicle infeasibility. Preserve the original
screen and failed verdict; do not turn this case into a successful certificate.

Candidate 15/16's static load convention is
`nodewise_frozen_mass_com_derivatives_zero`; nominal fuel changes between nodes.
The requested pose is advanced at the declared static node spacing. Its loads
and governed requests are hypothetical and do not reproduce a live controller's
update period or actual moving-COM kinematics. Any future numerical plant trial
must use actual material-pin kinematics, finite actuators, startup/shutdown,
torque, fuel and the unchanged simultaneous arrival/support limits.

## Required future connection

A physical driver in this project means a numerical six-DOF driver, not hardware.
Connecting it requires a separately approved fixed entrypoint, source-bound
retained input, actual integrations, both applicable return-site qualifications,
current Rules, and later physical-model observations. No static `passed` flag,
self-hashed directive or caller path assertion provides those facts. Keep
proposal, human approval, Rules, dispatch, model execution and observed outcome
separate. All four completion milestones remain unfinished.
