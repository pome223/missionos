"""Opt-in finite fin commands before closing the local moment allocator.

Headroom is sampled in the existing frozen-pose/flow panel model. It is not a
continuous bound, an achieved RCS response, or entry/capture admission. Only
independent, force-balanced signed RCS pairs are supported conservatively.
"""
from __future__ import annotations

from dataclasses import replace
import math

from . import starship_physics as env
from . import starship_sixdof as dyn
from .starship_fin_allocation import actuator_endpoint

POLICY_ID = "finite_entry_pretrim_v1"
SCHEMA = "missionos.starship_entry_pretrim.v1"
CONFIG = {"maximum_dynamic_pressure_pa": 100., "near_trim_tolerance_deg": .1,
          "probe_fractions": [.2, .4, .6, .8, 1.], "maximum_probes": 5,
          "headroom_numeric_tolerance_nm": 1e-6, "group_relative_tolerance": 1e-9,
          "pair_throttle_tolerance": 1e-8}


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _vector(value, size):
    return isinstance(value, (tuple, list)) and len(value) == size and all(_number(x) for x in value)


def _error_class(error):
    name = type(error).__name__
    return name if name in {"ValueError", "TypeError", "ArithmeticError", "RuntimeError", "IndexError", "KeyError", "LinAlgError"} else "unexpected_prediction_error"


def _balanced_rcs_groups(state, body, command, centroid):
    groups = {(axis, sign): [] for axis in range(3) for sign in (-1, 1)}
    for index, (engine, actual) in enumerate(zip(body.engines, state.engine_states)):
        if not engine.name.startswith("rcs_"):
            continue
        parts = engine.name.split("_")
        if len(parts) != 4:
            raise ValueError("unsupported_rcs_geometry")
        axis, sign, side = (int(x) for x in parts[1:])
        if axis not in range(3) or sign not in (-1, 1) or side not in (-1, 1):
            raise ValueError("unsupported_rcs_geometry")
        force = env.scale(engine.direction_body, engine.max_thrust_n)
        lever = env.add(engine.position_body_m, env.scale(centroid, -1.))
        torque = env.cross(lever, force)
        groups[axis, sign].append((index, side, engine, actual, force, torque, command.engines[index]))
    capacities, receipts, unsupported = [[0., 0.] for _ in range(3)], [], False
    for (axis, sign), members in groups.items():
        available = [entry for entry in members if entry[3].available]
        forces = [sum(entry[4][i] for entry in available) for i in range(3)]
        torques = [sum(entry[5][i] for entry in available) for i in range(3)]
        complete_shape = len(members) == 2 and {entry[1] for entry in members} == {-1, 1}
        full = complete_shape and len(available) == 2
        force_scale = max(1., sum(env.norm(entry[4]) for entry in available))
        torque_scale = max(1., sum(env.norm(entry[5]) for entry in available))
        balanced = (full and env.norm(forces) <= CONFIG["group_relative_tolerance"]*force_scale
                    and env.norm([torques[i] if i != axis else 0. for i in range(3)])
                        <= CONFIG["group_relative_tolerance"]*torque_scale
                    and torques[axis]*sign > 0)
        matched_response = False
        matched_current = False
        matched_commands = False
        if full:
            left, right = available
            matched_response = all(getattr(left[2], key) == getattr(right[2], key) for key in
                                   ("throttle_time_constant_s", "throttle_rate_per_s", "max_thrust_n"))
            matched_current = abs(left[3].throttle-right[3].throttle) <= CONFIG["pair_throttle_tolerance"]
            matched_commands = left[6] == right[6]
        supported = bool(balanced and matched_response and matched_current and matched_commands)
        # An entirely unavailable pair is an absent channel, not a coupled
        # channel. Missing one mate or mismatched geometry is unsupported.
        if not complete_shape or available and not supported:
            unsupported = True
        capacity = abs(torques[axis]) if supported else 0.
        capacities[axis][int(sign > 0)] = float(capacity)
        receipts.append({"axis": axis, "sign": sign, "engine_indices": [entry[0] for entry in members],
            "available_indices": [entry[0] for entry in available], "force_sum_body_n": [float(x) for x in forces],
            "torque_sum_body_nm": [float(x) for x in torques], "complete_pair_shape": complete_shape,
            "full_group_available": full, "force_and_cross_axis_balanced": bool(balanced),
            "matched_response_parameters": matched_response, "matched_actual_throttle": matched_current,
            "matched_base_commands": matched_commands, "supported_group": supported, "capacity_nm": float(capacity)})
    return capacities, receipts, unsupported


