"""Fail-closed evidence for wind-to-obstacle Recovery transitions."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any


WIND_SAFE_WINDOW_SCHEMA_VERSION = "missionos_runtime_recovery_wind_safe_window.v1"
TELEMETRY_ARBITRATION_SCHEMA_VERSION = (
    "missionos_runtime_recovery_telemetry_arbitration.v1"
)
DEFAULT_TELEMETRY_CURSOR_MAX_ELAPSED_DELTA_S = 15.0
COMPOUND_HAZARD_STATE_SCHEMA_VERSION = (
    "missionos_runtime_recovery_compound_hazard_state.v1"
)


def _number(*values: Any) -> float | None:
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed):
            return parsed
    return None


def telemetry_cursor(value: Mapping[str, Any]) -> dict[str, Any]:
    sample_index = _number(value.get("sample_index"))
    elapsed_seconds = _number(value.get("elapsed_seconds"))
    complete = bool(
        sample_index is not None
        and float(sample_index).is_integer()
        and sample_index >= 0
        and elapsed_seconds is not None
        and elapsed_seconds >= 0
    )
    return {
        "cursor_status": "complete" if complete else "incomplete",
        "sample_index": int(sample_index) if complete else None,
        "elapsed_seconds": float(elapsed_seconds) if complete else None,
    }


def arbitrate_latest_telemetry(
    *,
    bridge_telemetry: Mapping[str, Any],
    runtime_telemetry: Mapping[str, Any],
    maximum_elapsed_delta_s: float = DEFAULT_TELEMETRY_CURSOR_MAX_ELAPSED_DELTA_S,
) -> dict[str, Any]:
    """Select the newest source only when both cursor dimensions agree."""

    maximum_elapsed_delta_s = max(0.001, float(maximum_elapsed_delta_s))
    bridge_present = bool(bridge_telemetry)
    runtime_present = bool(runtime_telemetry)
    bridge_cursor = telemetry_cursor(bridge_telemetry)
    runtime_cursor = telemetry_cursor(runtime_telemetry)
    reasons: list[str] = []
    selected_source = ""
    selected_telemetry: dict[str, Any] = {}
    if not bridge_present or not runtime_present:
        reasons.append("telemetry_arbitration_source_missing")
    if bridge_present and bridge_cursor["cursor_status"] != "complete":
        reasons.append("telemetry_arbitration_bridge_cursor_incomplete")
    if runtime_present and runtime_cursor["cursor_status"] != "complete":
        reasons.append("telemetry_arbitration_runtime_cursor_incomplete")
    if not reasons:
        index_delta = int(runtime_cursor["sample_index"]) - int(
            bridge_cursor["sample_index"]
        )
        elapsed_delta = float(runtime_cursor["elapsed_seconds"]) - float(
            bridge_cursor["elapsed_seconds"]
        )
        if (index_delta == 0) != (abs(elapsed_delta) <= 1e-6):
            reasons.append("telemetry_arbitration_cursor_dimensions_disagree")
        elif index_delta * elapsed_delta < 0:
            reasons.append("telemetry_arbitration_cursor_regression")
        elif abs(elapsed_delta) > maximum_elapsed_delta_s:
            reasons.append("telemetry_arbitration_elapsed_delta_exceeded")
        else:
            if index_delta > 0:
                selected_source = "missionos_auto_mission_runtime_snapshot"
                selected_telemetry = dict(runtime_telemetry)
            elif index_delta < 0:
                selected_source = "missionos_runtime_recovery_agent_live_bridge"
                selected_telemetry = dict(bridge_telemetry)
            else:
                selected_source = "cursor_match"
                # The live bridge carries the normalized obstacle/conflict and
                # recovery facts. Exact cursor equality proves they describe
                # the same raw runtime sample.
                selected_telemetry = dict(bridge_telemetry)
    return {
        "schema_version": TELEMETRY_ARBITRATION_SCHEMA_VERSION,
        "arbitration_status": "verified" if not reasons else "unverified",
        "selected_source": selected_source or None,
        "selected_telemetry": selected_telemetry,
        "bridge_cursor": bridge_cursor,
        "runtime_cursor": runtime_cursor,
        "maximum_elapsed_delta_s": maximum_elapsed_delta_s,
        "blocking_reasons": list(dict.fromkeys(reasons)),
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def safe_window_tail_matches_telemetry(
    samples: Sequence[Mapping[str, Any]],
    telemetry: Mapping[str, Any],
) -> dict[str, Any]:
    latest_cursor = telemetry_cursor(telemetry)
    tail_cursor = telemetry_cursor(samples[-1]) if samples else telemetry_cursor({})
    matched = bool(
        latest_cursor["cursor_status"] == "complete"
        and tail_cursor["cursor_status"] == "complete"
        and latest_cursor["sample_index"] == tail_cursor["sample_index"]
        and abs(
            float(latest_cursor["elapsed_seconds"])
            - float(tail_cursor["elapsed_seconds"])
        )
        <= 1e-6
    )
    return {
        "match_status": "matched" if matched else "unmatched",
        "matched": matched,
        "latest_telemetry_cursor": latest_cursor,
        "safe_window_tail_cursor": tail_cursor,
    }


def build_compound_hazard_state(
    *,
    task_id: str,
    prior_state: Mapping[str, Any],
    telemetry: Mapping[str, Any],
    recovery_window_samples: Sequence[Mapping[str, Any]],
    recovery_policy: Mapping[str, Any],
    wind_safe_window: Mapping[str, Any],
    safety_hold_observed: bool,
    safety_hold_stable: bool,
    observed_at: str,
) -> dict[str, Any]:
    """Persist source-backed compound facts before any LLM proposal exists."""

    obstacle = telemetry.get("obstacle")
    obstacle = obstacle if isinstance(obstacle, Mapping) else {}
    conflict = obstacle.get("conflict_assessment")
    conflict = conflict if isinstance(conflict, Mapping) else {}
    nearest = conflict.get("nearest_obstacle")
    nearest = nearest if isinstance(nearest, Mapping) else {}
    obstacle_name = str(nearest.get("obstacle_name") or "").strip()
    local_conflict = bool(
        conflict.get("local_avoidance_required") is True and obstacle_name
    )
    wind = telemetry.get("wind")
    wind = wind if isinstance(wind, Mapping) else {}
    wind_speed_mps = _number(
        wind.get("speed_mps"),
        wind.get("observed_speed_mps"),
        telemetry.get("wind_speed_mps"),
    )
    wind_limit_mps = _number(recovery_policy.get("max_wind_speed_mps"))
    wind_above_limit = bool(
        wind_speed_mps is not None
        and wind_limit_mps is not None
        and wind_speed_mps > wind_limit_mps
    )
    prior_obstacle_name = str(prior_state.get("source_obstacle_name") or "")
    same_unresolved_hazard = bool(
        prior_state.get("hazard_state_id")
        and prior_obstacle_name == obstacle_name
        and prior_state.get("hazard_status")
        not in {"proposal_created", "hazard_cleared"}
    )
    if not (local_conflict and (wind_above_limit or same_unresolved_hazard)):
        if prior_state.get("hazard_state_id") and not local_conflict:
            return {
                **dict(prior_state),
                "hazard_status": "hazard_cleared",
                "latest_telemetry_cursor": telemetry_cursor(telemetry),
                "observed_at": observed_at,
                "dispatch_authority_created": False,
                "physical_execution_invoked": False,
                "progress_counted": False,
            }
        return {}

    current_cursor = telemetry_cursor(telemetry)
    if same_unresolved_hazard:
        hazard_state_id = str(prior_state.get("hazard_state_id") or "")
        first_observed_cursor = prior_state.get("first_observed_cursor")
        first_observed_at = prior_state.get("first_observed_at")
        judgment_epoch = prior_state.get("judgment_epoch")
        judgment_epoch = (
            dict(judgment_epoch)
            if isinstance(judgment_epoch, Mapping)
            else {}
        )
    else:
        identity = {
            "task_id": task_id,
            "source_obstacle_name": obstacle_name,
            "first_observed_cursor": current_cursor,
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        hazard_state_id = f"compound_hazard_state_{digest[:12]}"
        first_observed_cursor = current_cursor
        first_observed_at = observed_at
        judgment_epoch = {
            "epoch_index": 0,
            "epoch_status": "waiting_for_safe_window",
            "attempt_count": 0,
        }
    safe_window_verified = bool(
        wind_safe_window.get("safe_window_observed") is True
        and safety_hold_observed
        and safety_hold_stable
    )
    if (
        safe_window_verified
        and judgment_epoch.get("epoch_status")
        == "waiting_for_safe_window"
    ):
        epoch_index = int(judgment_epoch.get("epoch_index") or 0) + 1
        epoch_material = {
            "hazard_state_id": hazard_state_id,
            "epoch_index": epoch_index,
            "safe_window_tail": telemetry_cursor(telemetry),
        }
        epoch_digest = hashlib.sha256(
            json.dumps(
                epoch_material,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        judgment_epoch = {
            "epoch_id": f"compound_judgment_epoch_{epoch_digest[:12]}",
            "epoch_index": epoch_index,
            "epoch_status": "ready",
            "attempt_count": 0,
            "opened_at": observed_at,
        }
    hazard_status = (
        "wind_above_limit_observed"
        if wind_above_limit
        else "rejudgment_ready"
        if safe_window_verified
        else "wind_safe_window_tracking"
    )
    return {
        "schema_version": COMPOUND_HAZARD_STATE_SCHEMA_VERSION,
        "hazard_state_id": hazard_state_id,
        "hazard_status": hazard_status,
        "hazard_kind": "wind_and_source_backed_route_obstacle",
        "source_backed": True,
        "source_obstacle_name": obstacle_name,
        "local_avoidance_required": True,
        "wind_speed_mps": wind_speed_mps,
        "max_wind_speed_mps": wind_limit_mps,
        "wind_above_limit": wind_above_limit,
        "wind_safe_window": dict(wind_safe_window),
        "safety_hold_observed": safety_hold_observed,
        "safety_hold_stable": safety_hold_stable,
        "first_observed_cursor": first_observed_cursor,
        "first_observed_at": first_observed_at,
        "latest_telemetry_cursor": current_cursor,
        "recovery_window_samples": [
            dict(sample)
            for sample in recovery_window_samples
            if isinstance(sample, Mapping)
        ],
        "judgment_epoch": judgment_epoch,
        "observed_at": observed_at,
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def build_wind_safe_window_evidence(
    samples: Sequence[Mapping[str, Any]],
    *,
    recovery_policy: Mapping[str, Any],
    minimum_window_s: float,
    maximum_sample_gap_s: float,
) -> dict[str, Any]:
    """Verify a bounded fresh-telemetry window below the recovery wind limit."""

    wind_limit_mps = _number(recovery_policy.get("max_wind_speed_mps"))
    minimum_window_s = max(0.001, float(minimum_window_s))
    maximum_sample_gap_s = max(0.001, float(maximum_sample_gap_s))
    normalized: list[tuple[float, Mapping[str, Any]]] = []
    for sample in samples:
        if not isinstance(sample, Mapping):
            continue
        elapsed_s = _number(sample.get("elapsed_seconds"))
        if elapsed_s is not None:
            normalized.append((elapsed_s, sample))
    normalized.sort(key=lambda item: item[0])
    latest_elapsed_s = normalized[-1][0] if normalized else None
    window_start_s = (
        latest_elapsed_s - minimum_window_s
        if latest_elapsed_s is not None
        else None
    )
    in_window = [
        (elapsed_s, sample)
        for elapsed_s, sample in normalized
        if window_start_s is not None and elapsed_s > window_start_s
    ]
    predecessors = [
        (elapsed_s, sample)
        for elapsed_s, sample in normalized
        if window_start_s is not None and elapsed_s <= window_start_s
    ]
    fresh_predecessors = [
        item
        for item in predecessors
        if not (
            isinstance(item[1].get("telemetry"), Mapping)
            and item[1]["telemetry"].get("stale") is True
        )
    ]
    window_samples = (
        [fresh_predecessors[-1]] if fresh_predecessors else []
    ) + in_window
    fresh_window_samples: list[tuple[float, Mapping[str, Any]]] = []
    observed_winds: list[float] = []
    missing_wind_samples = 0
    stale_samples = 0
    for elapsed_s, sample in window_samples:
        telemetry = sample.get("telemetry")
        telemetry = telemetry if isinstance(telemetry, Mapping) else {}
        if telemetry.get("stale") is True:
            stale_samples += 1
            continue
        fresh_window_samples.append((elapsed_s, sample))
        wind = sample.get("wind")
        wind = wind if isinstance(wind, Mapping) else {}
        wind_speed_mps = _number(
            wind.get("speed_mps"),
            wind.get("observed_speed_mps"),
            sample.get("wind_speed_mps"),
        )
        if wind_speed_mps is None:
            missing_wind_samples += 1
        else:
            observed_winds.append(wind_speed_mps)
    latest_sample_stale = bool(
        window_samples
        and isinstance(window_samples[-1][1].get("telemetry"), Mapping)
        and window_samples[-1][1]["telemetry"].get("stale") is True
    )
    coverage_start_s = (
        window_start_s
        if fresh_predecessors
        and window_start_s is not None
        else fresh_window_samples[0][0]
        if fresh_window_samples
        else None
    )
    observed_span_s = (
        fresh_window_samples[-1][0] - coverage_start_s
        if fresh_window_samples and coverage_start_s is not None
        else 0.0
    )
    observed_gaps_s = [
        current[0] - previous[0]
        for previous, current in zip(
            fresh_window_samples, fresh_window_samples[1:]
        )
    ]
    maximum_observed_gap_s = max(observed_gaps_s) if observed_gaps_s else None
    wind_max_mps = max(observed_winds) if observed_winds else None
    blocking_reasons: list[str] = []
    if wind_limit_mps is None:
        blocking_reasons.append("recovery_wind_limit_missing")
    if observed_span_s < minimum_window_s:
        blocking_reasons.append("wind_safe_window_duration_insufficient")
    if (
        maximum_observed_gap_s is not None
        and maximum_observed_gap_s > maximum_sample_gap_s
    ):
        blocking_reasons.append("wind_safe_window_sample_gap_exceeded")
    if missing_wind_samples or not observed_winds:
        blocking_reasons.append("wind_safe_window_observation_missing")
    # A transient stale poll does not erase the surrounding source-backed
    # observations.  Its absence is represented by the gap between fresh
    # samples, which remains bounded above.  The tail itself must always be
    # fresh so a historical safe window cannot open an epoch on stale input.
    if latest_sample_stale:
        blocking_reasons.append("wind_safe_window_telemetry_stale")
    if (
        wind_limit_mps is not None
        and wind_max_mps is not None
        and wind_max_mps > wind_limit_mps
    ):
        blocking_reasons.append("wind_safe_window_limit_exceeded")
    verified = not blocking_reasons
    return {
        "schema_version": WIND_SAFE_WINDOW_SCHEMA_VERSION,
        "verification_status": "verified_safe" if verified else "unverified",
        "safe_window_observed": verified,
        "minimum_window_s": minimum_window_s,
        "maximum_sample_gap_s": maximum_sample_gap_s,
        "observed_window_s": round(observed_span_s, 3),
        "maximum_observed_sample_gap_s": (
            round(maximum_observed_gap_s, 3)
            if maximum_observed_gap_s is not None
            else None
        ),
        "sample_count": len(fresh_window_samples),
        "total_sample_count": len(window_samples),
        "wind_observation_count": len(observed_winds),
        "wind_speed_max_mps": (
            round(wind_max_mps, 3) if wind_max_mps is not None else None
        ),
        "max_wind_speed_mps": wind_limit_mps,
        "telemetry_stale_count": stale_samples,
        "missing_wind_sample_count": missing_wind_samples,
        "blocking_reasons": list(dict.fromkeys(blocking_reasons)),
        "dispatch_authority_created": False,
        "progress_counted": False,
        "physical_execution_invoked": False,
    }


def proposal_trigger_reasons(proposal: Mapping[str, Any]) -> set[str]:
    result = proposal.get("runtime_recovery_agent_result")
    result = result if isinstance(result, Mapping) else {}
    agent_output = result.get("agent_output")
    agent_output = agent_output if isinstance(agent_output, Mapping) else {}
    assessment = result.get("assessment")
    assessment = assessment if isinstance(assessment, Mapping) else {}
    reasons = [
        *list(agent_output.get("trigger_reasons") or []),
        *list(assessment.get("observed_risk_reasons") or []),
    ]
    return {str(reason).strip() for reason in reasons if str(reason).strip()}


__all__ = [
    "COMPOUND_HAZARD_STATE_SCHEMA_VERSION",
    "DEFAULT_TELEMETRY_CURSOR_MAX_ELAPSED_DELTA_S",
    "TELEMETRY_ARBITRATION_SCHEMA_VERSION",
    "WIND_SAFE_WINDOW_SCHEMA_VERSION",
    "arbitrate_latest_telemetry",
    "build_compound_hazard_state",
    "build_wind_safe_window_evidence",
    "proposal_trigger_reasons",
    "safe_window_tail_matches_telemetry",
    "telemetry_cursor",
]
