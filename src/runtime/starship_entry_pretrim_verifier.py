"""Independent record arithmetic for scoped finite entry-pretrim preparation.

This module does not import the fin producer, dynamics or control allocator.
Frozen-flow panel probes are retained predictions, not certified physical loads.
"""
from __future__ import annotations

import math

from .starship_booster_recovery_verifier import (
    _cross, _flow_and_centroid, _near, _norm, _require, _vector, _number,
    _same_value, _compare_vector, _prediction_origin,
)

POLICY_ID = "finite_entry_pretrim_v1"
CONFIG = {"maximum_dynamic_pressure_pa": 100., "near_trim_tolerance_deg": .1,
          "probe_fractions": [.2, .4, .6, .8, 1.], "maximum_probes": 5,
          "headroom_numeric_tolerance_nm": 1e-6, "group_relative_tolerance": 1e-9,
          "pair_throttle_tolerance": 1e-8}


def _balanced_rcs_groups(state, command, profile):
    """Reconstruct the declared six balanced pairs around the current centroid.

    This accepts only the repository's named signed-axis paired-jet design.
    It is not a general vector-thruster reachability or hardware certificate.
    """
    _, centroid, _, _ = _flow_and_centroid(state, profile)
    radius, thrust = profile["actuators"]["rcs_radius_m"], profile["actuators"]["rcs_thrust_n"]
    actual, requested = state["engine_states"], command["engines"]
    groups, capacities, unsupported, index = [], [[0., 0.] for _ in range(3)], False, profile["booster"]["engine_count"]
    for axis in range(3):
        for sign in (-1, 1):
            indices, total_force, total_moment = [], [0., 0., 0.], [0., 0., 0.]
            for side in (-1, 1):
                position = [0., 0., profile["booster"]["dry_com_z_m"]]
                position[(axis+1) % 3] += side*radius
                direction = [0., 0., 0.]
                direction[(axis+2) % 3] = side*sign
                lever = [position[i]-centroid[i] for i in range(3)]
                force = [component*thrust for component in direction]
                moment = _cross(lever, force)
                if actual[index]["available"]:
                    total_force = [a+b for a, b in zip(total_force, force)]
                    total_moment = [a+b for a, b in zip(total_moment, moment)]
                indices.append(index)
                index += 1
            first, second = (actual[i] for i in indices)
            a, b = (requested[i] for i in indices)
            available = first["available"] and second["available"]
            missing_mate = first["available"] is not second["available"]
            balanced = (available and _norm(total_force) <= 1e-6
                        and _norm([total_moment[i] for i in range(3) if i != axis]) <= 1e-6
                        and sign*total_moment[axis] > 0)
            matched_current = available and _near(first["throttle"], second["throttle"], 1e-8)
            matched_commands = available and _same_value(a, b)
            supported = bool(balanced and matched_current and matched_commands)
            unsupported = unsupported or missing_mate or available and not supported
            capacity = abs(total_moment[axis]) if supported else 0.
            capacities[axis][int(sign > 0)] = capacity
            groups.append({"axis": axis, "sign": sign, "engine_indices": indices,
                "available_indices": [i for i in indices if actual[i]["available"]],
                "force_sum_body_n": total_force, "torque_sum_body_nm": total_moment,
                "complete_pair_shape": True, "full_group_available": bool(available),
                "force_and_cross_axis_balanced": bool(balanced), "matched_response_parameters": bool(available),
                "matched_actual_throttle": bool(matched_current), "matched_base_commands": bool(matched_commands),
                "supported_group": supported, "capacity_nm": capacity})
    return capacities, groups, bool(unsupported)


def _endpoint(actual, target, tau, rate, elapsed):
    distance = abs(target-actual)
    saturation = max(0., (distance-rate*tau)/rate)
    movement = rate*elapsed if elapsed <= saturation else distance-min(distance, rate*tau)*math.exp(-(elapsed-saturation)/tau)
    return actual+math.copysign(movement, target-actual)


