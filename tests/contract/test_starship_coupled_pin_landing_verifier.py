"""Public/stub pin-policy arithmetic; no observer or trajectory invocation."""

from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
import runpy

import pytest

from src.runtime import starship_coupled_pin_landing as producer
from src.runtime import starship_coupled_pin_landing_verifier as check
from src.runtime import starship_sixdof as dynamics
from src.runtime.starship_fixed_terminal_reference_verifier import _coefficients, _mass, _solve
from src.runtime.starship_terminal_reference_screen_verifier import _gravity
from src.runtime.starship_terminal_wrench_verifier import _specs
from src.runtime.starship_actual_recovery_shooting import saved
from src.runtime.starship_attitude_reference import ConditionedGeographicFrame
from src.runtime.starship_reference_tracking import ReferenceTracker

PUBLIC = runpy.run_path(str(Path(__file__).with_name("test_starship_fixed_terminal_reference.py")))


@pytest.fixture(autouse=True)
def no_model_or_integrator(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("source-only fixture cannot call observer, model RHS or integrator")
    for name in ("observe", "derivatives", "_rhs", "step"):
        monkeypatch.setattr(dynamics, name, forbidden)


def fixture_inputs(*, hot=False, post_reference=False, near_target=False, low_reserve=False):
    profile, catch, body, state, _, _, previous_command = PUBLIC["fixture"]()
    if hot:
        states = list(state.engine_states)
        states[0] = replace(states[0], throttle=0.4)
        state = replace(state, engine_states=tuple(states))
    if low_reserve:
        state = replace(state, propellant_kg=500.0)
    if near_target:
        _, com, _, _ = _mass(profile, state.propellant_kg)
        lever = [sum(p[i] for p in catch["support_points_body_m"]) / 2 - com[i] for i in range(3)]
        site, axes = check._tower(profile, state.time_s)
        pin_position = check._add(site, check._scale(axes[2], 103.4))
        cg_position = check._add(pin_position, check._rotate(list(state.q_body_to_eci), lever), -1.0)
        earth = [0.0, 0.0, 7.292115e-5]
        state = replace(state, r_eci_m=tuple(cg_position), v_eci_mps=tuple(check._cross(earth, cg_position)),
            omega_body_rad_s=tuple(check._rotate([state.q_body_to_eci[0], *(-v for v in state.q_body_to_eci[1:])], earth)))
    state_json = json.loads(json.dumps(asdict(state)))
    arrival = check._arrival(state_json, profile, catch)
    points = [{"id": i, "position_body_m": catch["support_points_body_m"][i],
        "position_enu_m": pin["position_enu_m"], "relative_velocity_enu_mps": pin["velocity_enu_mps"],
        "normal_force_n": 0.0, "force_enu_n": [0.0, 0.0, 0.0],
        "footprint_active": False, "top_contact_eligible": False} for i, pin in enumerate(arrival["pins"])]
    observed = {"time_s": state.time_s,
        "position_error_enu_m": [*arrival["midpoint_enu_m"][:2], arrival["midpoint_enu_m"][2] - catch["support_height_m"]],
        "pin_height_above_support_m": [pin["height_above_support_m"] for pin in arrival["pins"]],
        "pins": points, "com_rate_body_mps": arrival["com_rate_body_mps"], "tilt_deg": arrival["tilt_deg"],
        "body_x_east_angle_deg": arrival["clocking_error_deg"], "body_rate_rad_s": arrival["body_rate_rad_s"],
        "propellant_kg": state.propellant_kg, "mass_kg": arrival["mass_kg"], "com_body_m": arrival["com_body_m"],
        "limits": arrival["limits"], "eligible": arrival["eligible"]}
    position, velocity, _ = check.actual_pin_midpoint(state_json, profile, catch)
    start = state.time_s - (30.0 if post_reference else 0.0)
    duration = 20.0
    reference = {"reference_start_time_s": start, "duration_s": duration, "terminal_time_s": start + duration,
        "position_coefficients": [_coefficients(position[i], velocity[i], 0.0, [0.0, 0.0, 103.4][i],
            [0.0, 0.0, -1.5][i], 0.0, duration) for i in range(3)],
        "target_position_enu_m": [0.0, 0.0, 103.4], "target_velocity_enu_mps": [0.0, 0.0, -1.5],
        "pose_scalar_coefficients": [0.0] * 6, "pose_relative_rotation_vector_eci_rad": [0.0] * 3,
        "pose_start_q_body_to_eci": list(state.q_body_to_eci), "pose_target_q_body_to_eci": list(state.q_body_to_eci)}
    return profile, catch, body, state, state_json, previous_command, observed, reference


def source_kinematics(state, command, profile, catch):
    """Explicit zero-aero source fixture, not a measured plant load."""
    mass, com, _, inertia = _mass(profile, state["propellant_kg"])
    motion = check.centroid_motion_from_command(state, command, profile)
    engine_rows, force, torque = [], [0.0] * 3, [0.0] * 3
    derivatives = []
    for i, (spec, actual, target) in enumerate(zip(_specs(profile), state["engine_states"], command["engines"])):
        main = i < profile["booster"]["engine_count"]
        name = f"booster_main_{i}" if main else f"rcs_{(i-33)//4}_{(-1,1)[((i-33)%4)//2]}_{(-1,1)[(i-33)%2]}"
        isp = profile["booster"]["engine_isp_s"] if main else profile["actuators"]["rcs_isp_s"]
        thrust = spec["thrust"] * actual["throttle"] if actual["available"] and state["propellant_kg"] > 0 else 0.0
        f = check._scale(spec["direction"], thrust)
        m = check._cross(check._add(spec["position"], com, -1.0), f)
        engine_rows.append({"name": name, "force_body_n": f, "torque_body_nm": m,
            "thrust_n": thrust, "mass_flow_kg_s": thrust / (isp * 9.80665)})
        force, torque = check._add(force, f), check._add(torque, m)
        target_throttle = target["throttle"] if target["enabled"] and actual["available"] and state["propellant_kg"] > 0 else 0.0
        delta = target_throttle - actual["throttle"]
        derivatives.append(0.0 if abs(delta) <= 1e-10 else max(-spec["rate"], min(spec["rate"], delta / spec["tau"])))
    q, omega, r = state["q_body_to_eci"], state["omega_body_rad_s"], state["r_eci_m"]
    gravity = _gravity(r)
    radial = check._rotate([q[0], *(-v for v in q[1:])], check._scale(r, 1.0 / check._norm(r)))
    def matvec(v):
        return [check._dot(row, v) for row in inertia]
    gg = check._scale(check._cross(radial, matvec(radial)), 3 * 3.986004418e14 / check._norm(r)**3)
    alpha = _solve(inertia, check._add(check._add(torque, gg), check._cross(omega, matvec(omega)), -1.0))
    cgacc = check._add(check._scale(check._rotate(q, force), 1.0 / mass), gravity)
    support_mid = [sum(p[i] for p in catch["support_points_body_m"]) / 2 for i in range(3)]
    lever = check._add(support_mid, com, -1.0)
    ldot, lddot = check._scale(motion["com_rate_body_mps"], -1.0), check._scale(motion["com_acceleration_body_mps2"], -1.0)
    pin_r = check._add(r, check._rotate(q, lever))
    pin_v = check._add(state["v_eci_mps"], check._rotate(q, check._add(check._cross(omega, lever), ldot)))
    pin_a = check._add(cgacc, check._rotate(q, check._add(
        check._add(check._cross(alpha, lever), check._cross(omega, check._cross(omega, lever))),
        check._add(check._scale(check._cross(omega, ldot), 2.0), lddot))))
    earth = [0.0, 0.0, 7.292115e-5]
    relative_v = check._add(pin_v, check._cross(earth, pin_r), -1.0)
    relative_a = check._add(check._add(pin_a, check._scale(check._cross(earth, pin_v), -2.0)), check._cross(earth, check._cross(earth, pin_r)))
    site, axes = check._tower(profile, state["time_s"])
    def local(v):
        return [check._dot(v, a) for a in axes]
    return {"position_enu_m": local(check._add(pin_r, site, -1.0)), "velocity_enu_mps": local(relative_v),
        "acceleration_enu_mps2": local(relative_a), "com_body_m": com,
        "com_rate_body_mps": motion["com_rate_body_mps"], "com_acceleration_body_mps2": motion["com_acceleration_body_mps2"],
        "angular_acceleration_body_rad_s2": alpha, "current_cg_acceleration_eci_mps2": cgacc,
        "source_model_load_inputs": {"mass_kg": mass, "inertia_kg_m2": inertia, "engine_loads": engine_rows,
            "aero_force_body_n": [0.0] * 3, "aero_torque_body_nm": [0.0] * 3,
            "gravity_acceleration_eci_mps2": gravity, "gravity_gradient_torque_body_nm": gg,
            "fuel_rate_kg_s": motion["fuel_rate_kg_s"], "fuel_acceleration_kg_s2": motion["fuel_acceleration_kg_s2"],
            "engine_throttle_derivatives_per_s": derivatives,
            "rhs_angular_variable_mass_convention": "instantaneous_properties_without_depletion_rate_loads",
            "aero_and_plant_rhs_independently_replayed": False},
        "convention": "current_finite_plant_rhs_under_last_causal_command", "physical_plant_integrations": 0,
        "physical_state_assigned": False}


def guidance_fixture(monkeypatch, **options):
    profile, catch, body, state, state_json, previous, observed, reference = fixture_inputs(**options)
    kin = source_kinematics(state_json, previous, profile, catch)
    monkeypatch.setattr(producer, "initial_pin_kinematics", lambda *a: deepcopy(kin))
    command = dynamics.command_from_dict(previous)
    carried = check._rotate(list(state.q_body_to_eci), [0.0, 0.0, 1.0])
    _, _, receipt = producer.pin_guidance_request(state, body, profile, catch, command, reference, observed,
        carried_axis_eci=carried)
    return receipt, state_json, previous, reference, profile, catch, observed, carried


@pytest.mark.parametrize("options,mode", [({}, "fixed_pin_polynomial_tracking"),
    ({"hot": True}, "fixed_pin_polynomial_tracking"), ({"post_reference": True}, "measured_pin_target_hold"),
    ({"post_reference": True, "near_target": True}, "measured_settled_terminal_descent"),
    ({"post_reference": True, "near_target": True, "low_reserve": True}, "measured_pin_target_hold")])
def test_public_stub_guidance_independent_actual_pin_and_existing_hold_feedback(monkeypatch, options, mode):
    data = guidance_fixture(monkeypatch, **options)
    result = check.verify_pin_guidance_request(*data[:6], arrival_observation=data[6], carried_axis_eci=data[7])
    if not result["passed"]:
        check._guidance(*data)
    assert result["arithmetic_passed"], result
    assert data[0]["mode"] == mode
    assert all(result[key] is False for key in ("physical_invocation_admitted", "force_request_is_achieved",
        "dynamics_replayed", "arrival_admitted", "support_admitted", "physical_execution"))


@pytest.mark.parametrize("change", ["comdot", "comddot", "current_alpha", "previous_command", "state_hash",
    "reference_hash", "gains", "pin_acceleration", "cg_acceleration", "requested_force", "axis", "arrival_time",
    "prospective_is_current", "force_is_achieved", "state_assignment", "extra"])
def test_public_stub_guidance_rejects_kinematic_command_reference_or_claim_mutations(monkeypatch, change):
    data = list(guidance_fixture(monkeypatch, hot=True))
    item = data[0]
    if change in ("comdot", "comddot"):
        item["actual_pin_kinematics"]["com_rate_body_mps" if change == "comdot" else "com_acceleration_body_mps2"][2] += 0.01
    elif change == "current_alpha":
        item["current_model_angular_acceleration_body_rad_s2"][1] += 0.01
    elif change == "previous_command":
        data[2]["engines"][0].update(enabled=True, throttle=0.4)
    elif change in ("state_hash", "reference_hash"):
        item["state_sha256" if change == "state_hash" else "translation_reference_sha256"] = "f" * 64
    elif change == "gains":
        item["feedback"]["feedback_gains"]["position_per_s2"][0] *= 2
    elif change == "pin_acceleration":
        item["feedback"]["pin_acceleration_request_enu_mps2"][0] += 0.01
    elif change in ("cg_acceleration", "requested_force", "axis"):
        item[{"cg_acceleration": "requested_cg_acceleration_eci_mps2", "requested_force": "requested_engine_force_eci_n",
            "axis": "requested_axis_eci"}[change]][0] += 0.01
    elif change == "arrival_time":
        data[6]["time_s"] -= 0.1
    elif change in ("prospective_is_current", "force_is_achieved", "state_assignment"):
        item[{"prospective_is_current": "prospective_control_angular_acceleration_is_current_rhs",
            "force_is_achieved": "force_request_is_achieved", "state_assignment": "actual_state_assigned"}[change]] = True
    else:
        item["extra"] = True
    assert not check.verify_pin_guidance_request(*data[:6], arrival_observation=data[6], carried_axis_eci=data[7])["passed"]


@pytest.mark.parametrize("change", ["cg_lever", "retuned", "request_space", "lever_twice", "horizontal_acceleration"])
def test_public_stub_terminal_hover_cg_space_and_no_second_lever_subtraction(monkeypatch, change):
    data = list(guidance_fixture(monkeypatch, post_reference=True))
    item = data[0]
    if change == "cg_lever":
        item["terminal_horizontal_feedback"]["observed_cg_position_enu_m"][0] += 0.01
    elif change == "retuned":
        item["terminal_horizontal_feedback"]["inner_controller_retuned"] = True
    elif change == "request_space":
        item["horizontal_request_space"] = "material_pin_tower_enu"
    elif change == "lever_twice":
        item["terminal_horizontal_pin_lever_subtracted_twice"] = True
    else:
        item["terminal_horizontal_feedback"]["requested_net_acceleration_enu_mps2"][0] += 0.01
    assert not check.verify_pin_guidance_request(*data[:6], arrival_observation=data[6], carried_axis_eci=data[7])["passed"]


def test_public_actual_pin_velocity_includes_current_fuel_centroid_motion():
    p, catch, _, _, state, command, _, _ = fixture_inputs(hot=True)
    position, velocity, _ = check.actual_pin_midpoint(state, p, catch)
    cold = deepcopy(state)
    cold["engine_states"][0]["throttle"] = 0.0
    cold_position, cold_velocity, _ = check.actual_pin_midpoint(cold, p, catch)
    _, _, cdot, _ = check._flow_and_centroid(state, p)
    _, axes = check._tower(p, state["time_s"])
    offset = check._rotate(state["q_body_to_eci"], check._scale(cdot, -1.0))
    assert position == cold_position
    assert check._add(velocity, cold_velocity, -1.0) == pytest.approx([check._dot(offset, axis) for axis in axes], abs=1e-10)
    assert check.centroid_motion_from_command(state, command, p)["com_acceleration_body_mps2"][2] != 0.0


@pytest.mark.parametrize("field,delta", [("requested_engine_force_eci_n", 1e-9), ("requested_axis_eci", 1e-14)])
def test_verified_recorded_operands_feed_downstream_without_roundoff_cascade(monkeypatch, field, delta):
    data = list(guidance_fixture(monkeypatch, near_target=True))
    data[0][field][0] += delta
    force, axis = check._guidance(*data)
    assert force == data[0]["requested_engine_force_eci_n"]
    assert axis == data[0]["requested_axis_eci"]
    expected = check._base_selection(data[1], data[0]["requested_engine_force_eci_n"], data[0]["requested_axis_eci"], data[4])
    replayed = check._base_selection(data[1], force, axis, data[4])
    assert expected == replayed


@pytest.mark.parametrize("field", ["requested_engine_force_eci_n", "requested_axis_eci"])
def test_downstream_replay_does_not_bypass_independent_operand_validation(monkeypatch, field):
    data = list(guidance_fixture(monkeypatch, near_target=True))
    data[0][field][0] += 0.01
    assert not check.verify_pin_guidance_request(*data[:6], arrival_observation=data[6], carried_axis_eci=data[7])["passed"]


@pytest.mark.parametrize("offset,passes", [(4.1e-10, True), (0.01, False)])
def test_post_reference_hover_uses_validated_recorded_pin_operands_without_cancellation_cascade(monkeypatch, offset, passes):
    data = list(guidance_fixture(monkeypatch, post_reference=True))
    for pin in data[6]["pins"]:
        pin["position_enu_m"][0] += offset
    data[6]["position_error_enu_m"][0] += offset
    state = dynamics.state_from_dict(data[1])
    body = PUBLIC["fixture"]()[2]
    _, _, receipt = producer.pin_guidance_request(state, body, data[4], data[5], dynamics.command_from_dict(data[2]),
        data[3], data[6], carried_axis_eci=data[7])
    data[0] = receipt
    result = check.verify_pin_guidance_request(*data[:6], arrival_observation=data[6], carried_axis_eci=data[7])
    assert result["arithmetic_passed"] is passes
    if passes:
        assert receipt["terminal_horizontal_feedback"]["observed_pin_midpoint_enu_m"][0] == sum(
            pin["position_enu_m"][0] for pin in data[6]["pins"]) / 2.0
        assert result["physical_invocation_admitted"] is False


def public_trace_fixture(monkeypatch, *, pending=False, eligible_endpoint=False):
    """Assemble one typed request and a kinematically consistent source fixture.

    No simulate_* or dynamics step is called. Declared aero inputs are zero;
    this fixture does not establish that a trajectory was actually integrated.
    """
    from src.runtime.starship_booster_control import control_with_measured_tvc, reallocate_measured_tvc
    from src.runtime.starship_terminal_wrench import allocate_terminal_wrench

    profile, catch, body, state, state_json, previous, arrival, reference = fixture_inputs(near_target=True)
    _, _, _, _, original, _, _ = PUBLIC["fixture"]()
    tracker = ReferenceTracker(profile, state.q_body_to_eci, 99.9)
    frame = ConditionedGeographicFrame(state.q_body_to_eci, 99.9,
        maximum_roll_rate_rad_s=profile["guidance"]["max_angular_acceleration_rad_s2"] / profile["guidance"]["attitude_frequency_rad_s"])
    snapshot = deepcopy(original)
    snapshot["state"] = deepcopy(state_json)
    snapshot["context"]["reference_tracker"] = saved(vars(tracker))
    snapshot["context"]["conditioned_reference"] = producer._controller_memory(
        frame, tracker, None, dynamics.command_from_dict(previous), 99.9)["conditioned_reference"]
    reference.update(origin_context_sha256=check.digest(snapshot), previous_command=deepcopy(previous),
        prior_state={**deepcopy(state_json), "time_s": 99.9})
    prior_command = dynamics.command_from_dict(previous)
    before_memory = producer._controller_memory(frame, tracker, None, prior_command, 99.9)
    kin = source_kinematics(state_json, previous, profile, catch)
    monkeypatch.setattr(producer, "initial_pin_kinematics", lambda *a: deepcopy(kin))
    observed = {"com_body_m": kin["com_body_m"], "engine_loads": kin["source_model_load_inputs"]["engine_loads"],
        "aero_torque_body_nm": [0.0] * 3, "dynamic_pressure_pa": 0.0}
    monkeypatch.setattr(dynamics, "observe", lambda *a, **k: deepcopy(observed))
    force, axis, guidance = producer.pin_guidance_request(state, body, profile, catch, prior_command, reference,
        arrival, carried_axis_eci=check._rotate(list(tracker.quaternion), [0.0, 0.0, 1.0]))
    _, axes = check._tower(profile, state.time_s)
    preferred = check._attitude(list(axis), axes[0])
    conditioned = frame.target(axis, axes[0], preferred, time_s=state.time_s)
    target, tracking = tracker.update(conditioned, state.q_body_to_eci, state.omega_body_rad_s, state.time_s)
    count, throttle, pair, selection = producer.base_actuation_request(state, body, force, axis)
    base, control = control_with_measured_tvc(state, body, target, throttle, count, profile,
        use_flaps=True, development_fin_allocation=True, control_interval_s=0.1,
        development_fin_policy="finite_regularized_fins_v1",
        reference_rate_body_rad_s=tracking["reference_rate_body_rad_s"],
        reference_acceleration_body_rad_s=tracking["reference_acceleration_body_rad_s"])
    if pair is not None:
        indices, throttle = pair
        engines = list(base.engines)
        for i in range(33):
            engines[i] = replace(engines[i], enabled=i in indices, throttle=throttle if i in indices else 0.0)
        base = dynamics.Command6DOF(tuple(engines), base.flap_angles_rad)
        base, control = reallocate_measured_tvc(state, body, base, control, profile, observed=observed)
    command, wrench = allocate_terminal_wrench(state, body, base, observed, force, control["requested_torque_body_nm"],
        interval_s=0.1, up_eci=axes[2], minimum_vertical_thrust_n=guidance["minimum_vertical_thrust_n"])
    selected = wrench["mask_candidates"][wrench["selected_mask_index"]] if wrench["status"] == "accepted" else None
    predicted_force = selected["predicted_force_eci_n"] if selected else wrench.get("base_predicted_force_eci_n")
    predicted_torque = selected["predicted_engine_torque_body_nm"] if selected else wrench.get("base_predicted_engine_torque_body_nm")
    navigation = {**control, "coupled_pin_guidance": guidance, "reference_tracking": tracking["receipt"],
        "conditioned_reference_diagnostics": deepcopy(frame.diagnostics), "terminal_wrench": wrench,
        "finite_wrench_summary": {"status": wrench["status"], "fallback_reason": wrench["reason"],
            "commanded_main_indices": [i for i in range(33) if command.engines[i].enabled],
            "predicted_finite_force_eci_n": predicted_force, "predicted_finite_engine_torque_body_nm": predicted_torque,
            "predicted_force_residual_eci_n": check._add(predicted_force, list(force), -1.0) if predicted_force is not None else None,
            "prediction_is_achieved_force_or_moment": False, "command_is_hardware_execution": False},
        "current_dynamic_pressure_pa": 0.0, "arrival_observation": arrival, "macrostep_s": 0.1,
        "base_main_throttle": throttle, "base_main_engine_count": count, "finite_fin_policy": "finite_regularized_fins_v1",
        "requested_engine_force_eci_n": list(force), "base_actuation": selection}
    binding = {key: "a" * 64 for key in check._BINDING_HASHES}
    binding.update(schema=check.BINDING_SCHEMA, source_origin_checkpoint_index=1,
        source_binding_is_caller_assertion=True, source_authentication_independently_verified=False,
        reference_is_execution=False, full_launch_reexecuted=False)
    for key, value in (("origin_context_sha256", snapshot), ("prior_command_sha256", previous),
            ("translation_reference_sha256", reference), ("profile_sha256", profile), ("catch_profile_sha256", catch)):
        binding[key] = check.digest(value)
    request = {"schema": check.REQUEST_SCHEMA, "origin_context_sha256": check.digest(snapshot), "duration_s": 0.1,
        "maximum_integration_steps": 1, "wall_deadline_monotonic_s": 130.0, "automatic_retry": False,
        "hardware_execution": False, "catch_execution": False}
    corpus = {}

    def persist(value):
        payload = saved(value)
        digest = check.digest(payload)
        identifier = digest[:32]
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        corpus[identifier] = payload
        return {"artifact_id": identifier, "relative_path": identifier + ".json", "format": "json",
            "bytes": len(raw), "sha256": digest, "raw_json_sha256": digest, "persisted_before_analysis": True}

    inputs = persist({"kind": "coupled_pin_trial_inputs", "schema": check.SCHEMA, "origin_context": snapshot,
        "translation_reference": reference, "profile": profile, "catch_profile": catch, "configuration": check.CONFIG,
        "request": request, "source_bindings": binding, "controller_memory": before_memory,
        "started_monotonic_s": 10.0, "isolated_saved_state_continuation": True, "full_launch_reexecuted": False,
        "hardware_execution": False})
    artifact = persist({"kind": "coupled_pin_step_receipt", "step_index": 0, "time_s": state.time_s,
        "state": state_json, "state_sha256": check.digest(state_json), "base_command": asdict(base),
        "command": asdict(command), "navigation": navigation, "controller_memory_before": before_memory})
    points = [{"time_s": state.time_s, "phase": check.PHASE, "state": state_json,
        "command": None if pending else saved(asdict(command)), "com_rate_body_mps": kin["com_rate_body_mps"],
        "step_receipt_artifact": artifact}]
    final_state = deepcopy(state_json)
    if not pending:
        final_state["time_s"] += 0.1
        final_state["r_eci_m"] = check._add(final_state["r_eci_m"], check._scale(final_state["v_eci_mps"], 0.1))
        if eligible_endpoint:
            _, com, _, _ = _mass(profile, final_state["propellant_kg"])
            lever = [sum(p[i] for p in catch["support_points_body_m"]) / 2 - com[i] for i in range(3)]
            site, axes = check._tower(profile, final_state["time_s"])
            final_state["r_eci_m"] = check._add(check._add(site, check._scale(axes[2], 103.4)),
                check._rotate(final_state["q_body_to_eci"], lever), -1.0)
            final_state["v_eci_mps"] = check._add(check._cross([0.0, 0.0, 7.292115e-5], final_state["r_eci_m"]),
                check._scale(axes[2], -1.5))
        points.append({"time_s": final_state["time_s"], "phase": check.PHASE, "state": final_state,
            "command": None, "com_rate_body_mps": [0.0] * 3, "step_receipt_artifact": None})
    actual = check._arrival(final_state, profile, catch)
    if eligible_endpoint:
        assert actual["eligible"] and not pending
    final_observation = deepcopy(arrival)
    final_observation.update(time_s=final_state["time_s"], eligible=actual["eligible"],
        position_error_enu_m=[*actual["midpoint_enu_m"][:2], actual["midpoint_enu_m"][2] - catch["support_height_m"]],
        pin_height_above_support_m=[p["height_above_support_m"] for p in actual["pins"]],
        tilt_deg=actual["tilt_deg"], body_x_east_angle_deg=actual["clocking_error_deg"],
        body_rate_rad_s=actual["body_rate_rad_s"], limits=actual["limits"])
    for i, point in enumerate(actual["pins"]):
        final_observation["pins"][i].update(position_enu_m=point["position_enu_m"], relative_velocity_enu_mps=point["velocity_enu_mps"])
    final_memory = producer._controller_memory(frame, tracker, None,
        prior_command if pending else command, 99.9 if pending else state.time_s)
    failure = {"error_class": "ArithmeticError", "computation_completed": False, "arrival_admitted": False} if pending else None
    termination = ("coupled_pin_controller_or_record_exception" if pending
        else "catch_handoff" if eligible_endpoint else "simulated_duration_exhausted")
    run = {"scenario": "booster_coupled_pin_trial", "body_id": "booster", "guidance_policy": check.POLICY_ID,
        "initial_state": state_json, "final_state": final_state, "samples": [], "contact": None,
        "events": [{"event": termination, "time_s": final_state["time_s"], "state": final_state}],
        "recovery_record": {"schema": check.SCHEMA, "policy_id": check.POLICY_ID, "guidance_configuration": check.CONFIG,
            "origin_context": snapshot, "translation_reference": reference, "source_bindings": binding,
            "inputs_artifact": inputs, "initial_controller_memory": before_memory, "final_controller_memory": final_memory,
            "checkpoints": points, "handoff": {"eligible": eligible_endpoint, "time_s": final_state["time_s"],
                "state": final_state if eligible_endpoint else None,
                "observation": final_observation, "limits": actual["limits"]}, "request": request,
            "failure": failure, "failure_chain": [failure] if failure else [], "started_monotonic_s": 10.0,
            "production_policy_admitted": False, "physical_execution": False, "missionos_dispatch": False},
        "outcome": {"termination": termination, "start_time_s": state.time_s, "end_time_s": final_state["time_s"],
            "duration_s": final_state["time_s"] - state.time_s, "integration_steps": 0 if pending else 1,
            "handoff_reached": eligible_endpoint, "final_hull_clearance_m": 1.0,
            "actual_isolated_simulator_continuation": not pending,
            "full_launch_reexecuted": False, "catch_executed": False, "physical_execution": False,
            "mission_completed": False, "wall_seconds": 0.1}}
    return saved(run), profile, catch, corpus


@pytest.mark.parametrize("pending", [False, True])
def test_public_stub_known_policy_trace_and_pending_request_have_no_physical_credit(monkeypatch, pending):
    run, profile, catch, corpus = public_trace_fixture(monkeypatch, pending=pending)
    verdict = check.verify_coupled_pin_landing_trial(run, profile, catch, step_corpus=corpus)
    if not verdict["passed"]:
        check._trace(run, profile, catch, corpus, None, None)
    assert verdict["arithmetic_record_integrity_passed"], verdict
    assert verdict["durable_command_attempts_checked"] == 1
    assert verdict["positive_time_transitions_checked"] == (0 if pending else 1)
    assert verdict["computation_completed_without_recorded_error"] is (not pending)
    assert verdict["requested_interval_completed"] is (not pending)
    assert all(verdict[k] is False for k in ("physical_invocation_admitted", "runtime_invocation_independently_verified",
        "arrival_admitted", "support_admitted", "mission_completed", "source_authenticated"))


def set_termination(run, termination):
    run["outcome"]["termination"] = termination
    run["events"][0]["event"] = termination


def public_wall_before_integration_fixture(monkeypatch):
    run, profile, catch, corpus = public_trace_fixture(monkeypatch, pending=True)
    run["recovery_record"].update(failure=None, failure_chain=[])
    set_termination(run, "wall_budget_exhausted_before_integration")
    return run, profile, catch, corpus


def test_public_pending_wall_exit_is_valid_partial_without_applied_step_or_interval_credit(monkeypatch):
    run, profile, catch, corpus = public_wall_before_integration_fixture(monkeypatch)
    verdict = check.verify_coupled_pin_landing_trial(run, profile, catch, step_corpus=corpus)
    assert verdict["arithmetic_record_integrity_passed"], verdict
    assert verdict["computation_completed_without_recorded_error"]
    assert not verdict["requested_interval_completed"]
    assert verdict["durable_command_attempts_checked"] == 1
    assert verdict["positive_time_transitions_checked"] == 0
    assert all(verdict[k] is False for k in ("physical_invocation_admitted", "arrival_admitted", "support_admitted",
        "physical_execution", "mission_completed"))


@pytest.mark.parametrize("change", ["undeclared_reason", "fake_applied_credit", "last_command_advanced", "lost_attempt"])
def test_public_pending_wall_exit_rejects_causal_credit_or_retention_mutations(monkeypatch, change):
    run, profile, catch, corpus = public_wall_before_integration_fixture(monkeypatch)
    record = run["recovery_record"]
    if change == "undeclared_reason":
        set_termination(run, "wall_budget_exhausted")
    elif change == "fake_applied_credit":
        run["outcome"].update(integration_steps=1, actual_isolated_simulator_continuation=True)
    elif change == "last_command_advanced":
        step = corpus[record["checkpoints"][0]["step_receipt_artifact"]["artifact_id"]]
        record["final_controller_memory"]["previous_actual_command"] = deepcopy(step["command"])
    else:
        manifest = record["checkpoints"][0]["step_receipt_artifact"]
        del corpus[manifest["artifact_id"]]
        record["checkpoints"][0]["step_receipt_artifact"] = None
    assert not check.verify_coupled_pin_landing_trial(run, profile, catch, step_corpus=corpus)["passed"], change


def test_public_completed_macro_eligible_final_state_has_matching_handoff_without_physical_admission(monkeypatch):
    run, profile, catch, corpus = public_trace_fixture(monkeypatch, eligible_endpoint=True)
    verdict = check.verify_coupled_pin_landing_trial(run, profile, catch, step_corpus=corpus)
    assert verdict["arithmetic_record_integrity_passed"] and verdict["final_same_time_arrival_gates_satisfied"], verdict
    assert verdict["positive_time_transitions_checked"] == 1
    assert all(verdict[k] is False for k in ("physical_invocation_admitted", "arrival_admitted", "support_admitted"))


def public_initial_eligible_fixture(monkeypatch):
    """A source-only already-eligible loophead record; no attempted command."""
    run, profile, catch, corpus = public_trace_fixture(monkeypatch, eligible_endpoint=True)
    record, outcome = run["recovery_record"], run["outcome"]
    final = deepcopy(run["final_state"])
    run["initial_state"] = deepcopy(final)
    record["origin_context"]["state"] = deepcopy(final)
    record["translation_reference"]["origin_context_sha256"] = check.digest(record["origin_context"])
    record["request"]["origin_context_sha256"] = check.digest(record["origin_context"])
    binding = record["source_bindings"]
    binding["origin_context_sha256"] = check.digest(record["origin_context"])
    binding["translation_reference_sha256"] = check.digest(record["translation_reference"])
    record["checkpoints"] = [record["checkpoints"][-1]]
    record["final_controller_memory"] = deepcopy(record["initial_controller_memory"])
    outcome.update(start_time_s=final["time_s"], duration_s=0.0, integration_steps=0,
        actual_isolated_simulator_continuation=False)
    manifest = record["inputs_artifact"]
    inputs = deepcopy(corpus[manifest["artifact_id"]])
    inputs.update(origin_context=deepcopy(record["origin_context"]),
        translation_reference=deepcopy(record["translation_reference"]),
        request=deepcopy(record["request"]), source_bindings=deepcopy(binding))
    corpus = {manifest["artifact_id"]: inputs}
    manifest["raw_json_sha256"] = check.digest(inputs)
    return run, profile, catch, corpus


def test_public_existing_initial_eligible_loophead_handoff_preserves_zero_step_fact(monkeypatch):
    run, profile, catch, corpus = public_initial_eligible_fixture(monkeypatch)
    verdict = check.verify_coupled_pin_landing_trial(run, profile, catch, step_corpus=corpus)
    assert verdict["arithmetic_record_integrity_passed"] and verdict["final_same_time_arrival_gates_satisfied"], verdict
    assert verdict["positive_time_transitions_checked"] == verdict["durable_command_attempts_checked"] == 0
    assert not verdict["requested_interval_completed"] and not verdict["physical_invocation_admitted"]


def test_public_no_step_wall_loophead_cannot_gain_new_endpoint_promotion(monkeypatch):
    run, profile, catch, corpus = public_initial_eligible_fixture(monkeypatch)
    run["recovery_record"]["handoff"].update(eligible=False, state=None)
    run["outcome"]["handoff_reached"] = False
    set_termination(run, "wall_budget_exhausted")
    verdict = check.verify_coupled_pin_landing_trial(run, profile, catch, step_corpus=corpus)
    assert verdict["arithmetic_record_integrity_passed"] and verdict["final_same_time_arrival_gates_satisfied"], verdict
    assert verdict["positive_time_transitions_checked"] == 0 and not verdict["arrival_admitted"]


@pytest.mark.parametrize("termination", ["simulated_duration_exhausted", "integration_step_budget_exhausted", "wall_budget_exhausted"])
def test_public_eligible_final_state_cannot_silently_lose_handoff_at_valid_exit(monkeypatch, termination):
    run, profile, catch, corpus = public_trace_fixture(monkeypatch, eligible_endpoint=True)
    run["recovery_record"]["handoff"].update(eligible=False, state=None)
    run["outcome"]["handoff_reached"] = False
    set_termination(run, termination)
    assert not check.verify_coupled_pin_landing_trial(run, profile, catch, step_corpus=corpus)["passed"]


@pytest.mark.parametrize("suppression", ["failure", "contact"])
def test_public_final_geometry_does_not_override_recorded_failure_or_contact(monkeypatch, suppression):
    run, profile, catch, corpus = public_trace_fixture(monkeypatch, eligible_endpoint=True)
    record = run["recovery_record"]
    record["handoff"].update(eligible=False, state=None)
    run["outcome"]["handoff_reached"] = False
    if suppression == "failure":
        failure = {"error_class": "ArithmeticError", "computation_completed": False, "arrival_admitted": False}
        record.update(failure=failure, failure_chain=[failure])
        set_termination(run, "coupled_pin_controller_or_record_exception")
    else:
        run["contact"] = {"contact": True}
        set_termination(run, "surface_contact")
    verdict = check.verify_coupled_pin_landing_trial(run, profile, catch, step_corpus=corpus)
    assert verdict["arithmetic_record_integrity_passed"] and verdict["final_same_time_arrival_gates_satisfied"], verdict
    record["handoff"].update(eligible=True, state=deepcopy(run["final_state"]))
    run["outcome"]["handoff_reached"] = True
    set_termination(run, "catch_handoff")
    assert not check.verify_coupled_pin_landing_trial(run, profile, catch, step_corpus=corpus)["passed"]


@pytest.mark.parametrize("change", ["old_policy", "state_projection", "prior_command", "initial_tracker_reset",
    "final_actual_command_advanced", "step_state", "clock", "scalar_selector", "achieved_wrench",
    "command", "step_credit", "hidden_receipt", "gate_widening", "failure_erasure"])
def test_public_stub_trace_rejects_command_history_source_or_outcome_mutations(monkeypatch, change):
    run, profile, catch, corpus = public_trace_fixture(monkeypatch, pending=True)
    record = run["recovery_record"]
    manifest = record["checkpoints"][0]["step_receipt_artifact"]
    step = corpus[manifest["artifact_id"]]
    if change == "old_policy":
        run["guidance_policy"] = "predictive_return_v1"
    elif change == "state_projection":
        run["initial_state"]["r_eci_m"][0] += 1.0
    elif change == "prior_command":
        record["initial_controller_memory"]["previous_actual_command"]["engines"][0].update(enabled=True, throttle=0.4)
    elif change == "initial_tracker_reset":
        record["initial_controller_memory"]["reference_tracker"]["rate_eci_rad_s"][0] += 0.01
    elif change == "final_actual_command_advanced":
        record["final_controller_memory"]["previous_actual_command"] = deepcopy(step["command"])
    elif change == "step_state":
        step["state"]["r_eci_m"][0] += 1.0
    elif change == "clock":
        record["checkpoints"][0]["time_s"] += 0.1
    elif change == "scalar_selector":
        step["navigation"]["base_actuation"]["least_squares_actual_axis_projection_n"] += 1.0
    elif change == "achieved_wrench":
        step["navigation"]["finite_wrench_summary"]["prediction_is_achieved_force_or_moment"] = True
    elif change == "command":
        step["base_command"]["engines"][0]["gimbal_x_rad"] += 0.01
    elif change == "step_credit":
        run["outcome"].update(integration_steps=1, actual_isolated_simulator_continuation=True)
    elif change == "hidden_receipt":
        corpus["f" * 32] = {"kind": "coupled_pin_step_receipt"}
    elif change == "gate_widening":
        record["handoff"]["limits"]["body_rate_rad_s"] *= 2
    else:
        record["failure_chain"] = []
    manifest["raw_json_sha256"] = check.digest(step)
    assert not check.verify_coupled_pin_landing_trial(run, profile, catch, step_corpus=corpus)["passed"], change