def prepare_entry_fins(state, booster, baseCommand, baseDiagnostic, profile, trim_angles_rad, *, interval_s):
    """Return real fin commands only when every sampled corrective demand fits.

    The caller supplies/later labels its future-entry or current-governed trim.
    This helper validates its shape/physical limits, not its flight provenance.
    All base engine/RCS commands are preserved. Prepared means input angles are
    actually near trim and all local guards passed, never predicted readiness.
    """
    receipt = {"schema": SCHEMA, "policy_id": POLICY_ID, "configuration": {**CONFIG,
        "probe_fractions": list(CONFIG["probe_fractions"])}, "time_s": getattr(state, "time_s", None),
        "interval_s": float(interval_s) if _number(interval_s) else None,
        "status": "deferred", "reason": "invalid_base_reference", "prepared": False,
        "actual_near_trim": False, "reference_validated_for_shape_limits_only": False,
        "probe_attempted_count": 0, "probe_completed_count": 0, "probe_failed_count": 0, "probes": [],
        "baseline_observation_attempted": False, "baseline_observation_completed": False,
        "base_engine_commands_preserved": True, "actual_state_assigned": False,
        "prediction_is_execution": False, "production_policy_admitted": False,
        "arrival_admitted": False, "support_admitted": False,
        "rcs_authority_scope": "six_independent_force_balanced_signed_pairs_only",
        "headroom_scope": "five frozen-pose/flow samples; not continuous or achieved dynamic response"}

    def defer(reason, error=None):
        receipt["reason"] = reason
        if error is not None:
            receipt["error_class"] = _error_class(error)
        return baseCommand, receipt

    if not _number(interval_s) or not 0 < interval_s <= .25:
        return defer("invalid_interval")
    if (type(state) is not dyn.State6DOF or type(booster) is not dyn.Vehicle6DOF
            or type(baseCommand) is not dyn.Command6DOF or not isinstance(baseDiagnostic, dict)
            or not isinstance(profile, dict) or profile.get("schema") != "missionos.starship_sixdof_profile.v1"
            or len(state.engine_states) != len(booster.engines)
            or len(baseCommand.engines) != len(booster.engines)
            or len(state.flap_angles_rad) != len(booster.aero_panels)
            or len(baseCommand.flap_angles_rad) != len(booster.aero_panels)
            or baseDiagnostic.get("control_allocation") != "coast_stopping_distance_v1"
            or baseDiagnostic.get("main_engine_commands_off") is not True
            or not _vector(baseDiagnostic.get("target_q_body_to_eci"), 4)
            or abs(sum(x*x for x in baseDiagnostic["target_q_body_to_eci"])-1.) > 1e-8
            or not _vector(baseDiagnostic.get("requested_torque_body_nm"), 3)
            or any(command.enabled or command.throttle != 0. for engine, command in
                   zip(booster.engines, baseCommand.engines) if not engine.name.startswith("rcs_"))):
        return defer("invalid_base_reference")
    indices = [i for i, panel in enumerate(booster.aero_panels) if panel.name.startswith("grid_fin_")]
    if (not 1 <= len(indices) <= 4 or not _vector(trim_angles_rad, len(booster.aero_panels))
            or any(abs(value) > panel.max_deflection_rad+1e-12 for panel, value in zip(booster.aero_panels, trim_angles_rad))):
        return defer("invalid_trim_reference")
    trim = [float(x) for x in trim_angles_rad]
    actual = list(state.flap_angles_rad)
    measured_error = max(abs(actual[i]-trim[i]) for i in indices)
    receipt.update(fin_indices=indices, actual_flap_angles_rad=actual, declared_trim_angles_rad=trim,
                   measured_trim_error_deg=math.degrees(measured_error),
                   actual_near_trim=measured_error <= math.radians(CONFIG["near_trim_tolerance_deg"]),
                   reference_validated_for_shape_limits_only=True,
                   base_coast_torque_demand_body_nm=[float(x) for x in baseDiagnostic["requested_torque_body_nm"]])
    receipt["baseline_observation_attempted"] = True
    try:
        observed = dyn.observe(state, booster, baseCommand)
        receipt["baseline_observation_completed"] = True
        pressure = observed["dynamic_pressure_pa"]
        current_moment = observed["aero_torque_body_nm"]
        if not _number(pressure) or pressure < 0 or not _vector(current_moment, 3):
            return defer("baseline_observation_failed")
        current_moment = [float(x) for x in current_moment]
        receipt.update(dynamic_pressure_pa=float(pressure), current_aero_torque_body_nm=current_moment,
                       current_com_body_m=[float(x) for x in observed["com_body_m"]])
        if pressure > CONFIG["maximum_dynamic_pressure_pa"]:
            return defer("outside_low_dynamic_pressure")
        capacities, groups, unsupported = _balanced_rcs_groups(state, booster, baseCommand, observed["com_body_m"])
        receipt.update(rcs_signed_capacity_nm=capacities, rcs_groups=groups)
        if unsupported:
            return defer("unsupported_or_unbalanced_rcs_group")
        if any(value <= 0. for pair in capacities for value in pair):
            return defer("unavailable_bidirectional_rcs")
        tvc = baseDiagnostic.get("measured_tvc_allocation") or {}
        demand = tvc.get("predicted_residual_torque_body_nm") if tvc.get("applied") is True else baseDiagnostic["requested_torque_body_nm"]
        if not _vector(demand, 3):
            return defer("invalid_base_reference")
        demand = [float(x) for x in demand]
        receipt.update(base_rcs_torque_demand_body_nm=demand,
            base_rcs_demand_source="measured_tvc_local_residual" if tvc.get("applied") is True else "coast_requested_torque")
        for group in groups:
            axis, sign, capacity = group["axis"], group["sign"], group["capacity_nm"]
            expected = max(0., min(1., demand[axis]*sign/capacity))
            expected = expected if expected > 1e-8 else 0.
            for index in group["engine_indices"]:
                command = baseCommand.engines[index]
                if command.enabled != (expected > 0.) or abs(command.throttle-expected) > 1e-8:
                    return defer("inconsistent_base_rcs_command")
    except Exception as error:
        return defer("baseline_observation_failed", error)
    exceeded = False
    for fraction in CONFIG["probe_fractions"]:
        receipt["probe_attempted_count"] += 1
        probe = {"fraction": fraction, "elapsed_s": fraction*interval_s, "status": "attempted"}
        receipt["probes"].append(probe)
        try:
            angles = list(actual)
            for index in indices:
                panel = booster.aero_panels[index]
                angles[index] = actuator_endpoint(actual[index], trim[index], panel.deflection_time_constant_s,
                                                  panel.deflection_rate_rad_s, fraction*interval_s)
            predicted = dyn.observe(replace(state, flap_angles_rad=tuple(angles)), booster)
            moment = predicted["aero_torque_body_nm"]
            if not _vector(moment, 3):
                raise ValueError("nonfinite_probe_load")
            moment = [float(x) for x in moment]
            increment = [moment[i]-current_moment[i] for i in range(3)]
            required = [demand[i]-increment[i] for i in range(3)]
            margins = [min(capacities[i][1]-required[i], capacities[i][0]+required[i]) for i in range(3)]
            within = all(value >= -CONFIG["headroom_numeric_tolerance_nm"] for value in margins)
            exceeded |= not within
            probe.update(status="completed", predicted_flap_angles_rad=angles,
                predicted_aero_torque_body_nm=list(moment), predicted_aero_increment_body_nm=increment,
                required_rcs_torque_body_nm=required, rcs_headroom_margin_nm=margins,
                sampled_headroom_met=within, pose_flow_and_clock_held=True)
            receipt["probe_completed_count"] += 1
        except Exception as error:
            probe.update(status="failed", error_class=_error_class(error))
            receipt["probe_failed_count"] += 1
            return defer("prediction_failure", error)
    if exceeded:
        return defer("sampled_rcs_headroom_exceeded")
    receipt.update(status="accepted", reason="sampled_corrective_headroom_met",
                   prepared=receipt["actual_near_trim"], command_flap_angles_rad=trim)
    return dyn.Command6DOF(baseCommand.engines, tuple(trim)), receipt
