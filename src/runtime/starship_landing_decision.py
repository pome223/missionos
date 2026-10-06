"""Offline, source-bound single landing-onset decisions; no state projection.

Archived observations reconstruct controller MEMORY only. The unmodified
continuation must reproduce the archive before action comparisons are trusted.
Only an exact live-state/context match can apply a frozen one-time decision.
"""
from __future__ import annotations

from copy import deepcopy
import math

from . import starship_physics as env, starship_sixdof as dyn
from . import starship_actual_recovery_shooting as shooting

SCHEMA = "missionos.starship_landing_onset_decision.v1"
BUNDLE_SCHEMA = "missionos.starship_frozen_boostback_bundle.v1"
CONFIG = {"maximum_local_calls": 4, "maximum_horizon_s": 40., "maximum_steps_per_call": 400,
    "clock_roundoff_tolerance_s": 1e-9,
    "allowed_delays_s": [0., .5], "runtime_preview_calls": 0,
    "baseline_equivalence_required": True, "predicted_full_handoff_required": True,
    "source_binding_independently_verified": False, "physical_execution": False}
BASELINE_SCHEMA = "missionos.starship_landing_baseline_binding.v1"
BASELINE_KEYS = {"schema", "source_run_sha256", "source_map_sha256", "profile_sha256", "catch_profile_sha256",
    "physical_configuration_sha256", "origin_context_sha256", "origin_state_sha256",
    "baseline_physical_checkpoints_sha256", "saved_physical_checkpoints_sha256", "baseline_checkpoint_count",
    "saved_checkpoint_count", "matched", "comparison_kind", "source_binding_is_caller_assertion",
    "source_authentication_independently_verified", "dynamics_replayed", "prediction_is_execution"}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _hash(value):
    return type(value) is str and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def local_macrostep(nominal_s, remaining_s):
    """Preserve a declared step only across its IEEE clock-roundoff boundary."""
    if not _number(nominal_s) or not _number(remaining_s) or nominal_s <= 0 or remaining_s <= 0:
        raise ValueError("invalid_local_landing_clock")
    return nominal_s if abs(remaining_s-nominal_s) <= CONFIG["clock_roundoff_tolerance_s"] else min(nominal_s, remaining_s)


def validate_baseline_binding(binding, snapshot, profile=None, catch=None):
    """Check a closed saved-data assertion; authentication remains external."""
    if (type(binding) is not dict or set(binding) != BASELINE_KEYS or binding.get("schema") != BASELINE_SCHEMA
            or any(not _hash(value) for key, value in binding.items() if key.endswith("sha256"))
            or binding["matched"] is not True or binding["source_binding_is_caller_assertion"] is not True
            or any(binding[key] is not False for key in
                ("source_authentication_independently_verified", "dynamics_replayed", "prediction_is_execution"))
            or binding["comparison_kind"] != "exact_common_timestamps_excluding_final_unexecuted_command"
            or type(binding["baseline_checkpoint_count"]) is not int
            or type(binding["saved_checkpoint_count"]) is not int
            or not 0 < binding["baseline_checkpoint_count"] <= CONFIG["maximum_steps_per_call"]
            or binding["saved_checkpoint_count"] != binding["baseline_checkpoint_count"]
            or binding["baseline_physical_checkpoints_sha256"] != binding["saved_physical_checkpoints_sha256"]
            or binding["origin_context_sha256"] != shooting.digest(snapshot)
            or binding["origin_state_sha256"] != shooting.digest(snapshot["state"])
            or binding["physical_configuration_sha256"] != shooting.digest(snapshot["physical_guidance_configuration"])
            or profile is not None and binding["profile_sha256"] != shooting.digest(profile)
            or catch is not None and binding["catch_profile_sha256"] != shooting.digest(catch)):
        raise ValueError("landing_baseline_saved_data_binding")


