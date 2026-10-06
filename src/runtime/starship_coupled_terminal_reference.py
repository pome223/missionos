"""Bounded static force/aerodynamic pose continuation, never a plant rollout.

The pin polynomial and duration are immutable. Hypothetical poses and load
queries are retained before Newton/branch use. The real reference tracker is
cloned with all prior history; its requested states are reference hypotheses,
not integrated vehicle observations. No SpaceX or NASA solver is invoked.
"""
from __future__ import annotations

from copy import deepcopy
import math
import time

from . import starship_actual_recovery_shooting as shooting
from . import starship_physics as env
from . import starship_sixdof as dyn
from .starship_fixed_terminal_reference import evaluate_terminal_reference
from .starship_terminal_reference_screen import _reference_state
from .starship_sixdof_mission import vehicle, _attitude
from .starship_fixed_terminal_reference import _tower_frame
from .starship_static_artifacts import StaticArtifactLimit

SCHEMA = "missionos.starship_coupled_terminal_reference.v1"
SCHEMA_V2 = "missionos.starship_coupled_terminal_reference.v2"
SCHEMA_V3 = "missionos.starship_coupled_terminal_reference.v3"
MASS_KINEMATICS_CONVENTION = "nodewise_frozen_mass_com_derivatives_zero"
CONFIG = {"maximum_sweeps": 3, "node_count": 101, "maximum_updates_per_node": 8,
    "maximum_line_search_queries": 4, "maximum_model_queries": 3*101*8*7+3*101,
    "maximum_wall_s": 120., "jacobian_angle_source": "sqrt_machine_epsilon_radians",
    "maximum_persisted_bytes": 100*1024*1024, "minimum_free_bytes": 400*1024*1024,
    "maximum_newton_update_rad": math.pi/4, "physical_plant_calls": 0,
    "translation_reference_changed": False, "profile_or_gain_changed": False,
    "duration_or_origin_search": False}
CONFIG_V2 = {**CONFIG, "newton_increment_source": "analytic_continuous_quaternion_exponential",
    "negative_axial_stationary_source": "existing_tolerance_transverse_stationary_negative_axial_unresolved"}
CONFIG_V3 = {**CONFIG_V2,
    "positive_axial_branch_source": "one_body_pitch_pi_hypothesis_then_positive_axial_constrained_newton",
    "maximum_positive_axial_branch_probes": 3*101, "maximum_model_queries": CONFIG["maximum_model_queries"]+3*101}


class _Budget(Exception):
    pass


def saved_summary_matches(saved_record, returned_record):
    """Compare the exact saved JSON domain, without changing numeric data."""
    returned_payload = {key: value for key, value in returned_record.items()
        if key not in ("raw_artifact", "raw_persisted_before_admission_analysis")}
    return shooting.digest(saved_record) == shooting.digest(returned_payload)


def _increment(q, x, y):
    length = math.hypot(x, y)
    return dyn.normalize_quaternion(dyn.quaternion_multiply(q, dyn.axis_angle((x, y, 0.), length))) if length else tuple(q)


def _increment_continuous(q, x, y):
    """Quaternion exponential with its continuous identity limit, no axis normalization."""
    length = math.hypot(x, y)
    if not length:
        return tuple(q)
    factor = math.sin(length/2)/length
    increment = (math.cos(length/2), x*factor, y*factor, 0.)
    return dyn.normalize_quaternion(dyn.quaternion_multiply(q, increment))


