"""Demand-specific coast bandwidth from finite, current aerodynamic surfaces.

This is a local command-feasibility calculation, not an achieved torque or a
stability proof.  It never advances a plant, changes a state, or creates an
actuator.  The existing jet/tail-only profile remains the fallback.  An extra
bandwidth is admitted only when the *same* bounded fin allocation used by the
caller leaves the recorded PD demand inside the available signed jet envelope
after a nonlinear, frozen-flow endpoint load calculation.
"""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
import math

import numpy as np

from . import starship_sixdof as dyn
from .starship_fin_allocation import (
    MOMENT_PRIORITY_POLICY_ID,
    POLICY_ID,
    REGULARIZATION,
    actuator_endpoint,
    bounded_least_squares,
    closest_trim_with_primary_image,
)

SCHEMA = "missionos.booster_finite_coast_authority.v1"
POLICY = "actual_pd_finite_endpoint_v1"
MAX_ALPHA_RAD_S2 = .03
# A fixed search for a usable local demand, not an optimizer or a tuning loop.
CAP_FRACTIONS = (1., .5, .25, .125, .0625, .03125, .015625)


def _number(value, *, positive=False):
    return (type(value) in (int, float) and math.isfinite(value)
        and (not positive or value > 0))


def _jet_capacities(state, craft, observed, interval):
    """Only complete, available, force-cancelling signed hardware pairs."""
    capacity = np.zeros((3, 2))
    lower, upper = np.zeros(3), np.zeros(3)
    paired = True
    props = np.asarray(observed["com_body_m"])
    for axis in range(3):
        for sign in (-1, 1):
            indices = [i for i, engine in enumerate(craft.engines)
                if engine.name.startswith(f"rcs_{axis}_{sign}_")]
            if len(indices) != 2:
                paired = False
                continue
            available = [state.engine_states[i].available for i in indices]
            if state.propellant_kg <= 0 or not any(available):
                continue
            if not all(available):
                paired = False
                continue
            forces = [np.asarray(craft.engines[i].direction_body)*craft.engines[i].max_thrust_n
                for i in indices]
            force = np.sum(forces, axis=0)
            torque = np.sum([np.cross(np.asarray(craft.engines[i].position_body_m)-props, f)
                for i, f in zip(indices, forces)], axis=0)
            expected = np.zeros(3)
            expected[axis] = sign*abs(torque[axis])
            if (np.linalg.norm(force) > 1e-7 or not np.allclose(torque, expected, rtol=1e-12, atol=1e-7)):
                raise ValueError("coast_authority_requires_axis_aligned_force_cancelling_jet_pairs")
            capacity[axis, int(sign > 0)] = abs(torque[axis])
            endpoints = []
            for i in indices:
                engine, actual = craft.engines[i], state.engine_states[i]
                endpoints.append([actuator_endpoint(actual.throttle, target,
                    engine.throttle_time_constant_s, engine.throttle_rate_per_s, interval)
                    for target in (0., 1.)])
            # Identical pair response keeps its force and parasitic moments
            # cancelled.  A mismatched/failed half-pair needs a full wrench
            # treatment; it must not borrow this axis-box admission.
            if not np.allclose(endpoints[0], endpoints[1], rtol=0, atol=1e-12):
                paired = False
                continue
            lo, hi = abs(torque[axis])*np.asarray(endpoints[0])
            lower[axis] += lo if sign > 0 else -hi
            upper[axis] += hi if sign > 0 else -lo
    return capacity, lower, upper, paired


def _profile(profile, alpha):
    frequency = min(profile["guidance"]["attitude_frequency_rad_s"], math.sqrt(alpha/math.pi))
    return {**profile, "guidance": {**profile["guidance"],
        "max_angular_acceleration_rad_s2": alpha, "attitude_frequency_rad_s": frequency}}


