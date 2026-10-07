# Retained-payload return planning

This is a local engineering-policy revision for the continuously integrated
six-DOF simulator. It addresses return preparation after deployment is skipped
and the payload remains attached. It is not a published SpaceX contingency
procedure, a validated Starship landing envelope, or evidence of LLM/Jev value.

## Primary-source basis

The sources were checked on 2026-10-04. Page references below are printed page
numbers, rather than a PDF viewer's page index.

| Source | Supported observation | Boundary |
| --- | --- | --- |
| NASA, [Aeronautics and Space Report of the President: 1985 Activities](https://www.nasa.gov/wp-content/uploads/2024/01/presrep1985.pdf), Appendix A-3, p. 117, STS-51B entry | GLOMR failed to deploy and returned to Earth with the orbiter. | A real retained-payload return; it supplies no Starship mass, control, or landing limits. |
| NASA, [Historical Data Book, Volume VII](https://www.nasa.gov/wp-content/uploads/2023/04/sp-4012v7.pdf), p. 248, STS-46 | A jammed tether prevented the planned extension; after unsuccessful recovery attempts, the satellite was restowed for return. | Mechanism and stowage conditions matter. This simulator does not reproduce that tether mechanism or its recovery. |
| NASA, [Mission Planning and Operations Team Report](https://www.nasa.gov/history/rogersrep/v2appj.htm), p. J20, section V.A.8 | Backup plans covered late deployment and alternate deorbit opportunities, preserving propellant for subsequent operations and return. Entry-system degradation also had an early-return plan. | Supports explicit contingency plans and resource accounting; these were Shuttle plans, not Starship procedures. |
| Same NASA report, p. J18, Figure 6, and p. J28, section V.C.5 | The planning table distinguishes nominal, no-deploy, and abort weight/center-of-gravity cases; entry-envelope work also addressed weight and center of gravity. | Supports reviewing the retained configuration. Shuttle numerical envelopes must not be applied to Starship. |
| SpaceX, [Flight 9 investigation update](https://www.spacex.com/updates/reusability), heading “Flight 9” | Attitude error automatically skipped deployment; later passivation skipped the in-space burn. The vehicle was lost during off-nominal entry. | Objective skipping is an actual Starship behavior. This event does not demonstrate safe return with retained payload. |

These sources establish the operational problem and the need for a bounded
configuration-specific plan. They do not supply a flight-qualified algorithm
for the local simulator. No full source documents or figures are vendored.

## Baseline diagnosis and limits

The previous [deployment-supervision run](starship-flight-supervision.md)
confirmed the sequencer skip while retaining all payloads. Its saved contact
speed was **238.740805 m/s**, with termination `surface_impact`. The original
record remains failure evidence even if a later policy produces a better result.

Retained payload changes mass, center of mass, and inertia. The model already
propagates attached mass; a fixed terminal flip altitude can still be inadequate
for the changed state and available actuation. This is a candidate explanation,
not proof that payload mass alone caused the impact. Diagnose angular motion,
controller error, engine response, clearance, and remaining propellant together.

There is also a target-frame conditioning issue to check separately. The
attitude constructor projects a reference direction perpendicular to the desired
body axis. If those directions become nearly parallel, their projection becomes
small and the requested roll direction can change rapidly; an alternate-axis
fallback only avoids division by zero. This is a roll-reference problem, not a
quaternion integration singularity. Inspect target continuity before attributing
a large attitude error entirely to insufficient physical control authority.
Any correction to that construction must be distinguished from the return-policy
change, and the historical saved result must remain intact.

## Local policy contract

`fixed_v1` preserves the original fixed-altitude terminal preparation as the
explicit comparator. `mass_state_terminal_v1` is a separately selected,
deterministic engineering policy for terminal flip preparation based on the
current integrated mass and motion state. It does not fit a recorded flight,
change aerodynamics, retime return, or alter deorbit targeting.

The revised policy requires a fresh preflight plan and approval binding the
policy identifier, profile, implementation, and execution bounds. Approval of
the older skip-only procedure does not authorize a silent policy replacement.
Jev still routes the deployment incident; an LLM proposal cannot grant approval,
change the policy constants, or issue arbitrary attitude or engine commands.
The deterministic executor applies the approved policy through finite actuators.

Policy diagnostics must expose the current payload count, mass, center of mass,
inertia, available thrust, attitude and descent state, propellant, and the
preparation criterion actually used. Values derived from the local profile are
engineering assumptions. They are not measured SpaceX operating limits. A
finite estimate of stopping or slew capability is not by itself a landing
guarantee; integrated motion and later contact evidence remain authoritative.

## Verification and reporting

Compare the original and revised policies under the same initial state,
payload configuration, injected failure, vehicle parameters, and integration
settings. Preserve an ordinary released-payload case and a retained-payload case
so that a regression is visible. Record any attitude-reference correction as a
separate change when interpreting the comparison.

Keep policy selection, approval, control activation, observed trajectory, and
contact outcome separate. The independent verifier must check saved evidence;
it must not accept the policy's own prediction as proof of its effect. A forward
rollout using the same dynamics is a planning prediction, not independent
validation of the real vehicle.

Report contact-point speed, orientation/body rate, residual propellant,
payload separation count, termination, and unmet mission objectives. Failed
criteria remain failed even when another candidate is worse. A successful
bounded contact result would establish only this model's behavior under the
stated assumptions; thermal protection, structure, slosh, payload survival,
catch hardware, and a validated return corridor remain outside that claim.

`physical_execution=false` and `starship_vehicle_validated=false` remain
mandatory. A local guidance improvement would not establish LLM/Jev superiority.

## Terminal-preparation estimate

The policy activates at the configured return request when payloads are still
attached. Activation does not require a successful Jev response or skip command:
an inhibited sequencer also retains payload. It changes only the terminal flip
trigger; ascent, coast duration, deorbit target, attitude allocation, finite
actuators, mass, inertia and aerodynamic coefficients remain unchanged.

On each ballistic-return step it uses the current mass, full inertia tensor,
CoM, available central engines, thrust-axis angle to geocentric up, transverse
body rate and co-rotating descent speed. A central-gravity estimate is used for
this scalar diagnostic, while the trajectory continues to use WGS84/J2 physics.
Terminal applicability is explicitly limited to descending, subsonic states.
No available thrust, nonpositive aligned net acceleration, or an empty tank
leaves the policy untriggered and preserves the resulting failure.

The nominal TVC moment is computed from the available engines' thrust at the
configured flip throttle, gimbal limit and axial lever arms. Divide it by the
largest principal inertia and cap it at the configured PD acceleration request
to obtain an estimated slew acceleration `a`. For thrust-axis angle `theta`
and transverse rate `w`, the preparation time is:

```text
lag = 4 * max(throttle_tau, gimbal_tau) + gimbal_limit / gimbal_rate
t = w/a + 2*sqrt((theta + w*w/(2*a))/a) + lag
required_height = descent*t + 0.5*g*t*t
                + (descent + g*t)^2 / (2*(available_thrust/mass - g))
                + ship_length
```

The trigger uses the greater of this estimate and the original fixed flip
altitude. This is an analytic engineering estimate, **not a conservative safety
bound**. Turn-time thrust/drag, aero moments, coupled allocation and achieved
angular acceleration are not captured by the scalar calculation. Full-thrust
equivalent fuel and its margin are diagnostics, not certified reserve gates.
The same 6DOF executor still has to accomplish the turn and braking.

The reference-frame conditioning defect described above is not repaired in
this change. Earlier preparation can avoid entering that region in one flight;
it does not prove that the attitude controller is robust across configurations.
The single 10 km flip-altitude diagnostic was used to test causality, not to
choose the operational trigger. No altitude sweep or vehicle-parameter fitting
was used to select this policy.

The independent return verifier recomputes composition and the preparation
budget, binds activation/trigger snapshots to saved trajectory and events, and
checks the preceding integration-step observation and earlier stored samples.
It does not verify unrecorded steps. The separate trajectory verifier checks
contact-point kinematics; the MissionOS controller checks the signed plan,
profile/source bindings, worker receipt and artifact hashes.
