from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any


RECOVERY_INTENT_SCHEMA_VERSION = "missionos_runtime_recovery_intent.v1"
RECOVERY_COMPILATION_SCHEMA_VERSION = (
    "missionos_runtime_recovery_intent_compilation.v1"
)
RECOVERY_REACHABILITY_SCHEMA_VERSION = (
    "missionos_runtime_recovery_reachability_verification.v1"
)
RECOVERY_OUTCOME_VERIFICATION_SCHEMA_VERSION = (
    "missionos_runtime_recovery_outcome_verification.v1"
)

PARAMETERIZED_RECOVERY_ACTIONS = frozenset(
    {"adjust_altitude", "reroute", "avoid_obstacle"}
)

ACTION_STRATEGIES = {
    "continue": "monitor",
    "hold": "hold",
    "operator_review": "hold",
    "return_to_launch": "rtl_or_land",
    "land": "rtl_or_land",
    "adjust_altitude": "local_avoidance",
    "adjust_speed": "local_avoidance",
    "avoid_obstacle": "local_avoidance",
    "reroute": "global_reroute",
}

ALLOWED_INTENT_CONSTRAINTS = frozenset(
    {
        "avoidance_side",
        "destination_kind",
        "maximum_duration_s",
        "maximum_speed_mps",
        "minimum_clearance_m",
        "target_altitude_max_m",
        "target_altitude_min_m",
    }
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _number(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _first_number(*values: Any) -> float | None:
    for value in values:
        parsed = _number(value)
        if parsed is not None:
            return parsed
    return None


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _artifact_with_hash(payload: Mapping[str, Any], *, id_prefix: str) -> dict[str, Any]:
    unhashed = dict(payload)
    digest = _canonical_sha256(unhashed)
    return {
        **unhashed,
        f"{id_prefix}_sha256": digest,
        f"{id_prefix}_id": f"{id_prefix}_{digest[:12]}",
    }


def recovery_artifact_hash_matches(
    artifact: Mapping[str, Any],
    *,
    id_prefix: str,
) -> bool:
    expected = str(artifact.get(f"{id_prefix}_sha256") or "")
    unhashed = {
        key: value
        for key, value in artifact.items()
        if key not in {f"{id_prefix}_id", f"{id_prefix}_sha256"}
    }
    return bool(expected and expected == _canonical_sha256(unhashed))


def _strategy_for_action(action: str) -> str:
    return ACTION_STRATEGIES.get(action, "hold")


def build_runtime_recovery_intent(
    *,
    agent_output: Mapping[str, Any],
    observed_at: str = "",
    decision_signature: str = "",
) -> dict[str, Any]:
    """Normalize hosted-model judgment without granting execution authority."""

    action = str(
        agent_output.get("selected_bounded_action")
        or agent_output.get("response_kind")
        or ""
    ).strip()
    explicit_strategy = str(agent_output.get("strategy") or "").strip()
    inferred_strategy = _strategy_for_action(action)
    constraints = _mapping(agent_output.get("intent_constraints"))
    requested_parameters = _mapping(agent_output.get("proposed_parameters"))
    reasons: list[str] = []
    if not action:
        reasons.append("recovery_intent_action_missing")
    if explicit_strategy and explicit_strategy != inferred_strategy:
        reasons.append("recovery_intent_strategy_action_mismatch")
    unsupported = sorted(set(constraints) - ALLOWED_INTENT_CONSTRAINTS)
    if unsupported:
        reasons.extend(
            f"unsupported_recovery_intent_constraint:{name}" for name in unsupported
        )
    avoidance_side = str(constraints.get("avoidance_side") or "").strip()
    if avoidance_side and avoidance_side not in {"left", "right"}:
        reasons.append("recovery_intent_avoidance_side_not_supported")
    destination_kind = str(constraints.get("destination_kind") or "").strip()
    if destination_kind and destination_kind not in {
        "alternate_dropoff",
        "original_route",
    }:
        reasons.append("recovery_intent_destination_kind_not_supported")
    for name in (
        "maximum_duration_s",
        "maximum_speed_mps",
        "minimum_clearance_m",
        "target_altitude_max_m",
        "target_altitude_min_m",
    ):
        if name in constraints and (
            _number(constraints.get(name)) is None
            or float(constraints[name]) <= 0.0
        ):
            reasons.append(f"recovery_intent_constraint_not_positive:{name}")
    altitude_minimum = _number(constraints.get("target_altitude_min_m"))
    altitude_maximum = _number(constraints.get("target_altitude_max_m"))
    if (
        altitude_minimum is not None
        and altitude_maximum is not None
        and altitude_minimum > altitude_maximum
    ):
        reasons.append("recovery_intent_altitude_envelope_invalid")
    payload = {
        "schema_version": RECOVERY_INTENT_SCHEMA_VERSION,
        "intent_status": "invalid" if reasons else "valid",
        "strategy": explicit_strategy or inferred_strategy,
        "selected_action": action,
        "intent_constraints": constraints,
        "requested_parameters": requested_parameters,
        "rationale": str(agent_output.get("rationale") or "")[:1000],
        "observed_at": str(observed_at or ""),
        "decision_signature": str(decision_signature or ""),
        "blocking_reasons": reasons,
        "requires_human_approval": bool(
            agent_output.get("requires_human_approval", True)
        ),
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    return _artifact_with_hash(payload, id_prefix="recovery_intent")


def _parameter_match(
    requested: Mapping[str, Any],
    compiled: Mapping[str, Any],
    *,
    tolerance: float = 0.05,
) -> bool:
    for key, expected in requested.items():
        actual = compiled.get(key)
        expected_number = _number(expected)
        actual_number = _number(actual)
        if expected_number is not None:
            if actual_number is None or abs(actual_number - expected_number) > tolerance:
                return False
        elif actual != expected:
            return False
    return True


def compile_runtime_recovery_intent(
    *,
    intent: Mapping[str, Any],
    candidate: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Compile intent only when the concrete candidate preserves its meaning."""

    requested_action = str(intent.get("selected_action") or "").strip()
    compiled_action = str(candidate.get("selected_bounded_action") or "").strip()
    requested_parameters = _mapping(intent.get("requested_parameters"))
    compiled_parameters = _mapping(candidate.get("proposed_parameters"))
    constraints = _mapping(intent.get("intent_constraints"))
    basis = _mapping(candidate.get("basis"))
    reasons = [str(item) for item in intent.get("blocking_reasons") or []]
    if intent.get("intent_status") != "valid":
        reasons.append("recovery_intent_invalid")
    if not compiled_action:
        reasons.append("recovery_compiler_candidate_missing")
    elif compiled_action != requested_action:
        reasons.append("recovery_compiler_changed_action_meaning")
    if requested_parameters and not _parameter_match(
        requested_parameters,
        compiled_parameters,
    ):
        reasons.append("recovery_compiler_changed_requested_parameters")

    avoidance_side = str(constraints.get("avoidance_side") or "").strip()
    if avoidance_side and avoidance_side != str(basis.get("avoidance_side") or ""):
        reasons.append("recovery_compiler_cannot_preserve_avoidance_side")
    minimum_clearance = _number(constraints.get("minimum_clearance_m"))
    compiled_clearance = _number(
        basis.get("minimum_lateral_clearance_m")
        or basis.get("required_lateral_clearance_m")
    )
    if minimum_clearance is not None and (
        compiled_clearance is None or compiled_clearance < minimum_clearance
    ):
        reasons.append("recovery_compiler_cannot_preserve_minimum_clearance")
    altitude = _number(compiled_parameters.get("target_altitude_m"))
    altitude_minimum = _number(constraints.get("target_altitude_min_m"))
    altitude_maximum = _number(constraints.get("target_altitude_max_m"))
    if altitude_minimum is not None and (altitude is None or altitude < altitude_minimum):
        reasons.append("recovery_compiler_target_altitude_below_intent_minimum")
    if altitude_maximum is not None and (altitude is None or altitude > altitude_maximum):
        reasons.append("recovery_compiler_target_altitude_above_intent_maximum")
    destination_kind = str(constraints.get("destination_kind") or "").strip()
    compiled_destination_kind = (
        "alternate_dropoff"
        if compiled_parameters.get("alternate_dropoff") is True
        else "original_route"
    )
    if destination_kind and destination_kind != compiled_destination_kind:
        reasons.append("recovery_compiler_changed_destination_meaning")

    reasons = list(dict.fromkeys(reasons))
    compiled = not reasons
    payload = {
        "schema_version": RECOVERY_COMPILATION_SCHEMA_VERSION,
        "compilation_status": "compiled" if compiled else "infeasible",
        "source_intent_id": str(intent.get("recovery_intent_id") or ""),
        "source_intent_sha256": str(intent.get("recovery_intent_sha256") or ""),
        "requested_strategy": str(intent.get("strategy") or ""),
        "requested_action": requested_action,
        "compiled_action": compiled_action if compiled else "",
        "requested_parameters": requested_parameters,
        "compiled_parameters": compiled_parameters if compiled else {},
        "intent_constraints": constraints,
        "candidate_basis": basis,
        "candidate_source_refs": [
            str(item) for item in candidate.get("source_refs") or [] if str(item)
        ],
        "policy_ref": str(
            recovery_policy.get("policy_ref")
            or recovery_policy.get("recovery_policy_ref")
            or ""
        ),
        "policy_snapshot": {
            key: recovery_policy.get(key)
            for key in (
                "battery_return_threshold_percent",
                "max_adjust_altitude_m",
                "max_recovery_duration_s",
                "max_recovery_horizontal_speed_mps",
                "max_recovery_vertical_speed_mps",
                "max_reroute_target_abs_m",
                "reachability_duration_margin_factor",
                "reachability_setup_seconds",
                "max_wind_speed_mps",
                "wind_uncertainty_floor_mps",
            )
            if recovery_policy.get(key) is not None
        },
        "meaning_preserved": compiled,
        "blocking_reasons": reasons,
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    return _artifact_with_hash(payload, id_prefix="recovery_compilation")


def verify_runtime_recovery_reachability(
    *,
    compilation: Mapping[str, Any],
    telemetry_snapshot: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Check conservative reachability bounds without claiming future arrival."""

    reasons: list[str] = []
    action = str(compilation.get("compiled_action") or "").strip()
    parameters = _mapping(compilation.get("compiled_parameters"))
    position = _mapping(telemetry_snapshot.get("position"))
    telemetry = _mapping(telemetry_snapshot.get("telemetry"))
    battery = _mapping(telemetry_snapshot.get("battery"))
    wind = _mapping(telemetry_snapshot.get("wind"))
    if compilation.get("compilation_status") != "compiled":
        reasons.append("recovery_reachability_requires_compiled_intent")
    if telemetry.get("stale") is True:
        reasons.append("recovery_reachability_telemetry_stale")

    current_x = _number(position.get("local_x_m"))
    current_y = _number(position.get("local_y_m"))
    current_altitude = _first_number(
        position.get("altitude_above_home_m"),
        telemetry_snapshot.get("altitude_above_home_m"),
    )
    target_x = _number(parameters.get("target_x_m"))
    target_y = _number(parameters.get("target_y_m"))
    target_altitude = _number(parameters.get("target_altitude_m"))

    horizontal_distance: float | None = 0.0
    if action in {"reroute", "avoid_obstacle"}:
        if None in {current_x, current_y, target_x, target_y}:
            horizontal_distance = None
            reasons.append("recovery_reachability_horizontal_geometry_missing")
        else:
            horizontal_distance = math.hypot(
                float(target_x) - float(current_x),
                float(target_y) - float(current_y),
            )
    vertical_distance: float | None = 0.0
    if target_altitude is not None:
        if current_altitude is None:
            vertical_distance = None
            reasons.append("recovery_reachability_current_altitude_missing")
        else:
            vertical_distance = abs(target_altitude - current_altitude)
    elif action == "adjust_altitude":
        vertical_distance = None
        reasons.append("recovery_reachability_target_altitude_missing")

    constraints = _mapping(compilation.get("intent_constraints"))
    max_horizontal_speed = _first_number(
        constraints.get("maximum_speed_mps"),
        recovery_policy.get("max_recovery_horizontal_speed_mps"),
    ) or 10.0
    max_vertical_speed = _number(
        recovery_policy.get("max_recovery_vertical_speed_mps")
    ) or 3.0
    wind_speed = _first_number(
        wind.get("speed_mps"),
        wind.get("observed_speed_mps"),
        telemetry_snapshot.get("wind_speed_mps"),
    ) or 0.0
    wind_gust = _first_number(
        wind.get("gust_mps"),
        wind.get("observed_gust_mps"),
        telemetry_snapshot.get("wind_gust_mps"),
    )
    wind_uncertainty = max(
        _number(recovery_policy.get("wind_uncertainty_floor_mps")) or 1.0,
        max(0.0, (wind_gust or wind_speed) - wind_speed),
    )
    conservative_horizontal_speed = max(
        0.0,
        max_horizontal_speed - wind_speed - wind_uncertainty,
    )
    if horizontal_distance and conservative_horizontal_speed <= 0.1:
        reasons.append("recovery_reachability_control_margin_not_positive")
    horizontal_duration = (
        horizontal_distance / conservative_horizontal_speed
        if horizontal_distance is not None
        and horizontal_distance > 0.0
        and conservative_horizontal_speed > 0.1
        else 0.0
        if horizontal_distance == 0.0
        else None
    )
    vertical_duration = (
        vertical_distance / max_vertical_speed
        if vertical_distance is not None
        else None
    )
    duration_margin = _number(
        recovery_policy.get("reachability_duration_margin_factor")
    ) or 1.25
    setup_seconds = _number(recovery_policy.get("reachability_setup_seconds")) or 5.0
    upper_bound_duration = (
        max(horizontal_duration, vertical_duration) * duration_margin + setup_seconds
        if horizontal_duration is not None and vertical_duration is not None
        else None
    )
    available_duration = _first_number(
        constraints.get("maximum_duration_s"),
        recovery_policy.get("max_recovery_duration_s"),
    ) or 75.0
    if upper_bound_duration is None:
        reasons.append("recovery_reachability_duration_bound_missing")
    elif upper_bound_duration > available_duration:
        reasons.append("recovery_reachability_duration_bound_exceeds_envelope")

    max_abs_m = _number(recovery_policy.get("max_reroute_target_abs_m")) or 5000.0
    if target_x is not None and abs(target_x) > max_abs_m:
        reasons.append("recovery_reachability_target_outside_local_geofence")
    if target_y is not None and abs(target_y) > max_abs_m:
        reasons.append("recovery_reachability_target_outside_local_geofence")
    endurance = _mapping(battery.get("endurance_projection"))
    if endurance.get("projected_insufficient_for_route") is True:
        reasons.append("recovery_reachability_battery_envelope_insufficient")

    reasons = list(dict.fromkeys(reasons))
    payload = {
        "schema_version": RECOVERY_REACHABILITY_SCHEMA_VERSION,
        "verification_status": "verified" if not reasons else "unverified",
        "source_compilation_id": str(
            compilation.get("recovery_compilation_id") or ""
        ),
        "source_compilation_sha256": str(
            compilation.get("recovery_compilation_sha256") or ""
        ),
        "action": action,
        "horizontal_distance_m": (
            round(horizontal_distance, 3) if horizontal_distance is not None else None
        ),
        "vertical_distance_m": (
            round(vertical_distance, 3) if vertical_distance is not None else None
        ),
        "max_horizontal_speed_mps": round(max_horizontal_speed, 3),
        "conservative_horizontal_speed_mps": round(
            conservative_horizontal_speed, 3
        ),
        "max_vertical_speed_mps": round(max_vertical_speed, 3),
        "wind_speed_mps": round(wind_speed, 3),
        "wind_uncertainty_mps": round(wind_uncertainty, 3),
        "estimated_duration_s": (
            round(max(horizontal_duration or 0.0, vertical_duration or 0.0), 3)
            if horizontal_duration is not None and vertical_duration is not None
            else None
        ),
        "upper_bound_duration_s": (
            round(upper_bound_duration, 3)
            if upper_bound_duration is not None
            else None
        ),
        "available_duration_s": round(available_duration, 3),
        "reachability_verified": not reasons,
        "blocking_reasons": reasons,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    return _artifact_with_hash(payload, id_prefix="recovery_reachability")


def verify_runtime_recovery_outcome(
    *,
    action: str,
    recovery_observation: Mapping[str, Any],
    dispatch_authority_created: bool,
) -> dict[str, Any]:
    """Verify observed effect separately from ACK and dispatch authority."""

    ack_observed = recovery_observation.get("command_ack_observed") is True
    assist_attempted = recovery_observation.get("assist_attempted") is True
    target_reached = recovery_observation.get("target_reached") is True
    resume_status = str(recovery_observation.get("resume_status") or "")
    resume_verification = _mapping(
        recovery_observation.get("resume_safety_verification")
    )
    target_dependent = action in PARAMETERIZED_RECOVERY_ACTIONS
    reasons: list[str] = []
    if not dispatch_authority_created:
        reasons.append("recovery_outcome_dispatch_authority_not_observed")
    if not ack_observed:
        reasons.append("recovery_outcome_command_ack_not_observed")
    if not assist_attempted:
        reasons.append("recovery_outcome_executor_effect_not_observed")
    if target_dependent and not target_reached:
        reasons.append("recovery_outcome_target_not_reached")
    if resume_status == "resumed_auto_mission" and not target_reached:
        reasons.append("recovery_outcome_auto_resumed_without_target")
    if resume_status == "resumed_auto_mission" and (
        resume_verification.get("verification_status") != "verified"
        or resume_verification.get("resume_auto_authorized") is not True
    ):
        reasons.append("recovery_outcome_auto_resume_not_verified")
    reasons = list(dict.fromkeys(reasons))
    payload = {
        "schema_version": RECOVERY_OUTCOME_VERIFICATION_SCHEMA_VERSION,
        "verification_status": "verified" if not reasons else "failed",
        "action": action,
        "dispatch_authority_observed": dispatch_authority_created,
        "command_ack_observed": ack_observed,
        "executor_effect_observed": assist_attempted,
        "target_reached": target_reached,
        "resume_status": resume_status,
        "resume_safety_verification": resume_verification,
        "ack_is_execution_effect": False,
        "recovery_success_verified": not reasons,
        "blocking_reasons": reasons,
        "delivery_completion_claimed": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    return _artifact_with_hash(payload, id_prefix="recovery_outcome_verification")


__all__ = [
    "ACTION_STRATEGIES",
    "PARAMETERIZED_RECOVERY_ACTIONS",
    "RECOVERY_COMPILATION_SCHEMA_VERSION",
    "RECOVERY_INTENT_SCHEMA_VERSION",
    "RECOVERY_OUTCOME_VERIFICATION_SCHEMA_VERSION",
    "RECOVERY_REACHABILITY_SCHEMA_VERSION",
    "build_runtime_recovery_intent",
    "compile_runtime_recovery_intent",
    "recovery_artifact_hash_matches",
    "verify_runtime_recovery_outcome",
    "verify_runtime_recovery_reachability",
]