def _pd(state, target, guidance, inertia):
    inverse = (state.q_body_to_eci[0], *(-x for x in state.q_body_to_eci[1:]))
    error = dyn.quaternion_multiply(inverse, target)
    if error[0] < 0:
        error = tuple(-x for x in error)
    omega = np.asarray(state.omega_body_rad_s)
    frequency = guidance["attitude_frequency_rad_s"]
    alpha = np.clip(2*frequency**2*np.asarray(error[1:])
        -2*guidance["attitude_damping_ratio"]*frequency*omega,
        -guidance["max_angular_acceleration_rad_s2"],
        guidance["max_angular_acceleration_rad_s2"])
    return alpha, inertia@alpha+np.cross(omega, inertia@omega)


def _fin_geometry(state, craft, observed, interval, trim):
    indices = [i for i, panel in enumerate(craft.aero_panels)
        if panel.name.startswith("grid_fin_") and panel.max_deflection_rad > 0]
    if not 1 <= len(indices) <= 4:
        raise ValueError("finite_coast_authority_requires_one_to_four_actual_grid_fins")
    if any(panel.name.startswith("flap_") for panel in craft.aero_panels):
        raise ValueError("booster_finite_authority_cannot_borrow_ship_flaps")
    actual = np.asarray([state.flap_angles_rad[i] for i in indices])
    limits = np.asarray([craft.aero_panels[i].max_deflection_rad for i in indices])
    if trim is None:
        reference = actual.copy()
    else:
        if (len(trim) != len(craft.aero_panels)
                or any(not _number(x) for x in trim)
                or any(abs(trim[i]) > limits[j]+1e-10 for j, i in enumerate(indices))):
            raise ValueError("invalid_finite_coast_trim_reference")
        reference = np.asarray([trim[i] for i in indices])
    columns, lower, upper = [], [], []
    for i in indices:
        panel, angle = craft.aero_panels[i], state.flap_angles_rad[i]
        lo, hi = max(-panel.max_deflection_rad, angle-.001), min(panel.max_deflection_rad, angle+.001)
        if hi <= lo:
            raise ValueError("finite_coast_fin_has_no_physical_angle_range")
        moments = []
        for value in (lo, hi):
            angles = list(state.flap_angles_rad)
            angles[i] = value
            moments.append(np.asarray(dyn.observe(replace(state, flap_angles_rad=tuple(angles)), craft)["aero_torque_body_nm"]))
        columns.append((moments[1]-moments[0])/(hi-lo))
        lower.append(actuator_endpoint(angle, -panel.max_deflection_rad,
            panel.deflection_time_constant_s, panel.deflection_rate_rad_s, interval)-angle)
        upper.append(actuator_endpoint(angle, panel.max_deflection_rad,
            panel.deflection_time_constant_s, panel.deflection_rate_rad_s, interval)-angle)
    return (indices, actual, limits, reference, np.asarray(columns).T,
        np.asarray(lower), np.asarray(upper))


def _endpoint(geometry, demand, jet_scale, policy):
    _, actual, limits, trim, matrix, lower, upper = geometry
    # These are the exact numerical objectives in allocate_fins.  No control
    # routine or allocator entrypoint is invoked by this feasibility helper.
    scale = np.maximum(jet_scale, np.sum(np.abs(matrix)*limits, axis=1))
    scale = np.maximum(scale, 1.)
    if policy == POLICY_ID:
        lhs = np.vstack((matrix/scale[:, None], REGULARIZATION*np.diag(1/limits)))
        rhs = np.concatenate((demand/scale, REGULARIZATION*(trim-actual)/limits))
        delta = bounded_least_squares(lhs, rhs, lower, upper)
    else:
        lhs, rhs = matrix/scale[:, None], demand/scale
        primary = bounded_least_squares(lhs, rhs, lower, upper)
        delta = closest_trim_with_primary_image(lhs, lhs@primary, trim-actual, limits, lower, upper)
    return actual+delta


