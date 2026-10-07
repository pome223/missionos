"""Pure arithmetic checks for bounded full-recovery development predictions.

Saved forecast digests do not prove that a forecast was executed or persisted.
The enclosing caller must bind the external corpus and baseline comparison.
No producer, controller or nonlinear dynamics is imported or replayed here.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import PurePosixPath
import re

from .starship_booster_recovery_verifier import (
    _arrival, _command, _compare_vector, _cross, _near, _norm, _number,
    _observe_handoff, _require, _same_state, _same_value, _state, _vector,
)

SCHEMA_V1 = "missionos.starship_actual_recovery_shooting.v1"
SCHEMA = "missionos.starship_actual_recovery_shooting.v2"
TRANSPORT_SCHEMA = "missionos.starship_actual_recovery_shooting.v3"
CONTEXT_SCHEMA = "missionos.starship_actual_recovery_context.v1"
TRANSPORT_CONTEXT_SCHEMA = "missionos.starship_actual_recovery_context.v2"
TRANSPORT_REFERENCE = {"policy_id": "parallel_transport_deferred_geographic_roll_v1",
    "scope": "powered_entry_prepare_and_command_off_tail",
    "geographic_roll_reacquisition": "after_four_tau_main_shutdown_tail_in_coast",
    "shutdown_tail_tau_multiplier": 4., "thrust_axis": "local_up", "actual_state_assigned": False}
CONFIG_V1 = {"maximum_forecast_calls": 8, "maximum_forecast_duration_s": 600.,
    "maximum_integration_steps_per_forecast": 6500, "maximum_wall_seconds": 900.,
    "angular_offset_bound_deg": 15., "cut_projection_offset_bound_mps": 300.,
    "angle_difference_step_rad": .002, "cut_difference_step_mps": 30.,
    "parameter_order": ["tangent1_rad", "tangent2_rad", "cut_projection_mps"],
    "score_window_max_lowest_pin_clearance_m": 100., "score": "same_checkpoint_normalized_arrival_residual_squared",
    "baseline_equivalence": "exact_physical_checkpoint_suffix_of_saved_v8",
    "joint_step": "central_difference_scaled_lstsq_rcond_1e-10_clipped_to_existing_bounds",
    "prediction_is_execution": False, "production_policy_admitted": False}
CONFIG = {**CONFIG_V1, "selection_priority": "predicted_handoff_then_same_time_score_then_attempt"}
TRANSPORT_CONFIG = {**CONFIG, "baseline_equivalence": "exact_physical_checkpoint_suffix_of_saved_v10",
    "physical_guidance_policy": "constrained_return_development_v10", "context_schema": TRANSPORT_CONTEXT_SCHEMA}
PARAMETER_BOUNDS = [math.radians(15.), math.radians(15.), 300.]
PROBE_PARAMETERS = [[0., 0., 0.], [.002, 0., 0.], [-.002, 0., 0.],
                    [0., .002, 0.], [0., -.002, 0.], [0., 0., 30.], [0., 0., -30.]]


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _parameter_candidate(seed, parameters):
    axis = seed["burn_axis_enu"]
    hint = [1., 0., 0.] if abs(axis[0]) < .8 else [0., 1., 0.]
    dot = sum(a*b for a, b in zip(hint, axis))
    tangent = [hint[i]-dot*axis[i] for i in range(3)]
    length = _norm(tangent)
    tangent = [x/length for x in tangent]
    second = _cross(axis, tangent)
    result = [axis[i]+math.tan(parameters[0])*tangent[i]+math.tan(parameters[1])*second[i] for i in range(3)]
    length = _norm(result)
    result = [x/length for x in result]
    return result, [seed["target_velocity_enu_mps"][i]+parameters[2]*result[i] for i in range(3)]


def _least_squares(matrix, rhs):
    """Independent tiny one-sided Jacobi SVD; no normal-equation squaring.

    Orthogonalize the three columns directly, retain singular values above the
    fixed relative 1e-10 threshold, and reconstruct the minimum-norm solution.
    """
    _require(type(matrix) is list and len(matrix) == 14 and all(_vector(row) for row in matrix)
             and _vector(rhs, 14), "actual_planning", "Invalid finite 14-by-3 local sensitivity")
    columns = [[row[i] for row in matrix] for i in range(3)]
    rotation = [[float(i == j) for j in range(3)] for i in range(3)]
    for _ in range(32):
        changed = False
        for p, q in ((0, 1), (0, 2), (1, 2)):
            a = sum(x*x for x in columns[p])
            b = sum(x*x for x in columns[q])
            cross = sum(x*y for x, y in zip(columns[p], columns[q]))
            if abs(cross) <= 1e-15*math.sqrt(a*b):
                continue
            tau = (b-a)/(2*cross)
            tangent = math.copysign(1., tau)/(abs(tau)+math.hypot(1., tau))
            cosine, sine = 1/math.hypot(1., tangent), tangent/math.hypot(1., tangent)
            first, second = columns[p][:], columns[q][:]
            columns[p] = [cosine*x-sine*y for x, y in zip(first, second)]
            columns[q] = [sine*x+cosine*y for x, y in zip(first, second)]
            for row in rotation:
                x, y = row[p], row[q]
                row[p], row[q] = cosine*x-sine*y, sine*x+cosine*y
            changed = True
        if not changed:
            break
    singular = [math.hypot(*column) for column in columns]
    largest = max(singular)
    result = [0., 0., 0.]
    for index, (column, value) in enumerate(zip(columns, singular)):
        if value <= largest*1e-10 or value == 0:
            continue
        coefficient = sum(a*b for a, b in zip(column, rhs))/(value*value)
        for i in range(3):
            result[i] += rotation[i][index]*coefficient
    return result


def _joint_parameters(baseline, neighbors):
    _require(_vector(baseline, 14) and type(neighbors) is list and len(neighbors) == 6
             and all(_vector(row, 14) for row in neighbors), "actual_planning", "A joint candidate needs every central probe")
    steps = [.002, .002, 30.]
    jacobian = [[(neighbors[2*j][i]-neighbors[2*j+1][i])/(2*steps[j])*PARAMETER_BOUNDS[j]
                 for j in range(3)] for i in range(14)]
    scaled = _least_squares(jacobian, [-x for x in baseline])
    return [bound*max(-1., min(1., value)) for bound, value in zip(PARAMETER_BOUNDS, scaled)], jacobian, scaled


def _score_checkpoint(point, profile, catch_config, *, observation=None, observation_source="checkpoint_navigation"):
    _require(type(point) is dict and type(point.get("navigation")) is dict
             and _number(point.get("time_s")) and (observation_source == "terminal_handoff_observation"
             or point.get("phase", "").startswith("recovery_landing_")),
             "actual_planning", "A forecast objective requires one actual saved landing checkpoint")
    state = point.get("state")
    _state(state, profile)
    _require(point["time_s"] == state["time_s"], "actual_planning", "The objective checkpoint has multiple clocks")
    _command(point.get("command"), point["phase"], state, profile)
    arrival = _arrival(state, profile, catch_config)
    observation = point["navigation"].get("arrival_observation") if observation is None else observation
    _observe_handoff(observation, arrival, state, catch_config)
    _require(min(pin["height_above_support_m"] for pin in arrival["pins"]) <= 100.,
             "actual_planning", "An unobserved ideal coast cannot replace the physical terminal staging window")
    limits = arrival["limits"]
    speed_center = (limits["pin_vertical_speed_min_mps"]+limits["pin_vertical_speed_max_mps"])/2
    speed_span = (limits["pin_vertical_speed_max_mps"]-limits["pin_vertical_speed_min_mps"])/2
    clearance_center = (limits["pin_clearance_min_m"]+limits["pin_clearance_max_m"])/2
    clearance_span = (limits["pin_clearance_max_m"]-limits["pin_clearance_min_m"])/2
    residual = [coordinate/limits["horizontal_position_m"] for coordinate in arrival["midpoint_enu_m"][:2]]
    for pin in arrival["pins"]:
        residual += [value/limits["pin_horizontal_speed_mps"] for value in pin["velocity_enu_mps"][:2]]
    residual += [(pin["velocity_enu_mps"][2]-speed_center)/speed_span for pin in arrival["pins"]]
    residual += [(pin["height_above_support_m"]-clearance_center)/clearance_span for pin in arrival["pins"]]
    residual += [arrival["tilt_deg"]/limits["attitude_angle_deg"],
                 arrival["clocking_error_deg"]/limits["attitude_angle_deg"],
                 arrival["body_rate_rad_s"]/limits["body_rate_rad_s"],
                 max(0., limits["propellant_reserve_kg"]-state["propellant_kg"])/limits["propellant_reserve_kg"]]
    return residual, sum(x*x for x in residual), arrival


def _validate_context(capsule, origin, profile, catch_config, guidance_configuration):
    transported = type(capsule) is dict and capsule.get("schema") == TRANSPORT_CONTEXT_SCHEMA
    snapshot_keys = {"schema", "state", "context", "profile_sha256", "catch_profile_sha256", "guidance_configuration_sha256"}
    if transported:
        snapshot_keys.add("physical_guidance_configuration")
    _require(type(capsule) is dict and set(capsule) == snapshot_keys and capsule.get("schema") in (CONTEXT_SCHEMA, TRANSPORT_CONTEXT_SCHEMA)
             and capsule.get("profile_sha256") == _digest(profile)
             and capsule.get("catch_profile_sha256") == _digest(catch_config)
             and capsule.get("guidance_configuration_sha256") == _digest(guidance_configuration),
             "actual_planning", "Forecast context is not bound to the unchanged physical profile and controller")
    _require(guidance_configuration.get("transport_prepare_roll", False) is transported
             and (not transported or _same_value(capsule["physical_guidance_configuration"], guidance_configuration)
                  and _same_value(guidance_configuration.get("powered_prepare_reference"), TRANSPORT_REFERENCE)),
             "actual_planning", "Context schema cannot change or detach its full opted-in physical request law")
    _same_state(capsule.get("state"), origin, "actual_planning")
    context = capsule.get("context")
    keys = {"phase", "start_time_s", "deadline_s", "plan", "refreshed", "burn_start_s", "settle_start_s",
            "previous_entry_axis_enu", "entry_pretrim_prepared_at_s", "next_preview_s", "braking_preview",
            "prior_command_reference", "conditioned_reference", "reference_tracker"}
    if transported:
        keys.add("prepare_tail_start_s")
    _require(type(context) is dict and set(context) == keys
             and context["phase"] in (("recovery_boostback_slew", "recovery_boostback_burn",
                 "recovery_powered_entry_prepare", "recovery_entry_coast", "recovery_landing_13")
                 + (("recovery_entry_shutdown_tail",) if transported else ()))
             and context["refreshed"] is True and type(context["plan"]) is dict,
             "actual_planning", "Forecasts must preserve the full nonrecursive live control context")
    now = origin["time_s"]
    _require(_number(context["start_time_s"]) and context["start_time_s"] <= now
             and _number(context["deadline_s"]) and now < context["deadline_s"] <= context["start_time_s"]+1200.
             and _number(context["next_preview_s"]) and context["next_preview_s"] <= context["deadline_s"],
             "actual_planning", "Forecast context rewrote the original mission clocks")
    for field in ("burn_start_s", "settle_start_s", "entry_pretrim_prepared_at_s"):
        _require(context[field] is None or _number(context[field]) and context["start_time_s"] <= context[field] <= now,
                 "actual_planning", "Forecast context invented future controller history")
    if transported:
        tail = context["prepare_tail_start_s"]
        _require(tail is None or _number(tail) and context["start_time_s"] <= tail <= now,
                 "actual_planning", "Transport tail clock is future or outside the original run")
        if context["phase"] == "recovery_entry_shutdown_tail":
            _require(tail is not None and context["settle_start_s"] is not None and context["settle_start_s"] <= tail,
                     "actual_planning", "Resumed shutdown tail lost its original cutoff and measured preparation clocks")
    axis = context["previous_entry_axis_enu"]
    _require(axis is None or _vector(axis) and _near(_norm(axis), 1., 1e-8),
             "actual_planning", "Invalid carried entry reference")
    prior = context["prior_command_reference"]
    _require(prior is None or type(prior) is dict and set(prior) == {"quaternion", "time_s"}
             and _vector(prior["quaternion"], 4) and _near(_norm(prior["quaternion"]), 1., 1e-8)
             and _number(prior["time_s"]) and max(context["start_time_s"], now-.25000001) <= prior["time_s"] <= now,
             "actual_planning", "Invalid last executed command anchor")
    reference = context["conditioned_reference"]
    _require(type(reference) is dict and _vector(reference.get("quaternion"), 4)
             and _near(_norm(reference["quaternion"]), 1., 1e-8)
             and _number(reference.get("time_s")) and max(context["start_time_s"], now-.25000001) <= reference["time_s"] <= now
             and type(reference.get("bridging")) is bool and type(reference.get("diagnostics")) is dict
             and type(reference.get("frame_diagnostics")) is dict
             and _near(reference.get("maximum_roll_rate_rad_s"),
                       profile["guidance"]["max_angular_acceleration_rad_s2"]/profile["guidance"]["attitude_frequency_rad_s"]),
             "actual_planning", "Forecast context lost its conditioned geographic frame")
    tracker = context["reference_tracker"]
    if tracker is not None:
        maximum = profile["guidance"]["max_angular_acceleration_rad_s2"]
        rate = maximum/profile["guidance"]["attitude_frequency_rad_s"]
        _require(type(tracker) is dict and _vector(tracker.get("quaternion"), 4)
                 and _near(_norm(tracker["quaternion"]), 1., 1e-8)
                 and _vector(tracker.get("raw_goal_q"), 4) and _near(_norm(tracker["raw_goal_q"]), 1., 1e-8)
                 and _vector(tracker.get("rate_eci_rad_s")) and _norm(tracker["rate_eci_rad_s"]) <= rate+1e-10
                 and _number(tracker.get("time_s")) and max(context["start_time_s"], now-.25000001) <= tracker["time_s"] <= now
                 and tracker.get("raw_goal_time_s") == tracker["time_s"] and type(tracker.get("first_update")) is bool
                 and _near(tracker.get("maximum_rate_rad_s"), rate) and _near(tracker.get("maximum_acceleration_rad_s2"), maximum),
                 "actual_planning", "Forecast context reset or altered bounded request history")
    return context


def _hash(value):
    return type(value) is str and re.fullmatch("[0-9a-f]{64}", value) is not None


def _artifact(item, *, payload=None):
    _require(type(item) is dict and type(item.get("artifact_id")) is str
             and re.fullmatch("[0-9a-f]{32}", item["artifact_id"]) is not None
             and item.get("format") in ("json", "json.gz") and type(item.get("bytes")) is int and item["bytes"] > 0
             and item.get("persisted_before_analysis") is True and _hash(item.get("sha256"))
             and _hash(item.get("raw_json_sha256")) and type(item.get("relative_path")) is str,
             "actual_planning", "Missing opaque persisted forecast artifact declaration")
    path = PurePosixPath(item["relative_path"])
    _require(bool(path.parts) and not path.is_absolute() and ".." not in path.parts
             and "\\" not in item["relative_path"] and ":" not in item["relative_path"]
             and path.name == item["artifact_id"]+"."+item["format"],
             "actual_planning", "Forecast artifacts must remain exclusive opaque relative files")
    if payload is not None:
        _require(item["raw_json_sha256"] == _digest(payload), "actual_planning", "Persisted declared input digest is unbound")
    return item["artifact_id"]


def _score(item, entry, origin, profile, catch_config):
    _require(type(item) is dict and item.get("prediction_is_execution") is False
             and item.get("arrival_admitted") is False and item.get("support_admitted") is False
             and _vector(item.get("residual"), 14) and _number(item.get("objective")) and item["objective"] >= 0
             and type(item.get("checkpoint_index")) is int and 0 <= item["checkpoint_index"] <= entry["integration_steps"],
             "actual_planning", "A forecast score requires one finite actual-time checkpoint, not separately minimized states")
    point = item.get("checkpoint")
    _require(type(point) is dict and item.get("checkpoint_sha256") == _digest(point)
             and item.get("time_s") == point.get("time_s")
             and origin["time_s"] <= item["time_s"] <= origin["time_s"]+entry["request"]["duration_s"]+1e-7,
             "actual_planning", "Scored checkpoint digest or clock escaped its finite forecast")
    source = item.get("observation_source")
    _require(source in ("checkpoint_navigation", "terminal_handoff_observation"), "actual_planning", "Unknown objective observation source")
    observation = item.get("actual_same_time_observation")
    if source == "checkpoint_navigation":
        _require(_same_value(observation, point.get("navigation", {}).get("arrival_observation")),
                 "actual_planning", "Objective observation differs from its actual saved checkpoint")
    else:
        _require(point.get("command") is None and item["checkpoint_index"] == entry["integration_steps"]
                 and ("forecast_end_time_s" not in entry or item["time_s"] == entry["forecast_end_time_s"]),
                 "actual_planning", "A terminal objective cannot replace an earlier executed command")
    residual, _, _ = _score_checkpoint(point, profile, catch_config, observation=observation, observation_source=source)
    _compare_vector(item["residual"], residual, "actual_planning", tolerance=1e-4)
    _require(_near(item["objective"], sum(x*x for x in item["residual"]), 1e-6),
             "actual_planning", "Joint objective is not the squared residual of the same checkpoint")
    return item


def verify_actual_recovery_prediction(plan, origin, profile, catch_config, guidance_configuration, *, previous_checkpoint=None):
    """Check compact declared predictions; never admit external corpus execution."""
    receipt = plan.get("actual_recovery_prediction")
    schema = receipt.get("schema") if type(receipt) is dict else None
    transported = schema == TRANSPORT_SCHEMA
    specification = TRANSPORT_CONFIG if transported else CONFIG if schema == SCHEMA else CONFIG_V1
    _require(type(receipt) is dict and schema in (SCHEMA_V1, SCHEMA, TRANSPORT_SCHEMA) and _same_value(receipt.get("configuration"), specification)
             and all(receipt.get(key) is False for key in ("prediction_is_execution", "actual_state_assigned",
                 "production_policy_admitted", "arrival_admitted", "support_admitted", "physical_execution", "missionos_dispatch",
                 "global_optimality_established", "global_infeasibility_established", "model_value_established"))
             and receipt.get("raw_forecasts_persisted_before_analysis") is True
             and plan.get("method") == "bounded_actual_sixdof_same_time_shooting" and plan.get("admissible") is False,
             "actual_planning", "Missing frozen actual-recovery prediction boundary")
    capsule = receipt.get("origin_context")
    _require(type(capsule) is dict and capsule.get("schema") == (TRANSPORT_CONTEXT_SCHEMA if transported else CONTEXT_SCHEMA),
             "actual_planning", "A baseline optimizer cannot combine old and new physical-law contexts")
    context = _validate_context(capsule, origin, profile, catch_config, guidance_configuration)
    seed = context["plan"]
    _require(_vector(seed.get("burn_axis_enu")) and _near(_norm(seed["burn_axis_enu"]), 1., 1e-8)
             and _vector(seed.get("target_velocity_enu_mps")) and _vector(seed.get("bank_heading_enu"))
             and _near(_norm(seed["bank_heading_enu"]), 1., 1e-8) and abs(seed["bank_heading_enu"][2]) <= 1e-8
             and type(seed.get("bank_sign")) is int and seed["bank_sign"] in (-1, 1)
             and seed.get("prediction_is_execution") is False,
             "actual_planning", "Context contains an invalid physical request or prescribed execution")
    _require(receipt.get("origin_context_sha256") == _digest(capsule)
             and receipt.get("origin_state_sha256") == _digest(origin) and receipt.get("seed_plan_sha256") == _digest(seed),
             "actual_planning", "Prediction state, controller context or source plan was changed")
    prior = context["prior_command_reference"]
    if previous_checkpoint is not None:
        _require(type(prior) is dict and prior["time_s"] == previous_checkpoint["time_s"],
                 "actual_planning", "Context did not preserve the immediately preceding command clock")
        _compare_vector(prior["quaternion"], previous_checkpoint["navigation"]["target_q_body_to_eci"],
                        "actual_planning", tolerance=1e-10)
    elif prior is not None:
        _require(prior["time_s"] == origin["time_s"], "actual_planning", "A historyless context cannot invent a past command")
    baseline_hash = receipt.get("baseline_reference_sha256")
    _require(baseline_hash is None or _hash(baseline_hash), "actual_planning", "Invalid declared baseline-reference digest")
    origin_payload = {"kind": "origin_context", "snapshot": capsule,
        "profile": profile, "catch_profile": catch_config, "guidance_configuration": guidance_configuration,
        "baseline_plan": seed, "baseline_reference_sha256": baseline_hash, "baseline_reference_is_objective": False}
    binding_fields = ("baseline_binding", "expected_reference_policy_id", "reference_policy_id",
                      "baseline_binding_is_caller_assertion", "source_binding_independently_verified")
    if transported:
        binding = receipt.get("baseline_binding")
        _require(type(binding) is dict and set(binding) == {"run_sha256", "profile_sha256", "catch_profile_sha256"}
                 and binding["run_sha256"] == baseline_hash and _hash(baseline_hash)
                 and binding["profile_sha256"] == _digest(profile) and binding["catch_profile_sha256"] == _digest(catch_config)
                 and receipt.get("expected_reference_policy_id") == "constrained_return_development_v10"
                 and receipt.get("reference_policy_id") == "constrained_return_development_v10"
                 and receipt.get("baseline_binding_is_caller_assertion") is True
                 and receipt.get("source_binding_independently_verified") is False,
                 "actual_planning", "The transport planner requires its matching run/profile/catch baseline declaration without authenticating caller source")
        origin_payload.update({key: receipt[key] for key in binding_fields})
    else:
        _require(all(key not in receipt for key in binding_fields),
                 "actual_planning", "A new transport baseline declaration cannot relabel an old planning schema")
    ids = {_artifact(receipt.get("origin_artifact"), payload=origin_payload)}
    entries = receipt.get("forecasts")
    _require(type(entries) is list and len(entries) <= 8, "actual_planning", "Forecast attempt budget is missing or exceeded")
    counts = [receipt.get(key) for key in ("attempted_forecast_count", "completed_forecast_count", "partial_forecast_count", "failed_forecast_count")]
    _require(all(type(value) is int and value >= 0 for value in counts) and counts[0] == len(entries)
             and counts[0] == sum(counts[1:]), "actual_planning", "A failed or partial attempt was omitted from the budget")
    actual_counts = [sum(entry.get("status") == status for entry in entries) for status in ("completed", "partial", "failed")]
    _require(counts[1:] == actual_counts, "actual_planning", "Forecast dispositions disagree with counted attempts")
    deadline = None
    for index, entry in enumerate(entries, 1):
        _require(type(entry) is dict and entry.get("attempt_index") == index and type(entry.get("attempt_index")) is int
                 and entry.get("status") in ("completed", "partial", "failed") and _vector(entry.get("parameters")),
                 "actual_planning", "Attempt ordering or bounded parameter record is invalid")
        parameters = entry["parameters"]
        _require(all(abs(x) <= bound+1e-12 for x, bound in zip(parameters, PARAMETER_BOUNDS)),
                 "actual_planning", "A forecast widened the existing actuator-independent parameter bounds")
        if index <= 7:
            _compare_vector(parameters, PROBE_PARAMETERS[index-1], "actual_planning", tolerance=1e-12)
        candidate_request = entry.get("candidate_request")
        _require(type(candidate_request) is dict and set(candidate_request) == {"burn_axis_enu", "target_velocity_enu_mps"},
                 "actual_planning", "Missing source-bound finite candidate vectors")
        axis, velocity = _parameter_candidate(seed, parameters) if any(parameters) else (seed["burn_axis_enu"], seed["target_velocity_enu_mps"])
        _compare_vector(candidate_request.get("burn_axis_enu"), axis, "actual_planning", tolerance=1e-10)
        _compare_vector(candidate_request.get("target_velocity_enu_mps"), velocity, "actual_planning", tolerance=1e-8)
        candidate = {**seed, **candidate_request}
        candidate_context = {**capsule, "context": {**context, "plan": candidate}}
        _require(entry.get("candidate_plan_sha256") == _digest(candidate)
                 and entry.get("candidate_context_sha256") == _digest(candidate_context),
                 "actual_planning", "Candidate source/context digest is detached from its independently checked parameters")
        request = entry.get("request")
        _require(type(request) is dict and set(request) == {"origin_context_sha256", "duration_s", "maximum_integration_steps", "wall_deadline_monotonic_s"}
                 and request["origin_context_sha256"] == _digest(candidate_context)
                 and _near(request.get("duration_s"), min(600., context["deadline_s"]-origin["time_s"]), 1e-9)
                 and type(request.get("maximum_integration_steps")) is int and request["maximum_integration_steps"] == 6500
                 and _number(request.get("wall_deadline_monotonic_s")),
                 "actual_planning", "A forecast changed the shared time, context or step budget")
        deadline = request["wall_deadline_monotonic_s"] if deadline is None else deadline
        _require(request["wall_deadline_monotonic_s"] == deadline,
                 "actual_planning", "A new forecast reset the global wall deadline")
        for key, payload in (("attempted_artifact", {"kind": "attempted", "attempt_index": index, "parameters": parameters,
                "candidate_plan_sha256": entry["candidate_plan_sha256"], "origin_context_sha256": _digest(capsule),
                "request": request, "prediction_is_execution": False}), ("raw_artifact", None)):
            identity = _artifact(entry.get(key), payload=payload)
            _require(identity not in ids, "actual_planning", "Forecast storage reused a previous exclusive identifier")
            ids.add(identity)
        steps = entry.get("integration_steps")
        _require(type(steps) is int and 0 <= steps <= 6500 and type(entry.get("termination")) is str,
                 "actual_planning", "A forecast exceeded its actual finite step count")
        end, duration = entry.get("forecast_end_time_s"), entry.get("forecast_duration_s")
        if entry["termination"] == "exception_before_result":
            _require(entry["status"] == "failed" and steps == 0 and end is None and duration is None,
                     "actual_planning", "An exception before observations cannot fabricate an integrated horizon")
        else:
            _require(_number(end) and _number(duration) and 0 <= duration <= request["duration_s"]+1e-7
                     and _near(end-origin["time_s"], duration, 1e-7),
                     "actual_planning", "A retained forecast exceeded its actual state-clock horizon")
        if entry["status"] != "completed":
            _require(entry.get("score") is None, "actual_planning", "Partial or failed predictions cannot become selectable scored completions")
        elif entry.get("score") is not None:
            _score(entry["score"], entry, origin, profile, catch_config)
        if schema in (SCHEMA, TRANSPORT_SCHEMA) and entry["status"] == "completed":
            _require(type(entry.get("handoff_eligible")) is bool
                     and entry["handoff_eligible"] is (entry["termination"] == "catch_handoff"),
                     "actual_planning", "Predicted handoff priority needs the completed terminal disposition, not a fabricated score label")
            if entry["handoff_eligible"]:
                _require(type(entry.get("score")) is dict
                         and entry["score"].get("observation_source") == "terminal_handoff_observation"
                         and entry["score"]["actual_same_time_observation"].get("eligible") is True,
                         "actual_planning", "Eligible priority requires the same actual terminal material-point gate")
    comparison = receipt.get("baseline_equivalence")
    if entries:
        _require(type(comparison) is dict and type(comparison.get("checked")) is bool and type(comparison.get("matched")) is bool
                 and comparison.get("reference_is_objective") is False,
                 "actual_planning", "Baseline equivalence must be explicit and separate from the objective")
        if comparison["matched"]:
            _require(comparison["checked"] is True and comparison.get("reason") == "exact_physical_checkpoint_suffix"
                     and comparison.get("origin_state_sha256") == _digest(origin)
                     and type(comparison.get("actual_checkpoint_count")) is int and comparison["actual_checkpoint_count"] > 0
                     and comparison["actual_checkpoint_count"] == entries[0]["integration_steps"]+1
                     and comparison.get("expected_checkpoint_count") == comparison["actual_checkpoint_count"]
                     and _hash(comparison.get("actual_checkpoint_sha256"))
                     and comparison.get("expected_checkpoint_sha256") == comparison["actual_checkpoint_sha256"]
                     and comparison.get("reference_sha256") == baseline_hash,
                     "actual_planning", "Declared baseline equality has differing source, state, counts or trace digests")
            if transported:
                _require(comparison.get("expected_reference_policy_id") == "constrained_return_development_v10"
                         and comparison.get("reference_policy_id") == "constrained_return_development_v10",
                         "actual_planning", "A matched transport baseline cannot refer to the old v8 physical law")
        elif comparison["checked"]:
            _require(_hash(comparison.get("actual_checkpoint_sha256")) and _hash(comparison.get("expected_checkpoint_sha256"))
                     and (comparison["actual_checkpoint_sha256"] != comparison["expected_checkpoint_sha256"]
                          or comparison.get("actual_checkpoint_count") != comparison.get("expected_checkpoint_count")),
                     "actual_planning", "A declared baseline mismatch contradicts identical persisted physical traces")
        if not comparison["matched"] or entries[0]["status"] != "completed" or entries[0].get("score") is None:
            _require(len(entries) == 1, "actual_planning", "Optimization continued without a matched complete scored baseline")
    else:
        _require(comparison is None, "actual_planning", "A nonexistent forecast cannot establish baseline equivalence")
    joint = receipt.get("joint_construction")
    if joint is not None:
        _require(len(entries) >= 7 and all(entry["status"] == "completed" and entry.get("score") is not None for entry in entries[:7]),
                 "actual_planning", "A joint step requires all six scored finite central probes")
        parameters, scaled_jacobian, unbounded = _joint_parameters(entries[0]["score"]["residual"], [entry["score"]["residual"] for entry in entries[1:7]])
        _require(type(joint) is dict and joint.get("rcond") == 1e-10 and _same_value(joint.get("parameter_bounds"), PARAMETER_BOUNDS),
                 "actual_planning", "Joint construction widened bounds or changed the rank threshold")
        expected_jacobian = [[value/PARAMETER_BOUNDS[i] for i, value in enumerate(row)] for row in scaled_jacobian]
        _require(type(joint.get("jacobian")) is list and len(joint["jacobian"]) == 14,
                 "actual_planning", "Missing same-objective central sensitivity")
        for row, expected in zip(joint["jacobian"], expected_jacobian):
            _compare_vector(row, expected, "actual_planning", tolerance=1e-5)
        _compare_vector(joint.get("unbounded_scaled_step"), unbounded, "actual_planning", tolerance=1e-6)
        _compare_vector(joint.get("bounded_scaled_step"), [max(-1., min(1., x)) for x in unbounded], "actual_planning", tolerance=1e-6)
        if len(entries) == 8:
            _compare_vector(entries[-1]["parameters"], parameters, "actual_planning", tolerance=1e-5)
    else:
        _require(len(entries) <= 7, "actual_planning", "An eighth candidate lacked its constrained central-probe construction")
    wall = receipt.get("wall_seconds")
    _require(_number(wall) and wall >= 0 and type(receipt.get("wall_budget_exhausted")) is bool
             and receipt["wall_budget_exhausted"] is (wall >= CONFIG["maximum_wall_seconds"]),
             "actual_planning", "Wall cleanup overruns must remain explicit and cannot reset the prediction budget")
    status = receipt.get("optimizer_status")
    _require(status in ("wall_budget_exhausted", "baseline_equivalence_not_established", "baseline_has_no_complete_scored_continuation",
             "bounded_neighbors_evaluated", "joint_candidate_evaluated", "joint_candidate_duplicates_prior"),
             "actual_planning", "Unknown bounded physical predictor stop condition")
    if receipt["wall_budget_exhausted"]:
        _require(status == "wall_budget_exhausted", "actual_planning", "Wall exhaustion was rewritten as successful optimization")
    eligible = [entry for entry in entries if entry["status"] == "completed" and entry.get("score") is not None]
    fallback = receipt["wall_budget_exhausted"] or comparison is None or comparison.get("matched") is not True
    ranking = (lambda entry: (not entry["handoff_eligible"], entry["score"]["objective"], entry["attempt_index"])) if schema in (SCHEMA, TRANSPORT_SCHEMA) else (
        lambda entry: (entry["score"]["objective"], entry["attempt_index"]))
    selected = entries[0] if entries and fallback else min(eligible, key=ranking) if eligible else entries[0] if entries else None
    _require(receipt.get("selected_attempt_index") == (selected["attempt_index"] if selected else None),
             "actual_planning", "Selected candidate differs from the recorded complete joint objective or required baseline fallback")
    parameters = selected["parameters"] if selected else [0., 0., 0.]
    _compare_vector(receipt.get("selected_parameters"), parameters, "actual_planning", tolerance=1e-12)
    axis, velocity = _parameter_candidate(seed, parameters) if any(parameters) else (seed["burn_axis_enu"], seed["target_velocity_enu_mps"])
    _compare_vector(plan.get("burn_axis_enu"), axis, "actual_planning", tolerance=1e-8)
    _compare_vector(plan.get("target_velocity_enu_mps"), velocity, "actual_planning", tolerance=1e-5)
    _require(_same_value(plan.get("bank_heading_enu"), seed.get("bank_heading_enu")) and plan.get("bank_sign") == seed.get("bank_sign"),
             "actual_planning", "Actual-recovery shooting changed the fixed entry bank branch")
    return receipt
