"""Opt-in residual-jet-budget reference capture; requests are not guarantees.

Feedforward and closing feedback share the same signed physical RCS box.
Only the closing-speed stopping heuristic uses the remaining braking margin.
The unchanged component acceleration bound and finite actuator plant still
apply to the total request; no pose, rate, fuel or actuator state is assigned.
"""

from dataclasses import replace
import math

import numpy as np

from . import starship_sixdof as dyn
from .starship_booster_control import reallocate_measured_tvc
from .starship_booster_lowq_capture import _checked_reference, _number, load_reference

POLICY_ID = "fixed_h0_residual_authority_capture_v1"


def residual_capture_control(
    state, craft, profile, *, coast_started_s, control_interval_s, reference=None
):
    """Issue all-OFF main/held-fin requests and physically bounded jet targets."""
    coast_started = _number(coast_started_s, "coast_started_s")
    interval = _number(control_interval_s, "control_interval_s", positive=True)
    if coast_started < 0 or interval > 1:
        raise ValueError("invalid_residual_capture_clock_or_interval")
    reference = _checked_reference(load_reference() if reference is None else reference)
    desired = reference.at(state.time_s, actual_cutoff_time_s=coast_started)
    observed = dyn.observe(state, craft)
    inertia = np.asarray(observed["inertia_kg_m2"], dtype=float)
    omega = np.asarray(state.omega_body_rad_s, dtype=float)
    actual_q = dyn.normalize_quaternion(state.q_body_to_eci)
    inverse = (actual_q[0], *(-x for x in actual_q[1:]))
    error = np.asarray(dyn.quaternion_multiply(inverse, desired.q_body_to_eci), dtype=float)
    if error[0] < 0:
        error = -error
    vector_norm = float(np.linalg.norm(error[1:]))
    angle = 2 * math.atan2(vector_norm, max(0.0, float(error[0])))
    axis = error[1:] / vector_norm if vector_norm > 1e-12 else np.zeros(3)
    rate_eci = dyn.rotate(desired.q_body_to_eci, desired.omega_reference_body_rad_s)
    alpha_eci = dyn.rotate(desired.q_body_to_eci, desired.alpha_reference_body_rad_s2)
    rate = np.asarray(dyn.rotate(inverse, rate_eci))
    reference_alpha = np.asarray(dyn.rotate(inverse, alpha_eci))
    capacities = np.zeros((3, 2))
    delays = [interval]
    unavailable = []
    for index, (engine, actual) in enumerate(zip(craft.engines, state.engine_states)):
        if not actual.available:
            unavailable.append(index)
        if engine.name.startswith("rcs_") and actual.available:
            _, coordinate, sign, _ = engine.name.split("_")
            coordinate, sign = int(coordinate), int(sign)
            if coordinate not in range(3) or sign not in (-1, 1):
                raise ValueError("invalid_residual_capture_signed_jet_identity")
            lever = np.asarray(engine.position_body_m) - observed["com_body_m"]
            torque = np.cross(lever, np.asarray(engine.direction_body) * engine.max_thrust_n)
            capacities[coordinate, int(sign > 0)] += abs(float(torque[coordinate]))
            delays.append(engine.throttle_time_constant_s + interval)
    lower, upper = -capacities[:, 0], capacities[:, 1]
    delay = max(delays)
    configured_alpha = _number(
        profile["guidance"]["max_angular_acceleration_rad_s2"], "alpha_limit", positive=True
    )
    transport = -np.cross(omega, rate)
    feedforward_acceleration = reference_alpha + transport
    gyro = np.cross(omega, inertia @ omega)
    aero = np.asarray(observed["aero_torque_body_nm"])
    gravity_gradient = np.asarray(observed["gravity_gradient_torque_body_nm"])
    main = np.zeros(3)
    for engine, load in zip(craft.engines, observed["engine_loads"]):
        if not engine.name.startswith("rcs_"):
            main += np.asarray(load["torque_body_nm"])
    feedforward_torque = inertia @ feedforward_acceleration + gyro - aero - gravity_gradient - main
    feedforward_feasible = bool(
        np.all(feedforward_torque >= lower) and np.all(feedforward_torque <= upper)
    )
    braking_direction = -(inertia @ axis)
    directional_limits = []
    for i, direction in enumerate(braking_direction):
        directional_limits.append(
            float((upper[i] - feedforward_torque[i]) / direction)
            if direction > 0
            else float((feedforward_torque[i] - lower[i]) / abs(direction))
            if direction < 0
            else None
        )
    stopping_alpha = (
        max(
            0.0,
            min([configured_alpha, *(value for value in directional_limits if value is not None)]),
        )
        if feedforward_feasible
        else 0.0
    )
    speed = (
        math.sqrt((stopping_alpha * delay) ** 2 + 2 * stopping_alpha * angle)
        - stopping_alpha * delay
    )
    desired_rate = rate + axis * speed
    closing_acceleration = (desired_rate - omega) / (2 * delay)
    acceleration_raw = feedforward_acceleration + closing_acceleration
    acceleration = np.clip(acceleration_raw, -configured_alpha, configured_alpha)
    rigid = inertia @ acceleration + gyro
    rcs_raw = rigid - aero - gravity_gradient - main
    rcs_clipped = np.clip(rcs_raw, lower, upper)
    wanted_before_main_allocator = rcs_clipped + main
    numeric = (
        inertia,
        omega,
        axis,
        rate,
        reference_alpha,
        capacities,
        feedforward_acceleration,
        feedforward_torque,
        braking_direction,
        acceleration_raw,
        acceleration,
        rcs_raw,
        rcs_clipped,
        wanted_before_main_allocator,
    )
    if not all(np.isfinite(value).all() for value in numeric) or not all(
        math.isfinite(value) for value in directional_limits if value is not None
    ):
        raise ValueError("nonfinite_residual_capture_request")
    receipt = {
        "schema": "missionos.booster_residual_reference_capture.v1",
        "policy_id": POLICY_ID,
        "desired_reference": desired.record(),
        "coast_started_s": coast_started,
        "control_interval_s": interval,
        "actual_q_body_to_eci": list(actual_q),
        "actual_omega_body_rad_s": omega.tolist(),
        "target_q_body_to_eci": list(desired.q_body_to_eci),
        "reference_rate_eci_rad_s": list(rate_eci),
        "reference_acceleration_eci_rad_s2": list(alpha_eci),
        "reference_rate_actual_body_rad_s": rate.tolist(),
        "reference_acceleration_actual_body_rad_s2": reference_alpha.tolist(),
        "frame_transport_correction_body_rad_s2": transport.tolist(),
        "error_axis_actual_body": axis.tolist(),
        "error_angle_rad": angle,
        "signed_jet_capacity_negative_positive_nm": capacities.tolist(),
        "signed_jet_lower_torque_nm": lower.tolist(),
        "signed_jet_upper_torque_nm": upper.tolist(),
        "actuator_delay_s": delay,
        "actual_inertia_kg_m2": inertia.tolist(),
        "configured_component_alpha_limit_rad_s2": configured_alpha,
        "feedforward_acceleration_body_rad_s2": feedforward_acceleration.tolist(),
        "feedforward_rcs_torque_body_nm": feedforward_torque.tolist(),
        "feedforward_within_signed_jet_box": feedforward_feasible,
        "feedforward_negative_headroom_torque_nm": (feedforward_torque - lower).tolist(),
        "feedforward_positive_headroom_torque_nm": (upper - feedforward_torque).tolist(),
        "gyro_torque_body_nm": gyro.tolist(),
        "actual_aero_torque_body_nm": aero.tolist(),
        "actual_gravity_gradient_torque_body_nm": gravity_gradient.tolist(),
        "actual_main_torque_body_nm": main.tolist(),
        "braking_direction_torque_nm_per_rad_s2": braking_direction.tolist(),
        "braking_alpha_limits_by_axis_rad_s2": directional_limits,
        "stopping_alpha_rad_s2": stopping_alpha,
        "stopping_rate_correction_rad_s": speed,
        "desired_rate_body_rad_s": desired_rate.tolist(),
        "closing_acceleration_unclipped_body_rad_s2": closing_acceleration.tolist(),
        "requested_acceleration_raw_body_rad_s2": acceleration_raw.tolist(),
        "requested_acceleration_body_rad_s2": acceleration.tolist(),
        "component_acceleration_clipped": bool(np.any(acceleration_raw != acceleration)),
        "requested_rigid_torque_body_nm": rigid.tolist(),
        "requested_rcs_torque_raw_body_nm": rcs_raw.tolist(),
        "requested_rcs_torque_body_nm": rcs_clipped.tolist(),
        "rcs_torque_clipped": bool(np.any(rcs_raw != rcs_clipped)),
        "requested_torque_before_main_allocator_body_nm": wanted_before_main_allocator.tolist(),
        "main_moment_added_back_before_allocator": True,
        "main_moment_subtracted_by_allocator_once": True,
        "unavailable_engine_indices": unavailable,
        "available_signed_rcs_command_normalization": True,
        "actual_flap_angles_held": True,
        "reference_flaps_applied": False,
        "stopping_alpha_applied_only_to_closing_speed": True,
        "total_acceleration_norm_clipped": False,
        "closing_acceleration_clipped_to_stopping_alpha": False,
        "stopping_law_is_heuristic_request": True,
        "stopping_guarantee": False,
        "entry_admission_granted": False,
        "finite_actuator_tracking_required": True,
        "actual_state_assigned": False,
        "physics_limits_changed": False,
        "qualification_refreshed": False,
        "model_calls": 0,
    }
    diagnostic = {
        "control_allocation": "residual_reference_capture_v1",
        "target_q_body_to_eci": list(desired.q_body_to_eci),
        "attitude_error_deg": math.degrees(angle),
        "requested_torque_body_nm": wanted_before_main_allocator.tolist(),
        "coast_rate_reference_body_rad_s": desired_rate.tolist(),
        "coast_rate_limit_rad_s": speed,
        "coast_stopping_alpha_rad_s2": stopping_alpha,
        "coast_actuator_delay_s": delay,
        "coast_available_jet_torque_nm": np.minimum(capacities[:, 0], capacities[:, 1]).tolist(),
        "main_engine_commands_off": True,
        "residual_reference_capture": receipt,
    }
    off = dyn.Command6DOF(tuple(dyn.EngineCommand() for _ in craft.engines), state.flap_angles_rad)
    command, diagnostic = reallocate_measured_tvc(
        state, craft, off, diagnostic, profile, observed=observed, recenter_uncommanded_main=True
    )
    engines = list(command.engines)
    for index, (engine, actual) in enumerate(zip(craft.engines, state.engine_states)):
        if not actual.available:
            engines[index] = dyn.EngineCommand()
        elif engine.name.startswith("rcs_"):
            _, coordinate, sign, _ = engine.name.split("_")
            coordinate, sign = int(coordinate), int(sign)
            capacity = capacities[coordinate, int(sign > 0)]
            throttle = float(
                max(0.0, min(1.0, float(rcs_clipped[coordinate]) * sign / max(capacity, 1e-30)))
            )
            engines[index] = dyn.EngineCommand(
                throttle > 1e-8, throttle if throttle > 1e-8 else 0.0
            )
    return replace(command, engines=tuple(engines)), diagnostic
