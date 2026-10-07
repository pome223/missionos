"""Independent references for the six-DOF kernel, not Starship validation.

Analytic laws, NASA's published torque-free rates and an opt-in native Basilisk
rigid hub are separate references. Kernel step-size convergence is a numerical
diagnostic, never an independent reference or a NASA certification.
"""
from __future__ import annotations

import csv
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
import json
import math
from pathlib import Path
import platform

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "examples/starship-sixdof-validation"
NASA_CSV_SHA256 = "e56d647786fa10588ec85c336f3b46ad8dae2008b35a413a8d0713a3e1155320"
NASA_UPSTREAM_SHA256 = "deb423c19bcdd1b99fdf6c0d2bbd1b6c1db5b68410a8e1dbe7a6cedb1b724f79"
BSK_VERSION = "2.12.0"
LIMITS = {
    "analytic_rate_rad_s": 1e-10, "analytic_attitude_chord": 1e-7,
    "free_rotation_energy_relative": 1e-7, "free_rotation_momentum_relative": 1e-7,
    "nasa_rotation_rate_rad_s": 1e-6,
    "basilisk_position_m": 1e-4, "basilisk_velocity_mps": 1e-6,
    "basilisk_rate_rad_s": 1e-7, "basilisk_attitude_chord": 1e-7,
    "mass_closure_kg": 1e-8, "rocket_equation_mps": 1e-6,
    "convergence_minimum_ratio": 8.0,
}


def _norm(v):
    return math.sqrt(math.fsum(float(x) ** 2 for x in v))


def _difference(a, b):
    if len(a) != len(b) or not all(type(x) in (int, float) and math.isfinite(x) for x in [*a, *b]):
        raise ValueError("comparison_vector_invalid")
    return _norm([float(x)-float(y) for x, y in zip(a, b)])


def quaternion_distance(a, b):
    """Sign-invariant Euclidean quaternion chord; not an Euler-angle error."""
    if len(a) != 4 or len(b) != 4 or not all(math.isfinite(float(x)) for x in [*a, *b]):
        raise ValueError("invalid_quaternion_comparison")
    if not all(abs(_norm(q)-1) < 1e-6 for q in (a, b)):
        raise ValueError("unnormalized_quaternion_comparison")
    return min(_difference(a, b), _difference(a, [-float(x) for x in b]))


