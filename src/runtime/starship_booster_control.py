"""Opt-in booster TVC allocation using measured finite engine thrust.

The ordinary controller builds its allocation from requested engine count and
throttle. During shutdown that omits engines which still produce real thrust.
This adapter preserves engine enable/throttle and flap commands, while assigning
gimbals and residual RCS against the current main-engine loads. It does not add
forces, restart engines, reset state or claim adequate return-guidance authority.
"""
from __future__ import annotations

from dataclasses import replace
import math

import numpy as np

from . import starship_sixdof as dyn

ALLOCATION_POLICY = "measured_tvc_v1"


def reallocate_measured_tvc(state, vehicle, command, diagnostic, profile, *, observed=None):
    """Reallocate a controller result around achieved main-engine deflections.

    The controller's requested torque is its remaining demand after predicted
    flap allocation. Current main moments are subtracted before solving for
    incremental gimbal angles. Actuator lag/rate limits still apply in dynamics;
    the residual estimate is not a statement of immediately achieved torque.
    """
    observed = dyn.observe(state, vehicle) if observed is None else observed
    columns, channels, main_moment = [], [], np.zeros(3)
    actual_thrust = 0.
    for index, (engine, actual, load) in enumerate(zip(vehicle.engines, state.engine_states, observed["engine_loads"])):
        if engine.name.startswith("rcs_"):
            continue
        main_moment += np.asarray(load["torque_body_nm"])
        thrust = load["thrust_n"]
        actual_thrust += thrust
        if not actual.available or thrust <= 0 or engine.max_gimbal_rad <= 0:
            continue
        if engine.direction_body != (0., 0., 1.):
            raise ValueError("measured TVC requires longitudinal main-engine thrust axes")
        gx, gy = actual.gimbal_x_rad, actual.gimbal_y_rad
        lever = np.asarray(engine.position_body_m)-observed["com_body_m"]
        # Exact local derivatives of Ry(gy) Rx(gx) body +Z.
        dx = np.asarray((-math.sin(gy)*math.sin(gx), -math.cos(gx), -math.cos(gy)*math.sin(gx)))
        dy = np.asarray((math.cos(gy)*math.cos(gx), 0., -math.sin(gy)*math.cos(gx)))
        columns.extend((np.cross(lever, thrust*dx), np.cross(lever, thrust*dy)))
        channels.extend(((index, "gimbal_x_rad"), (index, "gimbal_y_rad")))
    info = {"policy_id": ALLOCATION_POLICY, "main_thrust_n": actual_thrust,
            "gimballed_engine_count": len(channels)//2, "main_throttle_commands_preserved": True,
            "current_main_torque_body_nm": main_moment.tolist(), "applied": bool(columns)}
    if not columns:
        return command, {**diagnostic, "measured_tvc_allocation": info}
    desired = np.asarray(diagnostic["requested_torque_body_nm"])
    matrix = np.asarray(columns).T
    changes = np.linalg.lstsq(matrix, desired-main_moment, rcond=1e-9)[0]
    engines, clipped = list(command.engines), []
    for change, (index, axis) in zip(changes, channels):
        actual = getattr(state.engine_states[index], axis)
        limit = vehicle.engines[index].max_gimbal_rad
        target = float(np.clip(actual+change, -limit, limit))
        engines[index] = replace(engines[index], **{axis: target})
        clipped.append(target-actual)
    residual = desired-main_moment-matrix@np.asarray(clipped)
    capacity = 2*profile["actuators"]["rcs_radius_m"]*profile["actuators"]["rcs_thrust_n"]
    for index, engine in enumerate(vehicle.engines):
        if engine.name.startswith("rcs_"):
            _, axis, sign, _ = engine.name.split("_")
            demand = max(0., min(1., float(residual[int(axis)])*int(sign)/capacity))
            engines[index] = dyn.EngineCommand(demand > 1e-8, demand if demand > 1e-8 else 0.)
    info.update(predicted_residual_torque_body_nm=residual.tolist(),
                allocation_scope="current main thrust and local gimbal Jacobian; finite actuator tracking still required")
    return dyn.Command6DOF(tuple(engines), command.flap_angles_rad), {**diagnostic, "measured_tvc_allocation": info}


def control_with_measured_tvc(state, vehicle, target_q, main_throttle, main_count, profile, *, use_flaps=False,
                              development_fin_allocation=False, control_interval_s=.1, trim_angles_rad=None,
                              development_fin_policy="finite_regularized_fins_v1",
                              reference_rate_body_rad_s=None, reference_acceleration_body_rad_s=None):
    """Explicit candidate entry point; legacy controller remains unchanged."""
    from .starship_sixdof_mission import control
    command, diagnostic = control(state, vehicle, target_q, main_throttle, main_count, profile, use_flaps=use_flaps,
        development_fin_allocation=development_fin_allocation, control_interval_s=control_interval_s,
        trim_angles_rad=trim_angles_rad, development_fin_policy=development_fin_policy,
        reference_rate_body_rad_s=reference_rate_body_rad_s,
        reference_acceleration_body_rad_s=reference_acceleration_body_rad_s)
    return reallocate_measured_tvc(state, vehicle, command, diagnostic, profile)