def compare_saved_baseline(baseline, source_run, snapshot, profile, catch, *, source_map):
    """Compare every executed CP at identical clocks, without interpolation."""
    points = baseline["recovery_record"]["checkpoints"]
    actual = [shooting.physical_checkpoint_view(point) for point in points if point["command"] is not None]
    source = {point["time_s"]: point for point in source_run["recovery_record"]["checkpoints"]}
    expected = [shooting.physical_checkpoint_view(source[point["time_s"]])
                for point in points if point["command"] is not None and point["time_s"] in source]
    metadata = baseline["forecast_metadata"]
    request = metadata["request"]
    matched = (metadata["failure"] is None and request["origin_context_sha256"] == shooting.digest(snapshot)
        and request["duration_s"] == CONFIG["maximum_horizon_s"]
        and request["maximum_integration_steps"] == CONFIG["maximum_steps_per_call"]
        and 0 < len(actual) <= CONFIG["maximum_steps_per_call"]
        and shooting.saved(actual) == shooting.saved(expected))
    return {"schema": BASELINE_SCHEMA, "source_run_sha256": shooting.digest(source_run),
        "source_map_sha256": shooting.digest(source_map), "profile_sha256": shooting.digest(profile),
        "catch_profile_sha256": shooting.digest(catch),
        "physical_configuration_sha256": shooting.digest(snapshot["physical_guidance_configuration"]),
        "origin_context_sha256": shooting.digest(snapshot), "origin_state_sha256": shooting.digest(snapshot["state"]),
        "baseline_physical_checkpoints_sha256": shooting.digest(actual),
        "saved_physical_checkpoints_sha256": shooting.digest(expected),
        "baseline_checkpoint_count": len(actual), "saved_checkpoint_count": len(expected), "matched": matched,
        "comparison_kind": "exact_common_timestamps_excluding_final_unexecuted_command",
        "source_binding_is_caller_assertion": True, "source_authentication_independently_verified": False,
        "dynamics_replayed": False, "prediction_is_execution": False}


def operative_context(snapshot):
    """Exclude only plan/provenance and supplemental frame diagnostics."""
    value = deepcopy(snapshot["context"])
    value.pop("plan")
    value["conditioned_reference"].pop("diagnostics")
    value["conditioned_reference"].pop("frame_diagnostics")
    return value


def frozen_boostback_bundle(run, profile, catch, *, source_map):
    receipt = run["recovery_record"]["actual_planning_receipt"]
    if (run["guidance_policy"] != "constrained_return_development_v11"
            or receipt["schema"] != shooting.TRANSPORT_SCHEMA or not receipt["baseline_equivalence"]["matched"]):
        raise ValueError("frozen_bundle_requires_matching_transport_planning_trace")
    plans = run["recovery_record"]["plans"]
    if len(plans) != 3:
        raise ValueError("frozen_bundle_requires_exact_initial_and_selected_plan_history")
    return shooting.saved({"schema": BUNDLE_SCHEMA, "source_run_sha256": shooting.digest(run),
        "source_map_sha256": shooting.digest(source_map),
        "profile_sha256": shooting.digest(profile), "catch_profile_sha256": shooting.digest(catch),
        "separation_state_sha256": shooting.digest(run["recovery_record"]["input_separation_state"]),
        "initial_plan": plans[0]["plan"], "refreshed_plan": plans[-1]["plan"],
        "refresh_time_s": receipt["origin_context"]["state"]["time_s"],
        "refresh_state_sha256": receipt["origin_state_sha256"],
        "refresh_operative_context_sha256": shooting.digest(operative_context(receipt["origin_context"])),
        "source_binding_is_caller_assertion": True, "source_binding_independently_verified": False,
        "runtime_short_forecast_calls": 0, "runtime_actual_forecast_calls": 0,
        "prediction_is_execution": False, "physical_execution": False})


