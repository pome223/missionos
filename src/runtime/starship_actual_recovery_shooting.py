"""Bounded actual-plant continuation planning; predictions create no authority.

Every forecast resumes the SAME finite recovery loop with exact physical and
controller memory.  Raw forecasts are persisted by the caller before analysis;
only one same-time scored checkpoint per attempt enters the compact receipt.
The external v8 trace is equivalence evidence, never an optimization objective.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
import math
from pathlib import PurePosixPath
import time
from uuid import uuid4

import numpy as np

CONTEXT_SCHEMA = "missionos.starship_actual_recovery_context.v1"
TRANSPORT_CONTEXT_SCHEMA = "missionos.starship_actual_recovery_context.v2"
SCHEMA = "missionos.starship_actual_recovery_shooting.v2"
TRANSPORT_SCHEMA = "missionos.starship_actual_recovery_shooting.v3"
CONFIG = {"maximum_forecast_calls": 8, "maximum_forecast_duration_s": 600.,
    "maximum_integration_steps_per_forecast": 6500, "maximum_wall_seconds": 900.,
    "angular_offset_bound_deg": 15., "cut_projection_offset_bound_mps": 300.,
    "angle_difference_step_rad": .002, "cut_difference_step_mps": 30.,
    "parameter_order": ["tangent1_rad", "tangent2_rad", "cut_projection_mps"],
    "score_window_max_lowest_pin_clearance_m": 100., "score": "same_checkpoint_normalized_arrival_residual_squared",
    "baseline_equivalence": "exact_physical_checkpoint_suffix_of_saved_v8",
    "joint_step": "central_difference_scaled_lstsq_rcond_1e-10_clipped_to_existing_bounds",
    "selection_priority": "predicted_handoff_then_same_time_score_then_attempt",
    "prediction_is_execution": False, "production_policy_admitted": False}
TRANSPORT_CONFIG = {**CONFIG,
    "baseline_equivalence": "exact_physical_checkpoint_suffix_of_saved_v10",
    "physical_guidance_policy": "constrained_return_development_v10",
    "context_schema": TRANSPORT_CONTEXT_SCHEMA}


def actual_planning_configuration(transported=False):
    if type(transported) is not bool:
        raise ValueError("invalid_actual_planning_configuration_mode")
    return deepcopy(TRANSPORT_CONFIG if transported else CONFIG)


def saved(value):
    return json.loads(json.dumps(value, allow_nan=False))


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _vector(value, count, unit=False):
    return (type(value) is list and len(value) == count and all(_number(x) for x in value)
            and (not unit or abs(math.hypot(*value)-1.) <= 1e-8))


def capture_context(state, profile, catch, guidance_configuration, **context):
    reference, tracker = context.pop("conditioned_reference"), context.pop("reference_tracker")
    context["conditioned_reference"] = {"quaternion": reference.frame.quaternion,
        "time_s": reference.time_s, "maximum_roll_rate_rad_s": reference.maximum_roll_rate_rad_s,
        "bridging": reference.bridging, "diagnostics": deepcopy(reference.diagnostics),
        "frame_diagnostics": deepcopy(reference.frame.diagnostics)}
    context["reference_tracker"] = deepcopy(vars(tracker)) if tracker is not None else None
    snapshot = saved({"schema": CONTEXT_SCHEMA, "state": asdict(state), "context": context,
        "profile_sha256": digest(profile), "catch_profile_sha256": digest(catch),
        "guidance_configuration_sha256": digest(guidance_configuration)})
    if guidance_configuration.get("transport_prepare_roll") is True:
        snapshot.update(schema=TRANSPORT_CONTEXT_SCHEMA,
                        physical_guidance_configuration=deepcopy(guidance_configuration))
    validate_context(snapshot, profile, catch, guidance_configuration)
    return snapshot


def physical_configuration_from_context(snapshot):
    from .starship_constrained_recovery import physical_guidance_configuration
    if type(snapshot) is not dict:
        raise ValueError("invalid_actual_recovery_context")
    if snapshot.get("schema") == CONTEXT_SCHEMA:
        return physical_guidance_configuration(False)
    if snapshot.get("schema") == TRANSPORT_CONTEXT_SCHEMA:
        declared = snapshot.get("physical_guidance_configuration")
        expected = physical_guidance_configuration(True)
        if declared != expected:
            raise ValueError("invalid_actual_recovery_physical_configuration")
        return deepcopy(declared)
    raise ValueError("invalid_actual_recovery_context_schema")


def validate_transport_baseline(reference, binding, profile, catch, *, initial_state=None):
    """Validate current-law/reference/input hashes only, never future objectives.

    Source provenance remains the enclosing caller's responsibility. The
    observed reference trajectory is used solely for the zero-offset check.
    """
    from .starship_constrained_recovery import physical_guidance_configuration
    if (type(reference) is not dict or reference.get("guidance_policy") != "constrained_return_development_v10"
            or type(reference.get("recovery_record")) is not dict
            or reference["recovery_record"].get("guidance_configuration") != physical_guidance_configuration(True)
            or type(binding) is not dict or set(binding) != {"run_sha256", "profile_sha256", "catch_profile_sha256"}
            or binding["run_sha256"] != digest(reference) or binding["profile_sha256"] != digest(profile)
            or binding["catch_profile_sha256"] != digest(catch)):
        raise ValueError("transport_actual_planning_requires_matching_v10_reference_inputs")
    separation = reference["recovery_record"].get("input_separation_state")
    if type(separation) is not dict or saved(reference.get("booster_separation_state")) != saved(separation):
        raise ValueError("transport_baseline_separation_binding")
    if initial_state is not None and saved(initial_state) != saved(separation):
        raise ValueError("transport_baseline_initial_state_mismatch")
    return deepcopy(binding)


def validate_context(snapshot, profile, catch, guidance_configuration):
    expected = {"phase", "start_time_s", "deadline_s", "plan", "refreshed", "burn_start_s", "settle_start_s",
        "previous_entry_axis_enu", "entry_pretrim_prepared_at_s", "next_preview_s", "braking_preview",
        "prior_command_reference", "conditioned_reference", "reference_tracker"}
    declared_configuration = physical_configuration_from_context(snapshot)
    transported = snapshot["schema"] == TRANSPORT_CONTEXT_SCHEMA
    expected_snapshot = {"schema", "state", "context", "profile_sha256", "catch_profile_sha256", "guidance_configuration_sha256"}
    if transported:
        expected_snapshot.add("physical_guidance_configuration")
        expected.add("prepare_tail_start_s")
    if (type(snapshot) is not dict or set(snapshot) != expected_snapshot
            or guidance_configuration != declared_configuration or type(snapshot["state"]) is not dict
            or type(snapshot["context"]) is not dict or set(snapshot["context"]) != expected
            or snapshot["profile_sha256"] != digest(profile) or snapshot["catch_profile_sha256"] != digest(catch)
            or snapshot["guidance_configuration_sha256"] != digest(guidance_configuration)):
        raise ValueError("invalid_actual_recovery_context")
    ctx, now = snapshot["context"], snapshot["state"].get("time_s")
    if (not _number(now) or not _number(ctx["start_time_s"]) or not _number(ctx["deadline_s"])
            or not ctx["start_time_s"] <= now <= ctx["deadline_s"] <= ctx["start_time_s"]+1200.
            or ctx["phase"] not in (("recovery_boostback_slew", "recovery_boostback_burn",
                "recovery_powered_entry_prepare", "recovery_entry_coast", "recovery_landing_13")
                + (("recovery_entry_shutdown_tail",) if transported else ()))
            or type(ctx["refreshed"]) is not bool or not ctx["refreshed"] or type(ctx["plan"]) is not dict
            or not _vector(ctx["plan"].get("burn_axis_enu"), 3, True)
            or not _vector(ctx["plan"].get("target_velocity_enu_mps"), 3)
            or not _vector(ctx["plan"].get("bank_heading_enu"), 3, True)
            or abs(ctx["plan"]["bank_heading_enu"][2]) > 1e-8
            or type(ctx["plan"].get("bank_sign")) is not int or ctx["plan"]["bank_sign"] not in (-1, 1)
            or not _number(ctx["next_preview_s"]) or ctx["braking_preview"] is not None and type(ctx["braking_preview"]) is not dict
            or ctx["previous_entry_axis_enu"] is not None and not _vector(ctx["previous_entry_axis_enu"], 3, True)):
        raise ValueError("invalid_actual_recovery_context")
    for field in ("burn_start_s", "settle_start_s", "entry_pretrim_prepared_at_s"):
        if ctx[field] is not None and (not _number(ctx[field]) or not ctx["start_time_s"] <= ctx[field] <= now):
            raise ValueError("invalid_actual_recovery_context_clock")
    if transported:
        tail_start = ctx["prepare_tail_start_s"]
        if tail_start is not None and (not _number(tail_start) or not ctx["start_time_s"] <= tail_start <= now):
            raise ValueError("invalid_actual_recovery_tail_clock")
        if ctx["phase"] == "recovery_entry_shutdown_tail" and (tail_start is None or ctx["settle_start_s"] is None):
            raise ValueError("shutdown_tail_requires_original_cut_and_ready_clocks")
    reference = ctx["conditioned_reference"]
    if (type(reference) is not dict or set(reference) != {"quaternion", "time_s", "maximum_roll_rate_rad_s",
            "bridging", "diagnostics", "frame_diagnostics"} or not _vector(reference["quaternion"], 4, True)
            or not _number(reference["time_s"]) or not now-.25000001 <= reference["time_s"] <= now
            or type(reference["bridging"]) is not bool or type(reference["diagnostics"]) is not dict
            or type(reference["frame_diagnostics"]) is not dict
            or reference["maximum_roll_rate_rad_s"] != profile["guidance"]["max_angular_acceleration_rad_s2"]/
                profile["guidance"]["attitude_frequency_rad_s"]):
        raise ValueError("invalid_actual_recovery_conditioned_reference")
    previous = ctx["prior_command_reference"]
    if previous is not None and (type(previous) is not dict or set(previous) != {"quaternion", "time_s"}
            or not _vector(previous["quaternion"], 4, True) or not _number(previous["time_s"])
            or not now-.25000001 <= previous["time_s"] <= now):
        raise ValueError("invalid_actual_recovery_previous_command")
    tracker = ctx["reference_tracker"]
    if tracker is not None:
        if (type(tracker) is not dict or set(tracker) != {"quaternion", "time_s", "rate_eci_rad_s", "raw_goal_q",
                "raw_goal_time_s", "first_update", "maximum_rate_rad_s", "maximum_acceleration_rad_s2"}
                or not _vector(tracker["quaternion"], 4, True) or not _vector(tracker["raw_goal_q"], 4, True)
                or not _vector(tracker["rate_eci_rad_s"], 3) or not _number(tracker["time_s"])
                or not now-.25000001 <= tracker["time_s"] <= now
                or tracker["raw_goal_time_s"] != tracker["time_s"] or type(tracker["first_update"]) is not bool
                or tracker["maximum_rate_rad_s"] != reference["maximum_roll_rate_rad_s"]
                or tracker["maximum_acceleration_rad_s2"] != profile["guidance"]["max_angular_acceleration_rad_s2"]
                or math.hypot(*tracker["rate_eci_rad_s"]) > tracker["maximum_rate_rad_s"]+1e-12):
            raise ValueError("invalid_actual_recovery_reference_tracker")
    # JSON serialization rejects nonfinite nested states/cached data as well.
    saved(snapshot)


def restore_references(context, profile):
    from .starship_attitude_reference import ConditionedGeographicFrame
    from .starship_reference_tracking import ReferenceTracker
    data = context["conditioned_reference"]
    reference = ConditionedGeographicFrame(data["quaternion"], data["time_s"],
        maximum_roll_rate_rad_s=data["maximum_roll_rate_rad_s"])
    reference.frame.quaternion = tuple(data["quaternion"])
    reference.bridging = data["bridging"]
    reference.diagnostics, reference.frame.diagnostics = deepcopy(data["diagnostics"]), deepcopy(data["frame_diagnostics"])
    data, tracker = context["reference_tracker"], None
    if data is not None:
        tracker = ReferenceTracker(profile, data["quaternion"], data["time_s"],
            initial_reference_rate_eci_rad_s=data["rate_eci_rad_s"], initial_raw_goal_q=data["raw_goal_q"])
        for key, value in data.items():
            setattr(tracker, key, tuple(value) if key in ("quaternion", "rate_eci_rad_s", "raw_goal_q") else value)
    return reference, tracker


def validate_forecast_request(request, snapshot):
    if (type(request) is not dict or set(request) != {"origin_context_sha256", "duration_s", "maximum_integration_steps",
            "wall_deadline_monotonic_s"} or request["origin_context_sha256"] != digest(snapshot)
            or not _number(request["duration_s"]) or not 0 < request["duration_s"] <= 600.
            or snapshot["state"]["time_s"]+request["duration_s"] > snapshot["context"]["deadline_s"]+1e-9
            or type(request["maximum_integration_steps"]) is not int or not 1 <= request["maximum_integration_steps"] <= 6500
            or not _number(request["wall_deadline_monotonic_s"])):
        raise ValueError("invalid_actual_recovery_forecast_request")


def forecast_constrained_continuation(snapshot, profile, catch, candidate_plan, request):
    from .starship_constrained_recovery import simulate_constrained_recovery
    resumed = deepcopy(snapshot)
    resumed["context"]["plan"] = deepcopy(candidate_plan)
    validate_context(resumed, profile, catch, physical_configuration_from_context(resumed))
    request = {**request, "origin_context_sha256": digest(resumed)}
    return simulate_constrained_recovery(profile, resumed["state"], catch, _resume_context=resumed,
        _forecast_request=request, duration_s=request["duration_s"])


def physical_checkpoint_view(point):
    return {key: deepcopy(point[key]) for key in ("time_s", "phase", "state", "command", "com_rate_body_mps")}


def baseline_equivalence(forecast, reference, origin):
    actual = forecast["recovery_record"]["checkpoints"]
    base = {"checked": False, "matched": False, "reference_is_objective": False,
            "actual_checkpoint_count": len(actual), "origin_state_sha256": digest(origin["state"])}
    if type(reference) is not dict or type(reference.get("recovery_record")) is not dict:
        return {**base, "reason": "missing_saved_v8_reference"}
    expected_policy = ("constrained_return_development_v10" if origin.get("schema") == TRANSPORT_CONTEXT_SCHEMA
                       else "constrained_return_development_v8")
    if origin.get("schema") == TRANSPORT_CONTEXT_SCHEMA:
        base.update(expected_reference_policy_id=expected_policy, reference_policy_id=reference.get("guidance_policy"))
    if reference.get("guidance_policy") != expected_policy:
        return {**base, "reason": "unexpected_reference_policy"}
    expected = [point for point in reference["recovery_record"].get("checkpoints", [])
                if point["time_s"] >= origin["state"]["time_s"]]
    # Runtime dataclass dictionaries retain tuples; parsed saved traces use
    # lists. Compare the SAME canonical JSON representation used by digests.
    # This normalizes serialization only, never the physical numeric values.
    actual_view = saved([physical_checkpoint_view(point) for point in actual])
    expected_view = saved([physical_checkpoint_view(point) for point in expected])
    return {**base, "checked": True, "matched": actual_view == expected_view,
        "reason": "exact_physical_checkpoint_suffix" if actual_view == expected_view else "baseline_continuation_mismatch",
        "expected_checkpoint_count": len(expected), "actual_checkpoint_sha256": digest(actual_view),
        "expected_checkpoint_sha256": digest(expected_view), "reference_sha256": digest(reference)}


def checkpoint_score(point, *, observation=None, observation_source="checkpoint_navigation"):
    observation = point.get("navigation", {}).get("arrival_observation") if observation is None else observation
    if (type(observation) is not dict or observation["time_s"] != point["time_s"]
            or min(observation["pin_height_above_support_m"]) > CONFIG["score_window_max_lowest_pin_clearance_m"]):
        return None
    limits, pins = observation["limits"], observation["pins"]
    vertical_center = (limits["pin_vertical_speed_min_mps"]+limits["pin_vertical_speed_max_mps"])/2
    vertical_half = (limits["pin_vertical_speed_max_mps"]-limits["pin_vertical_speed_min_mps"])/2
    height_center = (limits["pin_clearance_min_m"]+limits["pin_clearance_max_m"])/2
    height_half = (limits["pin_clearance_max_m"]-limits["pin_clearance_min_m"])/2
    residual = [value/limits["horizontal_position_m"] for value in observation["position_error_enu_m"][:2]]
    residual += [pin["relative_velocity_enu_mps"][axis]/limits["pin_horizontal_speed_mps"] for pin in pins for axis in (0, 1)]
    residual += [(pin["relative_velocity_enu_mps"][2]-vertical_center)/vertical_half for pin in pins]
    residual += [(height-height_center)/height_half for height in observation["pin_height_above_support_m"]]
    residual += [observation["tilt_deg"]/limits["attitude_angle_deg"],
        observation["body_x_east_angle_deg"]/limits["attitude_angle_deg"], observation["body_rate_rad_s"]/limits["body_rate_rad_s"],
        max(0., limits["propellant_reserve_kg"]-observation["propellant_kg"])/limits["propellant_reserve_kg"]]
    if len(residual) != 14 or any(not math.isfinite(value) for value in residual):
        raise ValueError("invalid_same_time_arrival_score")
    return {"time_s": point["time_s"], "residual": residual, "objective": sum(value*value for value in residual),
        "checkpoint": deepcopy(point), "checkpoint_sha256": digest(point),
        "observation_source": observation_source,
        "actual_same_time_observation": deepcopy(observation), "prediction_is_execution": False,
        "arrival_admitted": False, "support_admitted": False}


def score_forecast(forecast):
    points, scores = forecast["recovery_record"]["checkpoints"], []
    for index, point in enumerate(points):
        score = checkpoint_score(point)
        if score is not None:
            scores.append({**score, "checkpoint_index": index})
    if points:
        terminal_observation = forecast["recovery_record"]["handoff"]["observation"]
        if points[-1]["state"] != forecast["final_state"]:
            raise ValueError("terminal_forecast_state_mismatch")
        terminal = checkpoint_score(points[-1], observation=terminal_observation,
                                    observation_source="terminal_handoff_observation")
        if terminal is not None:
            terminal = {**terminal, "checkpoint_index": len(points)-1}
            if forecast["recovery_record"]["handoff"]["eligible"] is True:
                return terminal
            scores.append(terminal)
        elif forecast["recovery_record"]["handoff"]["eligible"] is True:
            raise ValueError("eligible_handoff_requires_scored_terminal_checkpoint")
    return min(scores, key=lambda score: (score["objective"], score["time_s"])) if scores else None


def candidate_plan(seed, parameters):
    from .starship_boostback_shooting import _candidate
    if not _vector(list(parameters), 3):
        raise ValueError("invalid_actual_shooting_parameters")
    bounds = [math.radians(CONFIG["angular_offset_bound_deg"])]*2+[CONFIG["cut_projection_offset_bound_mps"]]
    if any(abs(value) > bound+1e-12 for value, bound in zip(parameters, bounds)):
        raise ValueError("actual_shooting_parameter_bounds_exceeded")
    if all(value == 0 for value in parameters):
        return deepcopy(seed)  # Zero-offset baseline preserves every floating bit.
    return {**deepcopy(seed), **_candidate(seed, parameters)}


def joint_parameters(baseline, neighbors):
    increments = [.002, .002, 30.]
    bounds = np.asarray([math.radians(15.), math.radians(15.), 300.])
    jacobian = np.column_stack([(np.asarray(neighbors[2*i]["residual"])-np.asarray(neighbors[2*i+1]["residual"]))/(2*increments[i])
                               for i in range(3)])
    scaled = np.linalg.lstsq(jacobian*bounds, -np.asarray(baseline["residual"]), rcond=1e-10)[0]
    clipped = np.clip(scaled, -1., 1.)
    return (bounds*clipped).tolist(), {"jacobian": jacobian.tolist(), "unbounded_scaled_step": scaled.tolist(),
        "bounded_scaled_step": clipped.tolist(), "parameter_bounds": bounds.tolist(), "rcond": 1e-10}


def _persist(sink, payload):
    identifier = uuid4().hex
    manifest = sink(identifier, payload)
    if (type(manifest) is not dict or manifest.get("artifact_id") != identifier
            or manifest.get("raw_json_sha256") != digest(payload) or manifest.get("format") not in ("json", "json.gz")
            or type(manifest.get("bytes")) is not int or manifest["bytes"] <= 0
            or manifest.get("persisted_before_analysis") is not True
            or not isinstance(manifest.get("sha256"), str) or len(manifest["sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in manifest["sha256"])
            or not isinstance(manifest.get("relative_path"), str)
            or manifest["relative_path"] not in (identifier+".json", identifier+".json.gz")
            or PurePosixPath(manifest["relative_path"]).is_absolute()
            or any(part in ("..", "") for part in PurePosixPath(manifest["relative_path"]).parts)
            or "\\" in manifest["relative_path"] or ":" in manifest["relative_path"]):
        raise ValueError("invalid_persisted_forecast_manifest")
    return deepcopy(manifest)


def refine_actual_recovery_plan(snapshot, profile, catch, baseline_plan, *, artifact_sink, baseline_reference=None,
                               baseline_binding=None):
    if not callable(artifact_sink):
        raise ValueError("actual_planning_requires_artifact_sink")
    clock_start = time.monotonic()
    snapshot, profile, catch, seed = deepcopy(snapshot), deepcopy(profile), deepcopy(catch), deepcopy(baseline_plan)
    driver_configuration = physical_configuration_from_context(snapshot)
    validate_context(snapshot, profile, catch, driver_configuration)
    transported = snapshot["schema"] == TRANSPORT_CONTEXT_SCHEMA
    planning_configuration = actual_planning_configuration(transported)
    if transported:
        baseline_binding = validate_transport_baseline(baseline_reference, baseline_binding, profile, catch)
    elif baseline_binding is not None:
        raise ValueError("baseline_input_binding_requires_transport_schema3")
    if snapshot["context"]["plan"] != seed:
        raise ValueError("actual_planning_seed_context_mismatch")
    binding_fields = ({"baseline_binding": deepcopy(baseline_binding),
        "expected_reference_policy_id": "constrained_return_development_v10",
        "reference_policy_id": baseline_reference["guidance_policy"],
        "baseline_binding_is_caller_assertion": True, "source_binding_independently_verified": False}
        if transported else {})
    origin_manifest = _persist(artifact_sink, {"kind": "origin_context", "snapshot": snapshot,
        "profile": profile, "catch_profile": catch, "guidance_configuration": driver_configuration, "baseline_plan": seed,
        "baseline_reference_sha256": digest(baseline_reference) if baseline_reference is not None else None,
        "baseline_reference_is_objective": False, **binding_fields})
    entries = []
    wall_deadline = clock_start+CONFIG["maximum_wall_seconds"]
    duration = min(600., snapshot["context"]["deadline_s"]-snapshot["state"]["time_s"])
    if duration <= 0:
        raise ValueError("actual_planning_origin_has_no_remaining_time")
    comparison, joint_detail = None, None

    def evaluate(parameters):
        if len(entries) >= 8 or time.monotonic() >= wall_deadline:
            return None
        index, candidate = len(entries)+1, candidate_plan(seed, parameters)
        candidate_snapshot = deepcopy(snapshot)
        candidate_snapshot["context"]["plan"] = candidate
        candidate_context_sha256 = digest(candidate_snapshot)
        request = {"origin_context_sha256": candidate_context_sha256, "duration_s": duration,
            "maximum_integration_steps": 6500, "wall_deadline_monotonic_s": wall_deadline}
        attempted_manifest = _persist(artifact_sink, {"kind": "attempted", "attempt_index": index,
            "parameters": list(parameters), "candidate_plan_sha256": digest(candidate), "origin_context_sha256": digest(snapshot),
            "request": request, "prediction_is_execution": False})
        entry = {"attempt_index": index, "parameters": list(parameters), "candidate_plan_sha256": digest(candidate),
            "candidate_request": {key: deepcopy(candidate[key]) for key in ("burn_axis_enu", "target_velocity_enu_mps")},
            "candidate_context_sha256": candidate_context_sha256, "request": deepcopy(request),
            "attempted_artifact": attempted_manifest, "status": "attempted", "score": None}
        entries.append(entry)
        forecast, error_type = None, None
        call_start = time.monotonic()
        try:
            forecast = forecast_constrained_continuation(snapshot, profile, catch, candidate, request)
        except Exception as exc:
            error_type = type(exc).__name__
        # External persistence always precedes scoring, equivalence, selection.
        entry["raw_artifact"] = _persist(artifact_sink, {"kind": "raw_forecast", "attempt_index": index,
            "parameters": list(parameters), "origin_context_sha256": digest(snapshot), "request": request,
            "forecast": forecast, "error_type": error_type, "wall_seconds": time.monotonic()-call_start,
            "prediction_is_execution": False, "production_policy_admitted": False})
        if forecast is None:
            entry.update(status="failed", error_type=error_type, integration_steps=0, termination="exception_before_result")
        else:
            meta = forecast["forecast_metadata"]
            entry.update(status="failed" if meta["failure"] is not None else "completed" if meta["prediction_complete"] else "partial",
                integration_steps=forecast["outcome"]["integration_steps"], termination=forecast["outcome"]["termination"],
                forecast_end_time_s=forecast["final_state"]["time_s"],
                forecast_duration_s=forecast["final_state"]["time_s"]-snapshot["state"]["time_s"],
                error_type=meta["failure"], contact=deepcopy(forecast["contact"]),
                handoff_eligible=forecast["recovery_record"]["handoff"]["eligible"])
            if entry["status"] == "completed":
                entry["score"] = score_forecast(forecast)
        return entry, forecast

    baseline = evaluate([0., 0., 0.])
    status = "baseline_not_attempted"
    if baseline is not None:
        comparison = baseline_equivalence(baseline[1], baseline_reference, snapshot) if baseline[1] is not None else {
            "checked": False, "matched": False, "reason": "baseline_failed", "reference_is_objective": False}
        baseline = (baseline[0], None)  # Raw baseline is already external and validated.
        if not comparison["matched"]:
            status = "baseline_equivalence_not_established"
        elif baseline[0]["status"] != "completed" or baseline[0]["score"] is None:
            status = "baseline_has_no_complete_scored_continuation"
        else:
            status = "bounded_neighbors_evaluated"
            probes = ([.002, 0., 0.], [-.002, 0., 0.], [0., .002, 0.], [0., -.002, 0.], [0., 0., 30.], [0., 0., -30.])
            for probe in probes:
                if evaluate(probe) is None:
                    status = "wall_budget_exhausted"
                    break
            if len(entries) == 7 and all(entry["status"] == "completed" and entry["score"] is not None for entry in entries):
                parameters, joint_detail = joint_parameters(entries[0]["score"], [entry["score"] for entry in entries[1:]])
                if not any(parameters == entry["parameters"] for entry in entries):
                    if evaluate(parameters) is None:
                        status = "wall_budget_exhausted"
                    else:
                        status = "joint_candidate_evaluated"
                else:
                    status = "joint_candidate_duplicates_prior"
    wall_exhausted = time.monotonic() >= wall_deadline
    if wall_exhausted:
        status = "wall_budget_exhausted"
    valid = [entry for entry in entries if entry["status"] == "completed" and entry["score"] is not None]
    if wall_exhausted or comparison is None or not comparison["matched"]:
        selected = entries[0] if entries else None
    else:
        selected = min(valid, key=lambda entry: (not entry["handoff_eligible"], entry["score"]["objective"], entry["attempt_index"])) if valid else entries[0]
    parameters = selected["parameters"] if selected is not None else [0., 0., 0.]
    receipt = {"schema": TRANSPORT_SCHEMA if transported else SCHEMA,
        "configuration": planning_configuration, "origin_context_sha256": digest(snapshot),
        "origin_context": deepcopy(snapshot),
        "baseline_reference_sha256": digest(baseline_reference) if baseline_reference is not None else None,
        "origin_state_sha256": digest(snapshot["state"]), "origin_artifact": origin_manifest,
        "seed_plan_sha256": digest(seed), "baseline_equivalence": comparison, "optimizer_status": status,
        "attempted_forecast_count": len(entries), "completed_forecast_count": sum(entry["status"] == "completed" for entry in entries),
        "partial_forecast_count": sum(entry["status"] == "partial" for entry in entries),
        "failed_forecast_count": sum(entry["status"] == "failed" for entry in entries), "forecasts": entries,
        "wall_seconds": time.monotonic()-clock_start, "joint_construction": joint_detail,
        "wall_budget_exhausted": wall_exhausted,
        "selected_attempt_index": selected["attempt_index"] if selected is not None else None,
        "selected_parameters": parameters, "raw_forecasts_persisted_before_analysis": True,
        "prediction_is_execution": False, "actual_state_assigned": False, "production_policy_admitted": False,
        "arrival_admitted": False, "support_admitted": False, "physical_execution": False, "missionos_dispatch": False,
        "global_optimality_established": False, "global_infeasibility_established": False, "model_value_established": False,
        **binding_fields}
    selected_plan = candidate_plan(seed, parameters)
    selected_plan.update(method="bounded_actual_sixdof_same_time_shooting", actual_recovery_prediction=receipt,
                         prediction_is_execution=False, production_policy_admitted=False, admissible=False)
    return saved(selected_plan), saved(receipt)