def _static_query(sample, q, rate, acceleration, fuel, initial, booster, profile, catch):
    hypothetical_sample = {**sample, "pose_q_body_to_eci": list(q), "pose_rate_eci_rad_s": list(rate),
                           "pose_acceleration_eci_rad_s2": list(acceleration)}
    state, cg_acceleration, props = _reference_state(hypothetical_sample, initial, booster, profile, catch, fuel)
    observed = dyn.observe(state, booster)
    gravity = env.gravity_acceleration(state.r_eci_m, j2=True)
    required = env.add(dyn.inverse_rotate(q, env.scale(env.add(cg_acceleration, env.scale(gravity, -1.)), props.mass_kg)),
                       env.scale(observed["aero_force_body_n"], -1.))
    angular_acceleration_body = dyn.inverse_rotate(q, tuple(acceleration))
    angular_momentum = dyn._mv(props.inertia_kg_m2, state.omega_body_rad_s)
    rigid_torque = env.add(dyn._mv(props.inertia_kg_m2, angular_acceleration_body),
                          env.cross(state.omega_body_rad_s, angular_momentum))
    engine_torque = env.add(env.add(rigid_torque, env.scale(observed["aero_torque_body_nm"], -1.)),
                           env.scale(observed["gravity_gradient_torque_body_nm"], -1.))
    return {"hypothetical_reference_state": True, "integrated_observation": False,
        "mass_kinematics_convention": MASS_KINEMATICS_CONVENTION,
        "reference_sample": hypothetical_sample, "mass_kg": props.mass_kg, "propellant_kg": fuel,
        "r_eci_m": list(state.r_eci_m), "v_eci_mps": list(state.v_eci_mps),
        "omega_body_rad_s": list(state.omega_body_rad_s), "com_body_m": list(props.com_body_m),
        "gravity_acceleration_eci_mps2": list(gravity), "cg_reference_acceleration_eci_mps2": list(cg_acceleration),
        "aero_force_body_n": list(observed["aero_force_body_n"]), "aero_torque_body_nm": list(observed["aero_torque_body_nm"]),
        "panel_loads": observed["panel_loads"], "required_main_force_body_n": list(required),
        "required_engine_torque_body_nm": list(engine_torque), "rigid_body_torque_body_nm": list(rigid_torque),
        "inertia_kg_m2": props.inertia_kg_m2, "gravity_gradient_torque_body_nm": observed["gravity_gradient_torque_body_nm"],
        "angular_variable_mass_convention": dyn.VARIABLE_MASS_CONVENTION,
        "aero_loads_independently_replayed": False, "future_fin_angles_assumption": "held_origin_actual_angles"}