def verify_pretrim_checkpoint(checkpoint, sample, profile, *, expected_trim_angles=None, remaining_s=None):
    """Check a low-q command receipt; return actual measured readiness only."""
    navigation, state, command = checkpoint["navigation"], checkpoint["state"], checkpoint["command"]
    item = navigation.get("entry_pretrim")
    _require(type(item) is dict and item.get("schema") == "missionos.starship_entry_pretrim.v1"
             and item.get("policy_id") == POLICY_ID and _same_value(item.get("configuration"), CONFIG)
             and _same_value(item, sample.get("controller", {}).get("entry_pretrim"))
             and item.get("time_s") == state["time_s"] and command is not None,
             "entry_pretrim", "Missing scoped pretrim receipt bound to the actual checkpoint clock")
    _require(all(item.get(key) is False for key in ("actual_state_assigned", "prediction_is_execution",
                 "production_policy_admitted", "arrival_admitted", "support_admitted"))
             and item.get("base_engine_commands_preserved") is True
             and item.get("rcs_authority_scope") == "six_independent_force_balanced_signed_pairs_only"
             and item.get("headroom_scope") == "five frozen-pose/flow samples; not continuous or achieved dynamic response",
             "entry_pretrim", "Frozen-flow preparation cannot prescribe state or establish flight authority")
    interval = item.get("interval_s")
    powered = any(engine["enabled"] for engine in command["engines"][:33])
    _require(not powered and _number(interval) and 0 < interval <= .25
             and navigation.get("control_allocation") == "coast_stopping_distance_v1"
             and navigation.get("main_engine_commands_off") is True
             and _vector(navigation.get("target_q_body_to_eci"), 4)
             and _near(_norm(navigation["target_q_body_to_eci"]), 1., 1e-8)
             and _vector(navigation.get("requested_torque_body_nm")),
             "entry_pretrim", "Preparation must preserve a bounded RCS-only base reference")
    maximum_interval = .1 if _prediction_origin(state, profile)[2] < 100000. else .25
    _require(interval <= maximum_interval+1e-9 and (remaining_s is None or
             _number(remaining_s) and remaining_s > 0 and _near(interval, min(maximum_interval, remaining_s), 1e-9)),
             "entry_pretrim", "Finite preparation interval differs from the actual macrostep clock")
    probes = item.get("probes")
    attempted, completed, failed = (item.get(key) for key in
        ("probe_attempted_count", "probe_completed_count", "probe_failed_count"))
    _require(type(probes) is list and all(type(value) is int and value >= 0 for value in (attempted, completed, failed))
             and len(probes) == attempted == completed+failed and attempted <= 5,
             "entry_pretrim", "Failed and returned probes exceed the bounded attempt budget")
    _require(type(item.get("prepared")) is bool and type(item.get("actual_near_trim")) is bool
             and type(item.get("baseline_observation_attempted")) is bool and type(item.get("baseline_observation_completed")) is bool
             and item.get("status") in ("accepted", "deferred"),
             "entry_pretrim", "Missing explicit observed readiness or command disposition")
    status, reason = item["status"], item.get("reason")
    actual, indices = state["flap_angles_rad"], list(range(3, len(state["flap_angles_rad"])))
    limit, tau, rate = (math.radians(profile["actuators"]["grid_fin_limit_deg"]),
                        profile["actuators"]["flap_tau_s"], math.radians(profile["actuators"]["flap_rate_deg_s"]))
    valid_reference = item.get("reference_validated_for_shape_limits_only") is True
    if not valid_reference:
        _require(status == "deferred" and reason == "invalid_trim_reference" and attempted == 0
                 and item["prepared"] is False and item["actual_near_trim"] is False
                 and item["baseline_observation_attempted"] is False and item["baseline_observation_completed"] is False
                 and _same_value(command["flap_angles_rad"], actual)
                 and expected_trim_angles is None,
                 "entry_pretrim", "An invalid/missing trim goal cannot move fins or create readiness")
        return False
    trim = item.get("declared_trim_angles_rad")
    _require(item.get("fin_indices") == indices and _vector(trim, len(actual))
             and all(abs(value) <= (0. if index < 3 else limit)+1e-12 for index, value in enumerate(trim))
             and _same_value(item.get("actual_flap_angles_rad"), actual)
             and (expected_trim_angles is None or _same_value(trim, expected_trim_angles)),
             "entry_pretrim", "Trim request differs from the actual angles, physical limits or declared driver reference")
    measured = max(abs(actual[index]-trim[index]) for index in indices)
    near = measured <= math.radians(.1)
    _require(_near(item.get("measured_trim_error_deg"), math.degrees(measured), 1e-8)
             and item["actual_near_trim"] is near
             and _same_value(item.get("base_coast_torque_demand_body_nm"), navigation["requested_torque_body_nm"]),
             "entry_pretrim", "Actual fin readiness was replaced by a predicted endpoint or unbound demand")
    if item.get("baseline_observation_completed") is not True:
        _require(status == "deferred" and reason == "baseline_observation_failed" and attempted == 0
                 and item["baseline_observation_attempted"] is True
                 and item["prepared"] is False and _same_value(command["flap_angles_rad"], actual),
                 "entry_pretrim", "Failed baseline observation cannot move fins or prepare entry")
        return False
    _require(item.get("baseline_observation_attempted") is True
             and _number(sample.get("dynamic_pressure_pa")) and sample["dynamic_pressure_pa"] >= 0
             and _near(item.get("dynamic_pressure_pa"), sample.get("dynamic_pressure_pa"), 1e-6),
             "entry_pretrim", "Pretrim pressure does not match the actual sampled flow")
    _compare_vector(item.get("current_aero_torque_body_nm"), sample.get("aero_torque_body_nm"), "entry_pretrim", tolerance=1e-4)
    _, centroid, _, _ = _flow_and_centroid(state, profile)
    _compare_vector(item.get("current_com_body_m"), centroid, "entry_pretrim")
    if item["dynamic_pressure_pa"] > 100.:
        _require(status == "deferred" and reason == "outside_low_dynamic_pressure" and attempted == 0
                 and item["prepared"] is False and _same_value(command["flap_angles_rad"], actual),
                 "entry_pretrim", "High-q observation cannot establish low-q preparation")
        return False
    capacities, expected_groups, unsupported = _balanced_rcs_groups(state, command, profile)
    recorded_groups = item.get("rcs_groups")
    _require(type(recorded_groups) is list and len(recorded_groups) == 6,
             "entry_pretrim", "RCS preparation requires six independent balanced signed pairs")
    for recorded, expected in zip(recorded_groups, expected_groups):
        for key in ("axis", "sign", "engine_indices", "available_indices", "complete_pair_shape", "full_group_available",
                    "force_and_cross_axis_balanced", "matched_response_parameters", "matched_actual_throttle", "matched_base_commands", "supported_group"):
            _require(_same_value(recorded.get(key), expected[key]), "entry_pretrim", "Paired RCS geometry, response or availability differs from actual state/command")
        _compare_vector(recorded.get("force_sum_body_n"), expected["force_sum_body_n"], "entry_pretrim", tolerance=1e-6)
        _compare_vector(recorded.get("torque_sum_body_nm"), expected["torque_sum_body_nm"], "entry_pretrim", tolerance=1e-6)
        _require(_near(recorded.get("capacity_nm"), expected["capacity_nm"], 1e-6), "entry_pretrim", "RCS pair capacity differs from declared current geometry")
    _require(type(item.get("rcs_signed_capacity_nm")) is list and len(item["rcs_signed_capacity_nm"]) == 3,
             "entry_pretrim", "Missing signed RCS headroom capacities")
    for pair, expected in zip(item["rcs_signed_capacity_nm"], capacities):
        _compare_vector(pair, expected, "entry_pretrim", tolerance=1e-6)
    if unsupported or any(value <= 0 for pair in capacities for value in pair):
        expected_reason = "unsupported_or_unbalanced_rcs_group" if unsupported else "unavailable_bidirectional_rcs"
        _require(status == "deferred" and reason == expected_reason and attempted == 0
                 and item["prepared"] is False and _same_value(command["flap_angles_rad"], actual),
                 "entry_pretrim", "Unsupported/missing balanced RCS authority cannot prepare or change fin requests")
        return False
    tvc = navigation.get("measured_tvc_allocation") or {}
    applied = tvc.get("applied") is True
    demand = tvc.get("predicted_residual_torque_body_nm") if applied else navigation["requested_torque_body_nm"]
    _require(_vector(demand) and _same_value(item.get("base_rcs_torque_demand_body_nm"), demand)
             and item.get("base_rcs_demand_source") == ("measured_tvc_local_residual" if applied else "coast_requested_torque"),
             "entry_pretrim", "Base RCS command must use its declared coast or measured-TVC residual demand")
    inconsistent = False
    for group in expected_groups:
        expected = max(0., min(1., demand[group["axis"]]*group["sign"]/group["capacity_nm"]))
        expected = expected if expected > 1e-8 else 0.
        for index in group["engine_indices"]:
            jet = command["engines"][index]
            inconsistent |= jet["enabled"] is not (expected > 0.) or not _near(jet["throttle"], expected, 1e-8)
    if inconsistent:
        _require(status == "deferred" and reason == "inconsistent_base_rcs_command" and attempted == 0
                 and item["prepared"] is False and _same_value(command["flap_angles_rad"], actual),
                 "entry_pretrim", "A mismatched base command/diagnostic cannot prepare or change fins")
        return False
    exceeded, actual_failed = False, 0
    for index, probe in enumerate(probes):
        fraction = CONFIG["probe_fractions"][index]
        _require(type(probe) is dict and probe.get("fraction") == fraction
                 and _near(probe.get("elapsed_s"), fraction*interval, 1e-10),
                 "entry_pretrim", "Frozen response probes must follow the exact bounded fraction schedule")
        if probe.get("status") == "failed":
            actual_failed += 1
            _require(index == len(probes)-1 and probe.get("error_class") in {
                "ValueError", "TypeError", "ArithmeticError", "RuntimeError", "IndexError", "KeyError", "LinAlgError", "unexpected_prediction_error"},
                     "entry_pretrim", "Failed probe was discarded or execution continued beyond it")
            continue
        _require(probe.get("status") == "completed" and probe.get("pose_flow_and_clock_held") is True,
                 "entry_pretrim", "Local load prediction must retain its frozen-pose/flow scope")
        expected_angles = list(actual)
        for channel in indices:
            expected_angles[channel] = _endpoint(actual[channel], trim[channel], tau, rate, fraction*interval)
        _compare_vector(probe.get("predicted_flap_angles_rad"), expected_angles, "entry_pretrim", tolerance=1e-9)
        moment = probe.get("predicted_aero_torque_body_nm")
        _require(_vector(moment), "entry_pretrim", "Missing finite predicted aerodynamic moment")
        increment = [moment[j]-item["current_aero_torque_body_nm"][j] for j in range(3)]
        required = [demand[j]-increment[j] for j in range(3)]
        margins = [min(capacities[j][1]-required[j], capacities[j][0]+required[j]) for j in range(3)]
        within = all(value >= -1e-6 for value in margins)
        for key, values in (("predicted_aero_increment_body_nm", increment), ("required_rcs_torque_body_nm", required), ("rcs_headroom_margin_nm", margins)):
            _compare_vector(probe.get(key), values, "entry_pretrim", tolerance=1e-6)
        _require(probe.get("sampled_headroom_met") is within, "entry_pretrim", "Sampled corrective headroom differs from its arithmetic")
        exceeded |= not within
    _require(actual_failed == failed and completed == len(probes)-actual_failed,
             "entry_pretrim", "Probe outcome counters differ from the retained attempt history")
    if failed or exceeded:
        expected_reason = "prediction_failure" if failed else "sampled_rcs_headroom_exceeded"
        _require(status == "deferred" and reason == expected_reason and item["prepared"] is False
                 and _same_value(command["flap_angles_rad"], actual),
                 "entry_pretrim", "Failed/exceeded predictions cannot create fin commands or observed readiness")
        return False
    _require(sample["dynamic_pressure_pa"] <= 100. and status == "accepted"
             and reason == "sampled_corrective_headroom_met" and attempted == completed == 5
             and _same_value(item.get("command_flap_angles_rad"), trim)
             and _same_value(command["flap_angles_rad"], trim) and item["prepared"] is near,
             "entry_pretrim", "Accepted preparation must bind its real command and actual measured fin readiness")
    return near