def validate_bundle(bundle, profile, catch, initial_state):
    if (type(bundle) is not dict or bundle.get("schema") != BUNDLE_SCHEMA
            or bundle.get("profile_sha256") != shooting.digest(profile)
            or bundle.get("catch_profile_sha256") != shooting.digest(catch)
            or bundle.get("separation_state_sha256") != shooting.digest(initial_state)
            or bundle.get("runtime_short_forecast_calls") != 0 or bundle.get("runtime_actual_forecast_calls") != 0
            or any(bundle.get(key) is not False for key in
                ("physical_execution", "prediction_is_execution", "source_binding_independently_verified"))
            or bundle.get("source_binding_is_caller_assertion") is not True
            or any(not _hash(bundle.get(key)) for key in
                ("source_run_sha256", "source_map_sha256", "refresh_state_sha256", "refresh_operative_context_sha256"))
            or not _number(bundle.get("refresh_time_s"))):
        raise ValueError("frozen_boostback_bundle_input_binding")
    for plan in (bundle.get("initial_plan"), bundle.get("refreshed_plan")):
        if type(plan) is not dict or any(plan.get(key, False) is not False for key in
                ("physical_execution", "missionos_dispatch", "actual_state_assigned", "production_policy_admitted")):
            raise ValueError("frozen_boostback_bundle_claim_boundary")
        if (type(plan.get("burn_axis_enu")) is not list or len(plan["burn_axis_enu"]) != 3
                or not all(_number(value) for value in plan["burn_axis_enu"])
                or abs(math.hypot(*plan["burn_axis_enu"])-1.) > 1e-8
                or any(type(plan.get(key)) is not list or len(plan[key]) != 3
                    or not all(_number(value) for value in plan[key]) for key in
                    ("target_velocity_enu_mps", "bank_heading_enu"))
                or type(plan.get("bank_sign")) not in (int, float) or plan["bank_sign"] not in (-1., 1.)):
            raise ValueError("frozen_boostback_bundle_axis")


def validate_refresh(bundle, snapshot):
    if (snapshot["state"]["time_s"] != bundle["refresh_time_s"]
            or shooting.digest(snapshot["state"]) != bundle["refresh_state_sha256"]
            or shooting.digest(operative_context(snapshot)) != bundle["refresh_operative_context_sha256"]):
        raise ValueError("frozen_refresh_actual_state_or_controller_mismatch")