def finite_coast_control_profile(state, craft, target_q, profile, *, control_interval_s,
        observed=None, trim_angles_rad=None, fin_policy=POLICY_ID):
    """Return a new profile and a local, demand-specific authority receipt.

    The caller must opt into this helper in coast only and must use the same
    target, fin policy, trim and interval for its real finite control request.
    Signed RCS *hardware* capacity bounds the predicted residual; finite jet,
    gimbal, fin and attitude tracking still happen in the actual plant.  The
    result is neither a universal axis authority nor proof of achieved alpha.
    """
    if (not _number(control_interval_s, positive=True) or control_interval_s > .25
            or fin_policy not in (POLICY_ID, MOMENT_PRIORITY_POLICY_ID)):
        raise ValueError("invalid_finite_coast_authority_scope")
    target = dyn.normalize_quaternion(tuple(target_q))
    guidance = profile["guidance"]
    for key in ("attitude_frequency_rad_s", "max_angular_acceleration_rad_s2", "attitude_damping_ratio"):
        if not _number(guidance[key], positive=True):
            raise ValueError("invalid_finite_coast_authority_guidance")
    # Refresh static loads from the complete actual state, including finite
    # actuators and body rate.  A caller observation is only a consistency
    # witness, never the source of authority.  This is not a plant step.
    fresh = dyn.observe(state, craft)
    if observed is not None:
        for key in ("time_s", "r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s",
                "propellant_kg", "com_body_m", "inertia_kg_m2", "aero_torque_body_nm",
                "thrust_torque_body_nm", "aero_force_body_n", "thrust_force_body_n"):
            if not np.array_equal(observed[key], fresh[key]):
                raise ValueError("finite_coast_authority_observation_is_not_actual_state")
        for family in ("engine_loads", "panel_loads"):
            if len(observed[family]) != len(fresh[family]):
                raise ValueError("finite_coast_authority_observation_is_not_actual_state")
            for old, actual in zip(observed[family], fresh[family]):
                for key in ("force_body_n", "torque_body_nm"):
                    if not np.array_equal(old[key], actual[key]):
                        raise ValueError("finite_coast_authority_observation_is_not_actual_state")
    observed = fresh
    inertia = np.asarray(observed["inertia_kg_m2"])
    inertia_bound = float(np.max(np.sum(np.abs(inertia), axis=1)))
    caps, jet_lower, jet_upper, paired_jets = _jet_capacities(state, craft, observed, control_interval_s)
    main_moment = np.zeros(3)
    tvc_capacity = 0.
    for engine, actual, load in zip(craft.engines, state.engine_states, observed["engine_loads"]):
        if engine.name.startswith("rcs_"):
            continue
        main_moment += np.asarray(load["torque_body_nm"])
        if actual.available and engine.max_gimbal_rad > 0:
            tvc_capacity += load["thrust_n"]*abs(engine.position_body_m[2]-observed["com_body_m"][2])*math.sin(engine.max_gimbal_rad)
    ceiling = min(MAX_ALPHA_RAD_S2, guidance["max_angular_acceleration_rad_s2"])
    jet_capacity = float(np.min(caps))
    if jet_capacity <= 0:
        raise ValueError("finite_coast_authority_requires_fuel_and_complete_bidirectional_jet_pairs")
    baseline = min(ceiling, (jet_capacity+tvc_capacity)/inertia_bound)
    result = _profile(profile, baseline)
    receipt = {"schema": SCHEMA, "policy_id": POLICY, "time_s": float(state.time_s),
        "control_interval_s": float(control_interval_s), "fin_policy_id": fin_policy,
        "dynamic_pressure_pa": float(observed["dynamic_pressure_pa"]),
        "baseline_angular_acceleration_limit_rad_s2": float(baseline),
        "configured_angular_acceleration_ceiling_rad_s2": float(ceiling),
        "current_main_torque_body_nm": main_moment.tolist(),
        "current_gimballed_main_torque_capacity_nm": float(tvc_capacity),
        "available_signed_jet_torque_nm": caps.tolist(),
        "finite_jet_lower_torque_nm": jet_lower.tolist(),
        "finite_jet_upper_torque_nm": jet_upper.tolist(),
        "finite_jet_pair_scope_valid": bool(paired_jets),
        "source_actuator_sha256": sha256(json.dumps({
            "engines": [[bool(e.available), float(e.throttle), float(e.gimbal_x_rad), float(e.gimbal_y_rad)]
                for e in state.engine_states],
            "fins": [float(x) for x in state.flap_angles_rad]}, sort_keys=True, separators=(",", ":"),
            allow_nan=False).encode()).hexdigest(),
        "additional_fin_authority_admitted": False, "candidate_count": 0,
        "selection": "jet_and_current_shutdown_tail_fallback",
        "prediction_is_execution": False, "actual_state_assigned": False,
        "physics_limits_changed": False,
        "authority_scope": "same current-state PD demand, frozen-flow finite-fin endpoint and reachable signed finite-jet-pair envelope; actual command tracking and stability unverified"}
    # Strong shutdown-tail allocation has a different finite-TVC solve; leave
    # it to the existing explicit policy rather than invent combined authority.
    if (observed["dynamic_pressure_pa"] <= 100. or tvc_capacity > jet_capacity
            or baseline >= ceiling or not np.all(caps > 0) or not paired_jets):
        receipt["selection"] = ("strong_shutdown_tail_fallback" if tvc_capacity > jet_capacity
            else "low_pressure_jet_fallback" if observed["dynamic_pressure_pa"] <= 100.
            else "no_additional_finite_fin_scope")
    else:
        geometry = _fin_geometry(state, craft, observed, control_interval_s, trim_angles_rad)
        receipt.update(fin_indices=geometry[0], actual_fin_angles_rad=geometry[1].tolist(),
            reachable_fin_delta_lower_rad=geometry[-2].tolist(),
            reachable_fin_delta_upper_rad=geometry[-1].tolist(),
            effectiveness_nm_per_rad=geometry[4].tolist())
        aero = np.asarray(observed["aero_torque_body_nm"])
        # The production allocator normalizes by the nominal physical jet pair
        # floor.  Admission additionally checks availability in caps above.
        jet_scale = 2*profile["actuators"]["rcs_radius_m"]*profile["actuators"]["rcs_thrust_n"]
        for fraction in CAP_FRACTIONS:
            cap = baseline+(ceiling-baseline)*fraction
            candidate = _profile(profile, cap)
            alpha, rigid = _pd(state, target, candidate["guidance"], inertia)
            endpoints = _endpoint(geometry, rigid-aero, jet_scale, fin_policy)
            angles = list(state.flap_angles_rad)
            for index, endpoint in zip(geometry[0], endpoints):
                angles[index] = float(endpoint)
            endpoint_aero = np.asarray(dyn.observe(replace(state, flap_angles_rad=tuple(angles)), craft)["aero_torque_body_nm"])
            residual = rigid-endpoint_aero-main_moment
            receipt["candidate_count"] += 1
            if np.all(residual >= jet_lower-1e-7) and np.all(residual <= jet_upper+1e-7):
                result = candidate
                receipt.update(additional_fin_authority_admitted=True,
                    selection="actual_pd_demand_finite_endpoint_accepted",
                    requested_acceleration_body_rad_s2=alpha.tolist(),
                    requested_rigid_torque_body_nm=rigid.tolist(),
                    predicted_fin_endpoint_angles_rad=endpoints.tolist(),
                    predicted_endpoint_aero_torque_body_nm=endpoint_aero.tolist(),
                    predicted_jet_residual_torque_body_nm=residual.tolist())
                break
    receipt.update(coast_control_frequency_rad_s=float(result["guidance"]["attitude_frequency_rad_s"]),
        coast_angular_acceleration_limit_rad_s2=float(result["guidance"]["max_angular_acceleration_rad_s2"]))
    return result, receipt