def control_coast_stopping_distance(state, vehicle, target_q, profile, *, control_interval_s=.25):
    """Opt-in jet-limited slew with a finite-delay stopping-distance reference.

    Main engine commands stay off. The desired body rate follows the shortest
    quaternion error, with ``w*delay + w²/(2*alpha) <= remaining_angle``. The
    bound uses configured bidirectional jet authority and current inertia, not
    an assumed instantaneous attitude. Actual jet lag, fuel use, gimbals and
    dynamics remain unchanged. This is a generic development controller, not
    SpaceX software or a guarantee of aerodynamic trim near entry.
    """
    if (type(control_interval_s) not in (int, float) or not math.isfinite(control_interval_s)
            or not 0 < control_interval_s <= 1):
        raise ValueError("coast control interval must be finite and in (0, 1]")
    target_q = dyn.normalize_quaternion(target_q)
    observed = dyn.observe(state, vehicle)
    inertia = np.asarray(observed["inertia_kg_m2"])
    omega = np.asarray(state.omega_body_rad_s)
    inverse = (state.q_body_to_eci[0], *(-x for x in state.q_body_to_eci[1:]))
    error = np.asarray(dyn.quaternion_multiply(inverse, target_q))
    if error[0] < 0:
        error = -error
    vector_norm = float(np.linalg.norm(error[1:]))
    angle = 2*math.atan2(vector_norm, max(0., float(error[0])))
    axis = error[1:]/vector_norm if vector_norm > 1e-12 else np.zeros(3)
    capacities = np.zeros((3, 2))
    delays = [control_interval_s]
    for engine, actual in zip(vehicle.engines, state.engine_states):
        if engine.name.startswith("rcs_") and actual.available:
            _, coordinate, sign, _ = engine.name.split("_")
            index = int(coordinate)
            lever = np.asarray(engine.position_body_m)-observed["com_body_m"]
            torque = np.cross(lever, np.asarray(engine.direction_body)*engine.max_thrust_n)
            capacities[index, int(int(sign) > 0)] += abs(torque[index])
            delays.append(engine.throttle_time_constant_s+control_interval_s)
    limits = np.min(capacities, axis=1)
    inertia_bound = float(np.max(np.sum(np.abs(inertia), axis=1)))
    alpha = min(profile["guidance"]["max_angular_acceleration_rad_s2"], float(np.min(limits))/inertia_bound)
    delay = max(delays)
    speed = math.sqrt((alpha*delay)**2+2*alpha*angle)-alpha*delay
    desired_rate = axis*speed
    acceleration = (desired_rate-omega)/(2*delay)
    magnitude = float(np.linalg.norm(acceleration))
    if magnitude > alpha:
        acceleration *= alpha/magnitude
    wanted = (inertia@acceleration+np.cross(omega, inertia@omega)
              -np.asarray(observed["aero_torque_body_nm"])
              -np.asarray(observed["gravity_gradient_torque_body_nm"]))
    wanted = np.clip(wanted, -limits, limits)
    commands = []
    for engine, actual in zip(vehicle.engines, state.engine_states):
        command = dyn.EngineCommand()
        if engine.name.startswith("rcs_") and actual.available:
            _, coordinate, sign, _ = engine.name.split("_")
            index, direction = int(coordinate), int(sign)
            capacity = capacities[index, int(direction > 0)]
            throttle = float(max(0., min(1., float(wanted[index])*direction/max(capacity, 1e-30))))
            command = dyn.EngineCommand(throttle > 1e-8, throttle if throttle > 1e-8 else 0.)
        commands.append(command)
    command = dyn.Command6DOF(tuple(commands), state.flap_angles_rad)
    diagnostic = {"control_allocation": "coast_stopping_distance_v1", "target_q_body_to_eci": list(target_q),
                  "attitude_error_deg": math.degrees(angle), "requested_torque_body_nm": wanted.tolist(),
                  "coast_rate_reference_body_rad_s": desired_rate.tolist(), "coast_rate_limit_rad_s": speed,
                  "coast_stopping_alpha_rad_s2": alpha, "coast_actuator_delay_s": delay,
                  "coast_available_jet_torque_nm": limits.tolist(), "main_engine_commands_off": True}
    return reallocate_measured_tvc(state, vehicle, command, diagnostic, profile, observed=observed)