def reconstruct_coast_context(run, profile, catch, checkpoint_index):
    """Reconstruct past request history, not future/physical state evolution."""
    from .starship_attitude_reference import ConditionedGeographicFrame
    from .starship_constrained_recovery import physical_guidance_configuration
    from .starship_constrained_recovery import CONFIG as BASE_CONFIG
    from .starship_sixdof_booster import _navigation
    from .starship_sixdof_mission import _attitude
    from .starship_constrained_guidance import entry_axis
    from .starship_booster_recovery import _slew_axis
    from .starship_sixdof_mission import vehicle
    points = run["recovery_record"]["checkpoints"]
    target_point = points[checkpoint_index]
    if target_point["phase"] != "recovery_entry_coast" or checkpoint_index <= 0:
        raise ValueError("landing_decision_requires_pre_landing_coast_context")
    separation = run["recovery_record"]["input_separation_state"]
    rate_limit = profile["guidance"]["max_angular_acceleration_rad_s2"]/profile["guidance"]["attitude_frequency_rad_s"]
    frame = ConditionedGeographicFrame(separation["q_body_to_eci"], separation["time_s"], maximum_roll_rate_rad_s=rate_limit)
    tracker_data, previous_entry, preview = None, None, None
    next_preview, preview_digest = separation["time_s"], None
    latch, prior_reference = None, None
    booster = vehicle(profile, "booster")
    plan = run["recovery_record"]["plans"][-1]["plan"]
    for point in points[:checkpoint_index]:
        nav = point["navigation"]
        tracking = nav.get("reference_tracking")
        raw = tracking["raw_goal_q_body_to_eci"] if tracking else nav["target_q_body_to_eci"]
        state = dyn.state_from_dict(point["state"])
        up, east, north, _, _, _ = _navigation(state, profile)
        axis = dyn.rotate(raw, (0., 0., 1.))
        preferred = _attitude(axis, east)
        deferred = point["phase"] in ("recovery_powered_entry_prepare", "recovery_entry_shutdown_tail")
        frame.target(axis, east, preferred, time_s=point["time_s"],
            **({"force_bridge": True, "defer_geographic_reacquisition": True} if deferred else {}))
        # Restore the RECORDED requested frame exactly after using the source
        # geometry to reconstruct its branch flag. This is controller memory,
        # never a physical vehicle quaternion or an interpolated state.
        frame.frame.quaternion = tuple(raw)
        if point["phase"] == "recovery_entry_coast":
            # Recompute only the causal REQUEST governor from past saved
            # observations. Inverting the recorded quaternion loses floating
            # point bits and must not be substituted for this controller memory.
            observed = dyn.observe(state, booster)
            _, _, _, velocity, displacement, _ = _navigation(state, profile)
            position = [-env.dot(displacement, east), -env.dot(displacement, north), observed["altitude_m"]]
            local_v = [env.dot(velocity, vector) for vector in (east, north, up)]
            candidate, _ = entry_axis(position, local_v, profile, previous_axis=previous_entry,
                mass_kg=observed["mass_kg"], propellant_kg=state.propellant_kg, vehicle=booster,
                com_body_m=observed["com_body_m"], bank_heading_enu=plan["bank_heading_enu"], bank_sign=plan["bank_sign"],
                fixed_bank_angle_deg=BASE_CONFIG["fixed_entry_bank_angle_deg"],
                preparation_altitude_m=BASE_CONFIG["entry_preparation_altitude_m"])
            before = previous_entry or (0., 0., 1.)
            interval = .1 if observed["altitude_m"] < 100000. else .25
            previous_entry = _slew_axis(before, candidate, rate_limit*interval)
            if "landing_attitude_preparation" in nav:
                previous_entry = _slew_axis(before,
                    nav["landing_attitude_preparation"]["requested_axis_enu"], rate_limit*interval)
        if tracking:
            tracker_data = {"quaternion": tracking["requested_q_body_to_eci"], "time_s": point["time_s"],
                "rate_eci_rad_s": tracking["reference_rate_eci_rad_s"], "raw_goal_q": raw,
                "raw_goal_time_s": point["time_s"], "first_update": False,
                "maximum_rate_rad_s": tracking["maximum_reference_rate_rad_s"],
                "maximum_acceleration_rad_s2": tracking["maximum_reference_acceleration_rad_s2"]}
        if "braking_prediction" in nav:
            current = nav["braking_prediction"]
            current_digest = shooting.digest(current)
            if current_digest != preview_digest:
                next_preview = point["time_s"]+BASE_CONFIG["preview_interval_s"]
                preview_digest = current_digest
            preview = deepcopy(current)
        latch = nav.get("entry_pretrim_prepared_at_s")
        prior_reference = {"quaternion": nav["target_q_body_to_eci"], "time_s": point["time_s"]}
    events = [event for event in run["events"] if event["time_s"] <= target_point["time_s"]]
    def event_time(name):
        return next(event["time_s"] for event in events if event["event"] == name)
    ctx = {"conditioned_reference": {"quaternion": list(frame.frame.quaternion), "time_s": frame.time_s,
        "maximum_roll_rate_rad_s": frame.maximum_roll_rate_rad_s, "bridging": frame.bridging,
        "diagnostics": deepcopy(frame.diagnostics), "frame_diagnostics": deepcopy(frame.frame.diagnostics)},
        "reference_tracker": tracker_data}
    frame, tracker = shooting.restore_references(ctx, profile)
    snapshot = shooting.capture_context(dyn.state_from_dict(target_point["state"]), profile, catch,
        physical_guidance_configuration(True), phase="recovery_entry_coast", start_time_s=separation["time_s"],
        deadline_s=separation["time_s"]+run["recovery_record"]["resolved_duration_s"],
        plan=run["recovery_record"]["plans"][-1]["plan"], refreshed=True,
        burn_start_s=event_time("boostback_ignition"), settle_start_s=event_time("constrained_boostback_cutoff"),
        previous_entry_axis_enu=previous_entry, entry_pretrim_prepared_at_s=latch,
        next_preview_s=next_preview, braking_preview=preview, prior_command_reference=prior_reference,
        conditioned_reference=frame, reference_tracker=tracker, prepare_tail_start_s=event_time("powered_coast_attitude_prepared"))
    return snapshot