def generate_coupled_terminal_reference(translation_reference, snapshot, profile, catch, *,
                                       artifact_sink, wall_deadline_monotonic_s, source_map,
                                       development_continuous_increment=False,
                                       development_positive_axial_branch=False):
    """Make one deterministic static continuation family within declared caps."""
    started = time.monotonic()
    if type(development_continuous_increment) is not bool:
        raise ValueError("coupled_reference_increment_policy_must_be_bool")
    if (type(development_positive_axial_branch) is not bool
            or development_positive_axial_branch and not development_continuous_increment):
        raise ValueError("coupled_reference_positive_branch_requires_continuous_policy")
    schema = SCHEMA_V3 if development_positive_axial_branch else SCHEMA_V2 if development_continuous_increment else SCHEMA
    configuration = CONFIG_V3 if development_positive_axial_branch else CONFIG_V2 if development_continuous_increment else CONFIG
    increment = _increment_continuous if development_continuous_increment else _increment
    failure_stage = None
    if (not callable(artifact_sink) or type(wall_deadline_monotonic_s) not in (int, float)
            or not math.isfinite(wall_deadline_monotonic_s)
            or not started < wall_deadline_monotonic_s <= started+CONFIG["maximum_wall_s"]):
        raise ValueError("coupled_reference_requires_predeclared_sink_and_wall_deadline")
    if (not callable(getattr(artifact_sink, "check_before_query", None))
            or not callable(getattr(artifact_sink, "resource_status", None))):
        raise ValueError("coupled_reference_requires_bounded_resource_sink")
    def resource_check():
        nonlocal failure_stage
        failure_stage = "resource_check"
        artifact_sink.check_before_query()
        status = artifact_sink.resource_status()
        keys = ("free_bytes", "maximum_stored_bytes", "minimum_free_bytes", "persisted_bytes")
        if (type(status) is not dict or any(type(status.get(key)) is not int for key in keys)
                or not 0 < status["maximum_stored_bytes"] <= CONFIG["maximum_persisted_bytes"]
                or status["minimum_free_bytes"] < CONFIG["minimum_free_bytes"]
                or status["free_bytes"] < status["minimum_free_bytes"]
                or not 0 <= status["persisted_bytes"] <= status["maximum_stored_bytes"]):
            failure_stage = "resource_status_contract"
            raise ValueError("coupled_reference_resource_status_invalid")
        return status
    resource_check()
    reference, snapshot, profile, catch = deepcopy(translation_reference), deepcopy(snapshot), deepcopy(profile), deepcopy(catch)
    if (type(source_map) is not dict or not source_map or any(type(key) is not str or type(value) is not str
            or len(value) != 64 or any(c not in "0123456789abcdef" for c in value) for key, value in source_map.items())):
        raise ValueError("coupled_reference_requires_frozen_source_map")
    if reference["origin_context_sha256"] != shooting.digest(snapshot):
        raise ValueError("coupled_reference_origin_binding")
    shooting.validate_context(snapshot, profile, catch, shooting.physical_configuration_from_context(snapshot))
    initial, booster = dyn.state_from_dict(snapshot["state"]), vehicle(profile, "booster")
    duration, nodes = reference["duration_s"], CONFIG["node_count"]
    rate_limit = profile["guidance"]["max_angular_acceleration_rad_s2"]/profile["guidance"]["attitude_frequency_rad_s"]
    acceleration_limit = profile["guidance"]["max_angular_acceleration_rad_s2"]
    protocol = {"kind": "coupled_static_protocol", "schema": schema, "configuration": deepcopy(configuration),
        "translation_reference": reference, "origin_context": snapshot, "profile": profile, "catch_profile": catch,
        "wall_deadline_monotonic_s": wall_deadline_monotonic_s,
        "started_monotonic_s": started,
        "source_map": deepcopy(source_map), "source_map_sha256": shooting.digest(source_map),
        "initial_resource_status": artifact_sink.resource_status(),
        "reference_sha256": shooting.digest(reference), "origin_context_sha256": shooting.digest(snapshot),
        "profile_sha256": shooting.digest(profile), "catch_profile_sha256": shooting.digest(catch),
        "hypothetical_reference_states_only": True, "physical_plant_calls": 0,
        "mass_kinematics_convention": MASS_KINEMATICS_CONVENTION,
        "source_binding_is_caller_assertion": True, "physical_execution": False}
    protocol["source_binding_independently_verified"] = False
    origin_artifact = shooting._persist(artifact_sink, protocol)
    carried_frame, carried_tracker = shooting.restore_references(snapshot["context"], profile)
    if carried_tracker is None:
        raise ValueError("coupled_reference_requires_carried_tracker_history")
    anchor = tuple(carried_tracker.quaternion)
    earth_rate = (0., 0., env.EARTH_ROTATION_RAD_S)
    # Seed ONLY with carried pose propagated by known Earth rotation; the
    # rejected independent pose quintic is never reused as a candidate.
    assumptions = [{"q": dyn.quaternion_multiply(dyn.axis_angle((0., 0., 1.), env.EARTH_ROTATION_RAD_S*duration*i/(nodes-1)), anchor),
                    "rate": earth_rate, "acceleration": (0., 0., 0.), "fuel": initial.propellant_kg}
                   for i in range(nodes)]
    counters = {"model_queries_attempted": 0, "model_queries_completed": 0, "model_queries_failed": 0,
        "model_queries_skipped_after_durable_attempt": 0,
        "sweeps_attempted": 0, "sweeps_completed": 0, "updates_attempted": 0, "line_search_queries": 0}
    if development_positive_axial_branch:
        counters["positive_axial_branch_probes"] = 0
    entries, query_artifacts, failure, failure_category = [], [], None, None
    incomplete_io = None
    sweep_metadata = {"index": None, "node": None, "stage": None}

    def query(sample, q, rate, acceleration, fuel, stage):
        nonlocal incomplete_io, failure_stage
        if counters["model_queries_attempted"] >= configuration["maximum_model_queries"] or time.monotonic() >= wall_deadline_monotonic_s:
            failure_stage = "static_budget_guard"
            raise _Budget()
        resource_check()
        counters["model_queries_attempted"] += 1
        request = {"kind": "static_model_attempted", "index": counters["model_queries_attempted"],
            "sweep": sweep_metadata["index"], "node": sweep_metadata["node"], "stage": stage,
            "q": list(q), "rate_eci_rad_s": list(rate), "acceleration_eci_rad_s2": list(acceleration),
            "fuel_kg": fuel, "reference_sample": sample, "hypothetical_not_integrated": True}
        failure_stage = "attempt_record_persistence"
        attempted = shooting._persist(artifact_sink, request)
        incomplete_io = {"stage": "after_attempt_before_model", "attempted_artifact": attempted, "request": request}
        if time.monotonic() >= wall_deadline_monotonic_s:
            counters["model_queries_skipped_after_durable_attempt"] += 1
            failure_stage = "static_budget_guard"
            raise _Budget()
        resource_check()
        if time.monotonic() >= wall_deadline_monotonic_s:
            counters["model_queries_skipped_after_durable_attempt"] += 1
            failure_stage = "static_budget_guard"
            raise _Budget()
        try:
            failure_stage = "static_model_evaluation"
            raw = _static_query(sample, q, rate, acceleration, fuel, initial, booster, profile, catch)
        except Exception as exc:
            counters["model_queries_failed"] += 1
            if development_continuous_increment:
                incomplete_io = {"stage": "model_failed_before_failure_persist", "attempted_artifact": attempted,
                    "request": request, "error_class": type(exc).__name__}
            failure_stage = "failed_model_record_persistence"
            query_artifacts.append(shooting._persist(artifact_sink, {"kind": "static_model_failed", "attempted": attempted,
                "error_class": type(exc).__name__, "counters": deepcopy(counters)}))
            incomplete_io = None
            failure_stage = "static_model_evaluation"
            raise
        counters["model_queries_completed"] += 1
        incomplete_io = {"stage": "model_completed_before_raw_persist", "attempted_artifact": attempted,
                         "request": request, "result": raw}
        failure_stage = "raw_model_record_persistence"
        raw_artifact = shooting._persist(artifact_sink, {"kind": "static_model_raw", "attempted": attempted, "result": raw})
        query_artifacts.append(raw_artifact)
        incomplete_io = None
        if time.monotonic() >= wall_deadline_monotonic_s:
            failure_stage = "static_budget_guard"
            raise _Budget()
        failure_stage = "reference_geometry_or_iteration"
        return {**raw, "raw_artifact": raw_artifact, "attempted_artifact": attempted}

    try:
        for sweep in range(CONFIG["maximum_sweeps"]):
            current = []
            sweep_metadata.update(index=None, node=None, stage=None)
            if time.monotonic() >= wall_deadline_monotonic_s:
                failure_stage = "static_budget_guard"
                raise _Budget()
            counters["sweeps_attempted"] += 1
            sweep_metadata["index"] = sweep
            frame, tracker, previous_raw = deepcopy(carried_frame), deepcopy(carried_tracker), anchor
            current, predicted_fuel = [], initial.propellant_kg
            for index in range(nodes):
                sweep_metadata["node"] = index
                when = initial.time_s+duration*index/(nodes-1)
                sample = evaluate_terminal_reference(reference, when)
                rate, acceleration = assumptions[index]["rate"], assumptions[index]["acceleration"]
                raw_q, converged, low_force, update_history = previous_raw, False, False, []
                branch_receipt = {"attempted": False, "from_query": None, "probe_query": None,
                    "positive_axial_passed": None, "applied": False,
                    "proposal_source": "body_pitch_pi_hypothesis", "physical_state_assigned": False}
                for update in range(CONFIG["maximum_updates_per_node"]):
                    counters["updates_attempted"] += 1
                    raw = query(sample, raw_q, rate, acceleration, predicted_fuel, "base")
                    force = tuple(raw["required_main_force_body_n"])
                    scale = max(1., raw["mass_kg"]*9.81)
                    tolerance = math.sqrt(math.ulp(1.))*scale
                    residual = math.hypot(*force[:2])
                    if env.norm(force) <= tolerance:
                        low_force, converged = True, False
                        break
                    if development_positive_axial_branch and force[2] < 0 and not branch_receipt["attempted"]:
                        counters["positive_axial_branch_probes"] += 1
                        branch_receipt.update(attempted=True, from_query=raw["raw_artifact"])
                        branch_q = _increment_continuous(raw_q, 0., math.pi)
                        branch = query(sample, branch_q, rate, acceleration, predicted_fuel, "positive_axial_branch_probe")
                        branch_force = tuple(branch["required_main_force_body_n"])
                        branch_receipt.update(probe_query=branch["raw_artifact"], positive_axial_passed=branch_force[2] >= 0)
                        if env.norm(branch_force) <= tolerance or branch_force[2] < 0:
                            low_force = env.norm(branch_force) <= tolerance
                            update_history.append({"update": update, "status": "positive_axial_branch_unresolved",
                                "base_query": raw["raw_artifact"]})
                            break
                        branch_receipt["applied"] = True
                        raw_q, raw, force = branch_q, branch, branch_force
                        residual = math.hypot(*force[:2])
                    if residual <= tolerance and force[2] >= 0:
                        converged = True
                        break
                    if development_continuous_increment and residual <= tolerance and force[2] < 0:
                        update_history.append({"update": update, "status": "transverse_stationary_negative_axial",
                            "base_query": raw["raw_artifact"], "transverse_residual_n": residual,
                            "positive_axial_passed": False, "inverse_convergence_established": False})
                        break
                    angle = math.sqrt(math.ulp(1.))
                    perturbations = [query(sample, increment(raw_q, angle if j == 0 else 0., angle if j == 1 else 0.),
                        rate, acceleration, predicted_fuel, "jacobian_"+str(j)) for j in range(2)]
                    a, b = ((perturbations[j]["required_main_force_body_n"][0]-force[0])/angle for j in range(2))
                    c, d = ((perturbations[j]["required_main_force_body_n"][1]-force[1])/angle for j in range(2))
                    determinant = a*d-b*c
                    if abs(determinant) <= math.sqrt(math.ulp(1.))*max(1., abs(a*d), abs(b*c)):
                        update_history.append({"update": update, "status": "singular_transverse_jacobian",
                            "base_query": raw["raw_artifact"], "jacobian_queries": [p["raw_artifact"] for p in perturbations]})
                        break
                    dx, dy = (-d*force[0]+b*force[1])/determinant, (c*force[0]-a*force[1])/determinant
                    size = math.hypot(dx, dy)
                    if size > CONFIG["maximum_newton_update_rad"]:
                        dx, dy = dx*CONFIG["maximum_newton_update_rad"]/size, dy*CONFIG["maximum_newton_update_rad"]/size
                    accepted, line_queries = None, []
                    for line in range(CONFIG["maximum_line_search_queries"]):
                        counters["line_search_queries"] += 1
                        trial_q = increment(raw_q, dx/2**line, dy/2**line)
                        trial = query(sample, trial_q, rate, acceleration, predicted_fuel, "line_search_"+str(line))
                        line_queries.append(trial["raw_artifact"])
                        if (math.hypot(*trial["required_main_force_body_n"][:2]) < residual
                                and (not development_positive_axial_branch or trial["required_main_force_body_n"][2] >= 0)):
                            accepted = trial_q
                            break
                    update_history.append({"update": update, "status": "accepted" if accepted else "line_search_no_decrease",
                        "transverse_residual_n": residual, "bounded_update_body_rad": [dx, dy],
                        "base_query": raw["raw_artifact"], "jacobian_queries": [p["raw_artifact"] for p in perturbations],
                        "line_search_queries": line_queries})
                    if accepted is None:
                        break
                    raw_q = accepted
                # These actual_* tracker inputs are hypothetical reference-body
                # assumptions ONLY. The generated REQUEST itself is determined
                # by the clone's carried quaternion/rate/raw-goal history.
                _, axes = _tower_frame(profile, when)
                axis = dyn.rotate(raw_q, (0., 0., 1.))
                conditioned = frame.target(axis, axes[0], _attitude(axis, axes[0]), time_s=when)
                requested, tracking = tracker.update(conditioned, assumptions[index]["q"],
                    dyn.inverse_rotate(assumptions[index]["q"], rate), when)
                governed = query(sample, requested, tracker.rate_eci_rad_s,
                    tracking["receipt"]["reference_acceleration_eci_rad_s2"], predicted_fuel, "governed_recheck")
                force = governed["required_main_force_body_n"]
                positive_axial = force[2] >= 0
                gx = max(engine.max_gimbal_rad for engine in booster.engines[:13])
                force_cone = (positive_axial and abs(force[0]) <= math.tan(gx)*force[2]
                    and abs(force[1]) <= math.tan(gx)/math.cos(gx)*force[2])
                current.append({"index": index, "time_s": when, "raw_goal_q_body_to_eci": list(raw_q),
                    "governed_request_q_body_to_eci": list(requested), "inverse_converged": converged,
                    "low_force_anchor_unresolved": low_force, "updates": update_history,
                    "conditioned_goal_q_body_to_eci": list(conditioned),
                    "conditioned_frame_diagnostics": deepcopy(frame.diagnostics),
                    "tracking_receipt": tracking["receipt"], "tracking_actual_inputs_are_hypothetical": True,
                    "governed_load_query": governed, "nominal_force_cone_passed": force_cone,
                    "inverse_last_query": raw["raw_artifact"],
                    "nominal_force_cone_is_model_feasibility_certificate": False})
                if development_positive_axial_branch:
                    current[-1]["positive_axial_branch"] = branch_receipt
                nominal_flow = env.norm(force)/(profile["booster"]["engine_isp_s"]*env.STANDARD_GRAVITY_MPS2)
                if index+1 < nodes:
                    predicted_fuel = max(0., predicted_fuel-nominal_flow*duration/(nodes-1))
                previous_raw = raw_q
            entries.append({"sweep": sweep, "nodes": current, "nominal_remaining_fuel_kg": predicted_fuel})
            counters["sweeps_completed"] += 1
            assumptions = [{"q": node["governed_request_q_body_to_eci"],
                "rate": node["tracking_receipt"]["reference_rate_eci_rad_s"],
                "acceleration": node["tracking_receipt"]["reference_acceleration_eci_rad_s2"],
                "fuel": node["governed_load_query"]["propellant_kg"]} for node in current]
    except _Budget:
        failure = "static_wall_or_query_budget_exhausted"
        failure_category = "budget"
        failure_stage = "static_budget_guard"
    except Exception as exc:
        failure = type(exc).__name__
        failure_category = ("recorded_model_failure" if failure_stage == "static_model_evaluation"
            else "resource_or_io" if isinstance(exc, (StaticArtifactLimit, OSError)) else "implementation")
    raw = {"kind": "coupled_static_complete_or_partial", "schema": schema, "origin_artifact": origin_artifact,
        "sweeps": entries, "current_partial_nodes": current if failure and "current" in locals()
            and counters["sweeps_attempted"] > counters["sweeps_completed"] else [],
        "query_artifacts": query_artifacts, "counters": deepcopy(counters), "failure": failure,
        "partial_sweep_index": sweep_metadata["index"] if failure and counters["sweeps_attempted"] > counters["sweeps_completed"] else None,
        "partial_node_index": sweep_metadata["node"] if failure and counters["sweeps_attempted"] > counters["sweeps_completed"] else None,
        "final_resource_status_before_summary": artifact_sink.resource_status(),
        "incomplete_io": incomplete_io, "reference_sha256": shooting.digest(reference),
        "origin_context_sha256": shooting.digest(snapshot), "source_map_sha256": shooting.digest(source_map),
        "wall_seconds": time.monotonic()-started, "hypothetical_reference_states_only": True,
        "mass_kinematics_convention": MASS_KINEMATICS_CONVENTION,
        "physical_plant_calls": 0, "actual_state_assigned": False, "physics_coefficients_changed": False,
        "gain_or_gate_changes": False, "global_optimality_established": False,
        "joint_reference_feasibility_established": False, "candidate_plant_admitted": False,
        "arrival_admitted": False, "support_admitted": False, "physical_execution": False}
    if development_continuous_increment:
        raw.update(failure_category=failure_category, failure_stage=failure_stage if failure else None,
            computation_completed=failure is None and counters["sweeps_completed"] == CONFIG["maximum_sweeps"])
    raw_artifact = shooting._persist(artifact_sink, raw)
    return {**raw, "raw_artifact": raw_artifact, "raw_persisted_before_admission_analysis": True}