def _rotate(q, vector):
    # Independent active Hamilton DCM; no production rotation helper imported.
    w, x, y, z = map(float, q)
    matrix = [[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
              [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
              [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]]
    return [math.fsum(a*b for a, b in zip(row, vector)) for row in matrix]


def _momentum(inertia, omega):
    return [math.fsum(float(a)*float(b) for a, b in zip(row, omega)) for row in inertia]


def _record(state):
    return {"time_s": float(state.time_s), "r_eci_m": list(map(float, state.r_eci_m)),
            "v_eci_mps": list(map(float, state.v_eci_mps)),
            "q_body_to_eci": list(map(float, state.q_body_to_eci)),
            "omega_body_rad_s": list(map(float, state.omega_body_rad_s)),
            "propellant_kg": float(state.propellant_kg)}


def _fixed_vehicle(inertia, mass=1000.0):
    from .starship_sixdof import Vehicle6DOF
    return Vehicle6DOF(dry_mass_kg=mass, dry_com_body_m=(0., 0., 0.),
                      dry_inertia_kg_m2=tuple(tuple(row) for row in inertia),
                      propellant_capacity_kg=1., tank_center_body_m=(0., 0., 0.),
                      tank_radius_m=1., tank_length_m=1., engines=(), aero_panels=())


def _initial(omega=(0., 0., 0.), quaternion=(1., 0., 0., 0.), velocity=(0., 0., 0.)):
    from .starship_sixdof import State6DOF
    return State6DOF(time_s=0., r_eci_m=(7_000_000., 0., 0.), v_eci_mps=velocity,
                     q_body_to_eci=quaternion, omega_body_rad_s=omega,
                     propellant_kg=0., engine_states=())


def _propagate(initial, vehicle, duration, dt, force=(0., 0., 0.), torque=(0., 0., 0.)):
    from .starship_sixdof import Command6DOF, step
    count = round(duration/dt)
    if not math.isclose(count*dt, duration, abs_tol=1e-10):
        raise ValueError("validation_grid_invalid")
    state, trace = initial, [_record(initial)]
    command = Command6DOF(engines=())
    for _ in range(count):
        state = step(state, vehicle, command, dt, gravity=False, j2=False, atmosphere=False,
                     external_force_body_n=force, external_torque_body_nm=torque)
        trace.append(_record(state))
    return trace


def native_basilisk(initial, inertia, mass, sample_times, *, force=(0., 0., 0.),
                    torque=(0., 0., 0.), approve_simulation=False):
    """Run an independent compiled rigid hub with fixed mass and body loads."""
    if approve_simulation is not True:
        raise PermissionError("native_basilisk_requires_simulation_opt_in")
    if version("bsk") != BSK_VERSION:
        raise ValueError("basilisk_version_mismatch")
    from Basilisk.simulation import spacecraft, svIntegrators, extForceTorque
    from Basilisk.utilities import RigidBodyKinematics
    craft = spacecraft.Spacecraft()
    craft.hub.mHub = float(mass)
    craft.hub.IHubPntBc_B = [list(row) for row in inertia]
    craft.hub.r_CN_NInit = list(initial.r_eci_m)
    craft.hub.v_CN_NInit = list(initial.v_eci_mps)
    craft.hub.sigma_BNInit = RigidBodyKinematics.EP2MRP(list(initial.q_body_to_eci)).tolist()
    craft.hub.omega_BN_BInit = list(initial.omega_body_rad_s)
    loads = extForceTorque.ExtForceTorque()
    loads.extForce_B, loads.extTorquePntB_B = list(force), list(torque)
    craft.addDynamicEffector(loads)
    integrator = svIntegrators.svIntegratorRKF78(craft)
    integrator.absTol, integrator.relTol = 1e-12, 1e-12
    craft.setIntegrator(integrator)
    craft.Reset(0)
    trace = []
    previous = -1
    for time in sample_times:
        ns = round(float(time)*1e9)
        if ns <= previous:
            raise ValueError("native_sample_grid_invalid")
        if ns:
            craft.UpdateState(ns)
        observed = craft.scStateOutMsg.read()
        if int(craft.timeBeforeNanos) != ns:
            raise ValueError("native_clock_mismatch")
        trace.append({"time_s": ns/1e9, "native_time_ns": ns,
                      "r_eci_m": list(map(float, observed.r_BN_N)),
                      "v_eci_mps": list(map(float, observed.v_BN_N)),
                      "omega_body_rad_s": list(map(float, observed.omega_BN_B)),
                      "q_body_to_eci": list(map(float, RigidBodyKinematics.MRP2EP(observed.sigma_BN)))})
        previous = ns
    return trace


def compare_native(kernel, native):
    if len(kernel) != len(native) or len(kernel) < 2:
        raise ValueError("native_coverage_invalid")
    errors = {"position_m": [], "velocity_mps": [], "rate_rad_s": [], "attitude_chord": []}
    previous = -math.inf
    for a, b in zip(kernel, native):
        if not all(type(p["time_s"]) in (int, float) and math.isfinite(p["time_s"]) for p in (a, b)) \
                or abs(a["time_s"]-b["time_s"]) > 1e-8 or a["time_s"] <= previous:
            raise ValueError("native_clock_alignment_invalid")
        previous = a["time_s"]
        for key, state_key in (("position_m", "r_eci_m"), ("velocity_mps", "v_eci_mps"),
                               ("rate_rad_s", "omega_body_rad_s")):
            if len(a[state_key]) != 3 or len(b[state_key]) != 3:
                raise ValueError("comparison_vector_invalid")
            delta = _difference(a[state_key], b[state_key])
            if not math.isfinite(delta):
                raise ValueError("nonfinite_native_comparison")
            errors[key].append(delta)
        errors["attitude_chord"].append(quaternion_distance(a["q_body_to_eci"], b["q_body_to_eci"]))
    maxima = {key: max(values) for key, values in errors.items()}
    return {"sample_count": len(kernel), "max_errors": maxima,
            "passed": all(value <= LIMITS["basilisk_"+key] for key, value in maxima.items())}


def analytic_axis_cases():
    inertia = ((200., 0., 0.), (0., 300., 0.), (0., 0., 400.))
    cases = []
    for axis, name in enumerate(("roll", "pitch", "yaw")):
        torque = [0., 0., 0.]
        torque[axis] = 2.
        trace = _propagate(_initial(), _fixed_vehicle(inertia), 12., .05, torque=torque)
        rate_errors, attitude_errors = [], []
        for point in trace:
            t = point["time_s"]
            expected_rate = [0., 0., 0.]
            expected_rate[axis] = torque[axis]/inertia[axis][axis]*t
            angle = .5*torque[axis]/inertia[axis][axis]*t*t
            quaternion = [math.cos(angle/2), 0., 0., 0.]
            quaternion[axis+1] = math.sin(angle/2)
            rate_errors.append(_difference(point["omega_body_rad_s"], expected_rate))
            attitude_errors.append(quaternion_distance(point["q_body_to_eci"], quaternion))
        cases.append({"case_id": "analytic_constant_torque_"+name,
                      "reference": "Euler principal-axis torque: omega=T*t/I, angle=T*t^2/(2I)",
                      "max_rate_error_rad_s": max(rate_errors), "max_attitude_chord": max(attitude_errors),
                      "passed": max(rate_errors) <= LIMITS["analytic_rate_rad_s"]
                      and max(attitude_errors) <= LIMITS["analytic_attitude_chord"], "trace": trace})
    return cases


def free_rotation_case():
    inertia = ((220., 12., -8.), (12., 310., 9.), (-8., 9., 430.))
    trace = _propagate(_initial(omega=(.4, .7, .9)), _fixed_vehicle(inertia), 30., .025)
    energies, momenta = [], []
    for point in trace:
        angular = _momentum(inertia, point["omega_body_rad_s"])
        energies.append(.5*math.fsum(a*b for a, b in zip(point["omega_body_rad_s"], angular)))
        momenta.append(_rotate(point["q_body_to_eci"], angular))
    energy_error = max(abs(x-energies[0])/abs(energies[0]) for x in energies)
    momentum_error = max(_difference(x, momenta[0])/_norm(momenta[0]) for x in momenta)
    excursions = [max(p["omega_body_rad_s"][i] for p in trace)-min(p["omega_body_rad_s"][i] for p in trace)
                  for i in range(3)]
    return {"case_id": "asymmetric_torque_free_rigid_body", "reference": "Independent rotational energy and inertial angular momentum conservation",
            "energy_relative_error": energy_error, "inertial_momentum_relative_error": momentum_error,
            "axis_rate_excursions_rad_s": excursions, "trace": trace,
            "passed": energy_error <= LIMITS["free_rotation_energy_relative"]
            and momentum_error <= LIMITS["free_rotation_momentum_relative"] and min(excursions) > .01}


def nasa_rotation_case():
    raw = (SOURCE / "nesc-case02-sim01-angular-rates.csv").read_bytes()
    metadata = json.loads((SOURCE / "nesc-source.json").read_text())
    if sha256(raw).hexdigest() != NASA_CSV_SHA256 or metadata["subset_sha256"] != NASA_CSV_SHA256 \
            or metadata["source_sha256"] != NASA_UPSTREAM_SHA256:
        raise ValueError("nasa_source_hash_mismatch")
    rows = list(csv.DictReader(raw.decode().splitlines()))
    columns = ["bodyAngularRateWrtEi_deg_s_"+name for name in ("Roll", "Pitch", "Yaw")]
    if len(rows) != 301 or any(abs(float(row["time"])-i*.1) > 1e-9 for i, row in enumerate(rows)):
        raise ValueError("nasa_sample_grid_invalid")
    # Exact SI definitions: foot=.3048 m; pound=.45359237 kg; standard g=9.80665.
    inertia_factor = .45359237*9.80665*.3048
    moments = [.001894220, .006211019, .007194665]
    inertia = [[moments[i]*inertia_factor if i == j else 0. for j in range(3)] for i in range(3)]
    mass = .155404754*.45359237*9.80665/.3048
    initial = _initial(omega=tuple(math.radians(float(rows[0][key])) for key in columns))
    trace = _propagate(initial, _fixed_vehicle(inertia, mass), 30., .025)[::4]
    errors = [[abs(p["omega_body_rad_s"][i]-math.radians(float(row[key]))) for i, key in enumerate(columns)]
              for p, row in zip(trace, rows)]
    maxima = [max(error[i] for error in errors) for i in range(3)]
    return {"case_id": "nesc_case02_rotational_rates_only", "reference": metadata,
            "sample_count": len(trace), "max_axis_rate_error_rad_s": maxima,
            "passed": max(maxima) <= LIMITS["nasa_rotation_rate_rad_s"],
            "nasa_full_checkcase_passed": False, "trace": trace}


def coupled_case(*, native=False, approve_simulation=False):
    inertia = ((220., 12., -8.), (12., 310., 9.), (-8., 9., 430.))
    angle, axis = .7, [1/math.sqrt(14), 2/math.sqrt(14), 3/math.sqrt(14)]
    initial = _initial(omega=(.11, -.17, .23), quaternion=(math.cos(angle/2), *[math.sin(angle/2)*x for x in axis]),
                       velocity=(2., -3., 1.))
    vehicle = _fixed_vehicle(inertia, 1200.)
    force, torque = (150., -100., 500.), (3., -5., 2.)
    # All requested steps lie at/below the kernel's current 0.05 s unloaded
    # substep cap. Larger output steps would compare the same internal grid.
    grids = [_propagate(initial, vehicle, 20., dt, force, torque) for dt in (.05, .025, .0125)]
    last = [grid[-1] for grid in grids]
    def rotational_difference(a, b):
        return _difference(a["omega_body_rad_s"], b["omega_body_rad_s"]) + quaternion_distance(a["q_body_to_eci"], b["q_body_to_eci"])
    coarse, fine = rotational_difference(last[0], last[1]), rotational_difference(last[1], last[2])
    ratio = coarse/fine if fine > 0 else None
    variations = {key: [max(p[key][i] for p in grids[-1])-min(p[key][i] for p in grids[-1]) for i in range(3)]
                  for key in ("r_eci_m", "v_eci_mps", "omega_body_rad_s")}
    result = {"case_id": "coupled_body_force_and_torque", "force_body_N": list(force), "torque_body_Nm": list(torque),
              "off_diagonal_inertia_kg_m2": [list(row) for row in inertia], "trace": grids[-1],
              "shared_reference_conditions": {"mass_kg": 1200., "inertia_about_com_body_kg_m2": [list(row) for row in inertia],
                                              "center_of_mass_equals_body_origin": True, "initial_state": _record(initial),
                                              "body_force_N": list(force), "body_torque_Nm": list(torque),
                                              "gravity_enabled": False, "atmosphere_enabled": False,
                                              "mass_flow_kg_s": 0., "duration_s": 20.},
              "native_integrator": {"name": "Basilisk svIntegratorRKF78", "absolute_tolerance": 1e-12,
                                    "relative_tolerance": 1e-12} if native else None,
              "axis_excursions": variations,
              "step_convergence": {"step_sizes_s": [.05, .025, .0125], "coarse_difference": coarse,
                                   "fine_difference": fine, "ratio": ratio,
                                   "independent_reference": False,
                                   "passed": ratio is not None and ratio >= LIMITS["convergence_minimum_ratio"]},
              "native_basilisk": None}
    if native:
        reference = native_basilisk(initial, inertia, 1200., [p["time_s"] for p in grids[-1]],
                                    force=force, torque=torque, approve_simulation=approve_simulation)
        result["native_basilisk"] = {**compare_native(grids[-1], reference), "trace": reference}
    result["passed"] = (result["step_convergence"]["passed"] and all(min(v) > .01 for v in variations.values())
                        and (not native or result["native_basilisk"]["passed"]))
    return result


def mass_flow_cases():
    """Compare a centered constant-thrust engine with integrated rocket laws."""
    from .starship_sixdof import Command6DOF, Engine, EngineCommand, EngineState, step
    vehicle = replace(_fixed_vehicle(((200., 0., 0.), (0., 300., 0.), (0., 0., 400.))),
                      propellant_capacity_kg=100.,
                      engines=(Engine("analytic-engine", (0., 0., 0.), 4000., 250.),))
    cases = []
    for fuel, duration, name in ((100., 20., "constant_thrust"), (.2, 1., "depletion_then_coast")):
        state = replace(_initial(), propellant_kg=fuel, engine_states=(EngineState(throttle=1.),))
        command = Command6DOF(engines=(EngineCommand(enabled=True, throttle=1.),))
        trace = [_record(state)]
        for _ in range(round(duration/.05)):
            state = step(state, vehicle, command, .05, gravity=False, j2=False, atmosphere=False)
            trace.append(_record(state))
        final = trace[-1]
        expected_fuel = max(0., fuel-4000.*duration/(250.*9.80665))
        expected_velocity = 250.*9.80665*math.log((1000.+fuel)/(1000.+expected_fuel))
        mass_error = abs(final["propellant_kg"]-expected_fuel)
        velocity_error = _difference(final["v_eci_mps"], [0., 0., expected_velocity])
        cases.append({"case_id": "analytic_mass_flow_"+name,
                      "reference": "m_dot=F/(Isp*g0), delta_v=Isp*g0*ln(m_initial/m_final); centered force, fixed attitude",
                      "expected_final_propellant_kg": expected_fuel, "expected_delta_v_mps": expected_velocity,
                      "mass_error_kg": mass_error, "velocity_error_mps": velocity_error, "trace": trace,
                      "variable_mass_flux_dynamics_validated": False,
                      "passed": mass_error <= LIMITS["mass_closure_kg"] and velocity_error <= LIMITS["rocket_equation_mps"]
                      and all(p["propellant_kg"] >= 0. for p in trace)})
    return cases


def mass_properties_case():
    from .starship_sixdof import mass_properties
    inertia = ((200., 5., -3.), (5., 300., 4.), (-3., 4., 400.))
    vehicle = replace(_fixed_vehicle(inertia, 100.), dry_com_body_m=(1., -2., 3.),
                      tank_center_body_m=(-1., 1., 5.), tank_radius_m=2., tank_length_m=6.,
                      propellant_capacity_kg=100.)
    errors = []
    for fuel in (0., 25., 50., 100.):
        total = 100.+fuel
        center = [(100.*a+fuel*b)/total for a, b in zip(vehicle.dry_com_body_m, vehicle.tank_center_body_m)]
        expected = [list(row) for row in inertia]
        cylinder = [fuel*(3*2.**2+6.**2)/12., fuel*(3*2.**2+6.**2)/12., fuel*2.**2/2.]
        for i in range(3):
            expected[i][i] += cylinder[i]
        # Independent two-body parallel-axis construction about the composite
        # center, rather than the production reduced-mass expression.
        for mass, origin in ((100., vehicle.dry_com_body_m), (fuel, vehicle.tank_center_body_m)):
            offset = [a-b for a, b in zip(origin, center)]
            for i in range(3):
                for j in range(3):
                    expected[i][j] += mass*((sum(x*x for x in offset) if i == j else 0.)-offset[i]*offset[j])
        actual = mass_properties(vehicle, fuel)
        errors.append({"propellant_kg": fuel, "mass_error_kg": abs(actual.mass_kg-total),
                       "com_error_m": _difference(actual.com_body_m, center),
                       "max_inertia_error_kg_m2": max(abs(actual.inertia_kg_m2[i][j]-expected[i][j])
                                                      for i in range(3) for j in range(3))})
    return {"case_id": "full_composite_inertia_and_center_of_mass", "reference": "Two independent parallel-axis contributions and homogeneous-cylinder moments",
            "samples": errors, "passed": all(x["mass_error_kg"] <= 1e-10 and x["com_error_m"] <= 1e-12
                                                and x["max_inertia_error_kg_m2"] <= 1e-9 for x in errors)}


def source_hashes():
    files = ["src/runtime/starship_sixdof.py", "src/runtime/starship_physics.py", "src/runtime/starship_sixdof_validation.py",
             "scripts/check_starship_sixdof.py", "examples/starship-sixdof-validation/nesc-source.json",
             "examples/starship-sixdof-validation/nesc-case02-sim01-angular-rates.csv"]
    return {name: sha256((ROOT/name).read_bytes()).hexdigest() for name in files}


def run_validation(*, native_basilisk_enabled=False, approve_simulation=False):
    if approve_simulation is not True:
        raise PermissionError("sixdof_validation_requires_simulation_opt_in")
    started, hashes = datetime.now(timezone.utc).isoformat(), source_hashes()
    cases = [*analytic_axis_cases(), free_rotation_case(), nasa_rotation_case(), *mass_flow_cases(), mass_properties_case(),
             coupled_case(native=native_basilisk_enabled, approve_simulation=approve_simulation)]
    if source_hashes() != hashes:
        raise ValueError("validation_source_changed")
    return {"schema": "missionos.starship_sixdof_validation.v1", "passed": all(case["passed"] for case in cases),
            "limits": dict(LIMITS), "cases": cases, "source_sha256": hashes,
            "runtime_invocation": {"started_at": started, "completed_at": datetime.now(timezone.utc).isoformat(),
                                   "python_version": platform.python_version(),
                                   "native_basilisk_invoked": native_basilisk_enabled,
                                   "basilisk_version": version("bsk") if native_basilisk_enabled else None,
                                   "native_scope": "fixed_mass_rigid_hub_translation_and_rotation" if native_basilisk_enabled else None},
            "nasa_full_checkcase_passed": False, "nasa_certification": False,
            "starship_fidelity_validated": False, "physical_execution": False,
            "limits_of_evidence": [
                "NASA comparison covers only torque-free body angular rates from atmospheric case02 Sim01, not its full trajectory/environment.",
                "Basilisk comparison uses fixed mass, prescribed body loads and no atmosphere or gravity; it does not validate variable-mass flux effects.",
                "Analytic and independent-integrator agreement checks equations and numerical implementation, not unavailable Starship aerodynamic or hardware parameters."]}