def decision_from_prediction(snapshot, delay_s, prediction, *, baseline_binding):
    if delay_s not in CONFIG["allowed_delays_s"] or type(delay_s) not in (int, float):
        raise ValueError("unsupported_landing_decision_delay")
    validate_baseline_binding(baseline_binding, snapshot)
    handoff = prediction["recovery_record"]["handoff"]
    if (prediction["forecast_metadata"]["failure"] is not None
            or prediction["forecast_metadata"]["prediction_complete"] is not True
            or prediction["outcome"]["termination"] != "catch_handoff" or handoff["eligible"] is not True
            or handoff["observation"]["eligible"] is not True
            or shooting.saved(handoff["state"]) != shooting.saved(prediction["final_state"])):
        raise ValueError("landing_decision_requires_predicted_full_handoff")
    return {"schema": SCHEMA, "origin_time_s": snapshot["state"]["time_s"], "origin_context": deepcopy(snapshot),
        "origin_state_sha256": shooting.digest(snapshot["state"]), "origin_operative_context_sha256": shooting.digest(operative_context(snapshot)),
        "delay_s": float(delay_s), "scheduled_onset_s": snapshot["state"]["time_s"]+delay_s,
        "predicted_handoff_state_sha256": shooting.digest(prediction["final_state"]),
        "prediction_sha256": shooting.digest(prediction),
        "predicted_handoff_observation_sha256": shooting.digest(handoff["observation"]),
        "baseline_binding": deepcopy(baseline_binding), "runtime_preview_calls": 0,
        "prediction_is_execution": False, "arrival_admitted": False, "support_admitted": False, "physical_execution": False}


def validate_live_decision(decision, snapshot):
    if (type(decision) is not dict or decision.get("schema") != SCHEMA or type(decision.get("runtime_preview_calls")) is not int
            or decision["runtime_preview_calls"] != 0 or not _number(decision.get("delay_s"))
            or decision["delay_s"] not in CONFIG["allowed_delays_s"]
            or not _number(decision.get("origin_time_s")) or not _number(decision.get("scheduled_onset_s"))
            or decision["scheduled_onset_s"] != decision["origin_time_s"]+decision["delay_s"]
            or any(decision.get(key) is not False for key in
                ("physical_execution", "prediction_is_execution", "arrival_admitted", "support_admitted"))
            or type(decision.get("origin_context")) is not dict
            or type(decision.get("baseline_binding")) is not dict
            or shooting.digest(decision["origin_context"]) != decision["baseline_binding"]["origin_context_sha256"]
            or decision["origin_time_s"] != decision["origin_context"]["state"]["time_s"]
            or any(not _hash(decision.get(key)) for key in
                ("prediction_sha256", "predicted_handoff_state_sha256", "predicted_handoff_observation_sha256"))
            or snapshot["state"]["time_s"] != decision["origin_time_s"]
            or shooting.digest(snapshot["state"]) != decision["origin_state_sha256"]
            or shooting.digest(operative_context(snapshot)) != decision["origin_operative_context_sha256"]):
        raise ValueError("landing_decision_live_context_mismatch")
    validate_baseline_binding(decision["baseline_binding"], decision["origin_context"])


def forecast_landing_decision(snapshot, profile, catch, *, delay_s, request):
    from .starship_constrained_recovery import simulate_constrained_recovery
    if delay_s is not None and (type(delay_s) not in (int, float) or delay_s not in (0., .5)):
        raise ValueError("unsupported_landing_decision_delay")
    if (request.get("duration_s") != CONFIG["maximum_horizon_s"]
            or request.get("maximum_integration_steps") != CONFIG["maximum_steps_per_call"]):
        raise ValueError("landing_forecast_requires_declared_local_budget")
    return simulate_constrained_recovery(profile, snapshot["state"], catch, duration_s=request["duration_s"],
        _resume_context=deepcopy(snapshot), _forecast_request=deepcopy(request), _forecast_landing_delay_s=delay_s,
        _forecast_landing_local=True)
