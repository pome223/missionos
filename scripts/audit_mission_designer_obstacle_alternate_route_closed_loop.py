#!/usr/bin/env python3
"""Audit the obstacle -> alternate-route closed loop.

This does not add a new applicator, verifier, approval chain, gate, or
delivery-completion authority. It runs or reads one horizontal-route SITL
summary and records whether the existing source-bound collision obstacle can
drive the existing operator-approved alternate mission upload / route execution
path to an observed alternate waypoint.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


SCHEMA_VERSION = "mission_designer_obstacle_alternate_route_closed_loop_audit.v1"
OBSTACLE_APPLICATION_SCHEMA = "gazebo_route_corridor_obstacle_spawn_application.v1"
OBSTACLE_APPLICATION_ID = (
    "gazebo_route_corridor_obstacle_spawn_application:"
    "mission_designer_collision_obstacle"
)
OBSTACLE_CONDITION_KIND = "gazebo_route_corridor_collision_obstacle_spawn"
ALTERNATE_CANDIDATE_REF = (
    "alternate_landing_candidate_evidence:mission_designer_route_blocking"
)
ALTERNATE_ROUTE_DISPATCH_REF = (
    "alternate_route_command_dispatch:mission_designer_route_blocking"
)
DEFAULT_PROGRESS_THRESHOLD_M = 1.0
DEFAULT_WAYPOINT_THRESHOLD_M = 3.0
OBSTACLE_PLACEMENT_MIN_M = -10.0
OBSTACLE_PLACEMENT_MAX_M = 10.0
DEFAULT_OBSTACLE_START_X_M = 2.1
DEFAULT_OBSTACLE_START_Y_M = 2.1
DEFAULT_OBSTACLE_END_X_M = 3.7
DEFAULT_OBSTACLE_END_Y_M = 3.7
OBSTACLE_PLACEMENT_ENV_BY_ARG = (
    ("obstacle_start_x_m", "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE_START_X_M"),
    ("obstacle_start_y_m", "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE_START_Y_M"),
    ("obstacle_end_x_m", "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE_END_X_M"),
    ("obstacle_end_y_m", "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE_END_Y_M"),
)

UNSAFE_AUTHORITY_KEYS = (
    "auto_gate",
    "task_status_mutated",
    "gate_status_mutated",
    "dropoff_verified",
    "delivery_completion_claimed",
    "hardware_target_allowed",
    "physical_execution_invoked",
    "approval_free_dispatch_allowed",
    "actuator_execution_performed",
)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _summary_path(run_dir: Path) -> Path:
    path = run_dir / "summary.json"
    if not path.exists():
        raise FileNotFoundError(f"summary.json not found under {run_dir}")
    return path


def _nested_true_keys(payload: Any, keys: set[str]) -> list[str]:
    observed: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in keys and value is True:
                observed.append(key)
            observed.extend(_nested_true_keys(value, keys))
    elif isinstance(payload, list):
        for value in payload:
            observed.extend(_nested_true_keys(value, keys))
    return observed


def _top_level_bool(summary: dict[str, Any], key: str) -> bool | None:
    value = summary.get(key)
    return value if isinstance(value, bool) else None


def _summary_artifact(summary: dict[str, Any], key: str) -> dict[str, Any]:
    value = summary.get(key)
    return value if isinstance(value, dict) else {}


def _xy_pair(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    try:
        x = float(value[0])
        y = float(value[1])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x) or not math.isfinite(y):
        return None
    return [x, y]


def _xy_matches(actual: Any, expected: list[float], *, tolerance: float = 1e-6) -> bool:
    pair = _xy_pair(actual)
    if pair is None:
        return False
    return all(abs(pair[index] - expected[index]) <= tolerance for index in range(2))


def _default_obstacle_placement() -> dict[str, list[float]]:
    return {
        "start_xy_m": [DEFAULT_OBSTACLE_START_X_M, DEFAULT_OBSTACLE_START_Y_M],
        "end_xy_m": [DEFAULT_OBSTACLE_END_X_M, DEFAULT_OBSTACLE_END_Y_M],
    }


def _obstacle_placement_from_summary(summary: dict[str, Any]) -> dict[str, Any]:
    application = _summary_artifact(
        summary, "gazebo_route_corridor_obstacle_spawn_application"
    )
    app_requested = application.get("requested")
    app_requested = app_requested if isinstance(app_requested, dict) else {}
    evidence = _summary_artifact(summary, "collision_obstacle_evidence")
    evidence_observed = evidence.get("observed")
    evidence_observed = (
        evidence_observed if isinstance(evidence_observed, dict) else {}
    )
    configured_samples = evidence_observed.get("configured_xy_samples_m")
    observed_start = None
    observed_end = None
    if isinstance(configured_samples, list) and len(configured_samples) >= 2:
        observed_start = _xy_pair(configured_samples[0])
        observed_end = _xy_pair(configured_samples[1])
    return {
        "application_requested_start_xy_m": _xy_pair(app_requested.get("start_xy_m")),
        "application_requested_end_xy_m": _xy_pair(app_requested.get("end_xy_m")),
        "evidence_configured_start_xy_m": observed_start,
        "evidence_configured_end_xy_m": observed_end,
        "sdf_pose_start_xy_m": _xy_pair(evidence_observed.get("sdf_pose_start_xy_m")),
        "sdf_waypoint_start_xy_m": _xy_pair(
            evidence_observed.get("sdf_waypoint_start_xy_m")
        ),
        "sdf_waypoint_end_xy_m": _xy_pair(
            evidence_observed.get("sdf_waypoint_end_xy_m")
        ),
        "sdf_placement_readback_observed": evidence_observed.get(
            "sdf_placement_readback_observed"
        ),
        "sdf_placement_matches_configured": evidence_observed.get(
            "sdf_placement_matches_configured"
        ),
    }


def _obstacle_placement_matches(
    summary: dict[str, Any],
    expected_placement: dict[str, list[float]] | None,
) -> bool:
    if expected_placement is None:
        expected_placement = _default_obstacle_placement()
    placement = _obstacle_placement_from_summary(summary)
    return (
        _xy_matches(
            placement.get("application_requested_start_xy_m"),
            expected_placement["start_xy_m"],
        )
        and _xy_matches(
            placement.get("application_requested_end_xy_m"),
            expected_placement["end_xy_m"],
        )
        and _xy_matches(
            placement.get("evidence_configured_start_xy_m"),
            expected_placement["start_xy_m"],
        )
        and _xy_matches(
            placement.get("evidence_configured_end_xy_m"),
            expected_placement["end_xy_m"],
        )
        and placement.get("sdf_placement_readback_observed") is True
        and placement.get("sdf_placement_matches_configured") is True
        and _xy_matches(
            placement.get("sdf_pose_start_xy_m"),
            expected_placement["start_xy_m"],
        )
        and _xy_matches(
            placement.get("sdf_waypoint_start_xy_m"),
            expected_placement["start_xy_m"],
        )
        and _xy_matches(
            placement.get("sdf_waypoint_end_xy_m"),
            expected_placement["end_xy_m"],
        )
    )


def _obstacle_application_source_bound(summary: dict[str, Any]) -> bool:
    application = _summary_artifact(
        summary, "gazebo_route_corridor_obstacle_spawn_application"
    )
    observed = application.get("observed")
    observed = observed if isinstance(observed, dict) else {}
    return (
        application.get("schema_version") == OBSTACLE_APPLICATION_SCHEMA
        and application.get("application_id") == OBSTACLE_APPLICATION_ID
        and application.get("condition_kind") == OBSTACLE_CONDITION_KIND
        and application.get("simulator_applicator") is True
        and application.get("application_status") == "applied"
        and application.get("requested_present") is True
        and observed.get("world_sdf_hash_match") is True
        and observed.get("model_materialized") is True
        and observed.get("collision_geometry_materialized") is True
        and observed.get("trajectory_follower_materialized") is True
    )


def _route_blocking_source_bound(summary: dict[str, Any]) -> bool:
    verification = _summary_artifact(summary, "route_blocking_verification")
    observed = verification.get("observed")
    observed = observed if isinstance(observed, dict) else {}
    return (
        verification.get("schema_version") == "route_blocking_verification.v1"
        and verification.get("verification_status") == "route_blocking_verified"
        and observed.get("route_blocking_verified") is True
        and observed.get("route_blocking_candidate") is True
        and observed.get("traffic_conflict_verified") is True
        and observed.get("operator_review_required") is True
        and observed.get("source_condition_application_ref") == OBSTACLE_APPLICATION_ID
        and observed.get("source_condition_application_verified") is True
        and observed.get("world_sdf_hash_match") is True
        and observed.get("auto_gate") is False
        and observed.get("task_status_mutated") is False
        and observed.get("gate_status_mutated") is False
        and observed.get("delivery_completion_claimed") is False
    )


def _alternate_candidate_observed(summary: dict[str, Any]) -> bool:
    candidate = _summary_artifact(summary, "alternate_landing_candidate_evidence")
    observed = candidate.get("observed")
    observed = observed if isinstance(observed, dict) else {}
    candidate_xy = observed.get("candidate_xy_m")
    return (
        candidate.get("schema_version") == "alternate_landing_candidate_evidence.v1"
        and candidate.get("observation_status") == "alternate_landing_candidate_observed"
        and candidate.get("requested_present") is True
        and observed.get("alternate_landing_candidate") is True
        and observed.get("route_blocking_verified") is True
        and observed.get("traffic_conflict_verified") is True
        and observed.get("operator_review_required") is True
        and isinstance(candidate_xy, list)
        and len(candidate_xy) == 2
        and observed.get("px4_route_changed") is False
        and observed.get("task_status_mutated") is False
        and observed.get("delivery_completion_claimed") is False
    )


def _alternate_mission_ack_observed(summary: dict[str, Any]) -> bool:
    request = _summary_artifact(summary, "alternate_mission_upload_request")
    receipt = _summary_artifact(summary, "alternate_mission_upload_receipt")
    command_ids = [
        int(item.get("command"))
        for item in receipt.get("mission_items", [])
        if isinstance(item, dict) and item.get("command") is not None
    ]
    return (
        request.get("schema_version") == "alternate_mission_upload_request.v1"
        and request.get("request_status") == "approved_for_sitl_alternate_mission_upload"
        and request.get("operator_approval_performed") is True
        and request.get("sitl_opt_in") is True
        and request.get("contains_waypoint_item") is True
        and request.get("contains_land_item") is True
        and receipt.get("schema_version") == "alternate_mission_upload_receipt.v1"
        and receipt.get("upload_status") == "uploaded"
        and receipt.get("alternate_mission_uploaded") is True
        and receipt.get("mission_ack_observed") is True
        and int(receipt.get("mission_ack_type", -1)) == 0
        and 16 in command_ids
        and 21 in command_ids
        and receipt.get("bounded_sitl_only") is True
        and receipt.get("auto_gate") is False
        and receipt.get("task_status_mutated") is False
        and receipt.get("gate_status_mutated") is False
        and receipt.get("delivery_completion_claimed") is False
        and receipt.get("hardware_target_allowed") is False
        and receipt.get("physical_execution_invoked") is False
    )


def _alternate_route_execution_observed(
    summary: dict[str, Any],
    *,
    progress_threshold_m: float,
    waypoint_threshold_m: float,
) -> bool:
    dispatch = _summary_artifact(summary, "alternate_route_command_dispatch")
    execution = _summary_artifact(summary, "alternate_route_execution_evidence")
    observed = execution.get("observed")
    observed = observed if isinstance(observed, dict) else {}
    try:
        progress_m = float(observed.get("horizontal_progress_toward_alternate_waypoint_m"))
        final_distance_m = float(observed.get("final_distance_to_alternate_waypoint_m"))
    except (TypeError, ValueError):
        return False
    return (
        dispatch.get("schema_version") == "alternate_route_command_dispatch.v1"
        and dispatch.get("dispatch_status") == "sent"
        and str(dispatch.get("approval_ref", "")).startswith(
            "px4_gazebo_coupled_command_approval:"
        )
        and str(dispatch.get("allowlist_ref", "")).startswith(
            "px4_gazebo_route_command_allowlist:"
        )
        and dispatch.get("candidate_evidence_ref") == ALTERNATE_CANDIDATE_REF
        and dispatch.get("candidate_id") == "mission_designer_alternate_landing_marker"
        and dispatch.get("alternate_mission_ack_required") is True
        and dispatch.get("alternate_mission_ack_observed") is True
        and dispatch.get("mavlink_dispatch_performed") is True
        and dispatch.get("bounded_sitl_only") is True
        and dispatch.get("approval_free_dispatch_allowed") is False
        and dispatch.get("hardware_target_allowed") is False
        and dispatch.get("physical_execution_invoked") is False
        and dispatch.get("delivery_completion_claimed") is False
        and execution.get("schema_version") == "alternate_route_execution_evidence.v1"
        and execution.get("observation_status") == "alternate_route_waypoint_reached_observed"
        and execution.get("alternate_mission_uploaded") is True
        and execution.get("alternate_route_execution_observed") is True
        and execution.get("alternate_waypoint_reached_observed") is True
        and observed.get("completion_basis") == "alternate_waypoint_reached_from_pose_progress"
        and observed.get("alternate_route_command_dispatch_ref") == ALTERNATE_ROUTE_DISPATCH_REF
        and observed.get("candidate_evidence_ref") == ALTERNATE_CANDIDATE_REF
        and observed.get("candidate_id") == "mission_designer_alternate_landing_marker"
        and observed.get("operator_approved_sitl_only") is True
        and observed.get("route_helper_sent") is True
        and progress_m >= progress_threshold_m
        and final_distance_m <= waypoint_threshold_m
        and observed.get("original_dropoff_verified") is False
        and observed.get("dropoff_verified") is False
        and observed.get("delivery_completion_claimed") is False
        and observed.get("auto_gate") is False
        and observed.get("task_status_mutated") is False
        and observed.get("gate_status_mutated") is False
        and observed.get("hardware_target_allowed") is False
        and observed.get("physical_execution_invoked") is False
    )


def _alternate_route_behavior_observed(summary: dict[str, Any]) -> bool:
    behavior = _summary_artifact(summary, "alternate_route_behavior_observation")
    return (
        behavior.get("schema_version") == "alternate_route_behavior_observation.v1"
        and behavior.get("alternate_mission_uploaded") is True
        and behavior.get("alternate_route_execution_observed") is True
        and behavior.get("alternate_waypoint_reached_observed") is True
        and behavior.get("mission_upload_ack_observed") is True
        and int(behavior.get("mission_ack_type", -1)) == 0
        and behavior.get("original_dropoff_verified") is False
        and behavior.get("dropoff_verified") is False
        and behavior.get("delivery_completion_claimed") is False
        and behavior.get("auto_gate") is False
        and behavior.get("task_status_mutated") is False
        and behavior.get("gate_status_mutated") is False
        and behavior.get("hardware_target_allowed") is False
        and behavior.get("physical_execution_invoked") is False
    )


def _summarize_closed_loop(
    run_dir: Path,
    *,
    progress_threshold_m: float,
    waypoint_threshold_m: float,
    expected_obstacle_placement: dict[str, list[float]] | None = None,
) -> dict[str, Any]:
    summary = _read_json(_summary_path(run_dir))
    route_execution = _summary_artifact(summary, "alternate_route_execution_evidence")
    route_observed = route_execution.get("observed")
    route_observed = route_observed if isinstance(route_observed, dict) else {}
    obstacle_placement = _obstacle_placement_from_summary(summary)
    effective_obstacle_placement = (
        expected_obstacle_placement or _default_obstacle_placement()
    )
    unsafe_flags = sorted(set(_nested_true_keys(summary, set(UNSAFE_AUTHORITY_KEYS))))
    route_blocking = _summary_artifact(summary, "route_blocking_verification")
    route_blocking_observed = route_blocking.get("observed")
    route_blocking_observed = (
        route_blocking_observed
        if isinstance(route_blocking_observed, dict)
        else {}
    )
    upload_receipt = _summary_artifact(summary, "alternate_mission_upload_receipt")
    route_dispatch = _summary_artifact(summary, "alternate_route_command_dispatch")
    checks = {
        "horizontal_route_smoke_observed": summary.get(
            "actual_px4_gazebo_horizontal_smoke_observed"
        )
        is True,
        "obstacle_application_source_bound": _obstacle_application_source_bound(summary),
        "obstacle_placement_source_bound": _obstacle_placement_matches(
            summary,
            expected_obstacle_placement,
        ),
        "route_blocking_source_bound": _route_blocking_source_bound(summary),
        "alternate_landing_candidate_observed": _alternate_candidate_observed(summary),
        "alternate_mission_ack_observed": _alternate_mission_ack_observed(summary),
        "alternate_route_execution_observed": _alternate_route_execution_observed(
            summary,
            progress_threshold_m=progress_threshold_m,
            waypoint_threshold_m=waypoint_threshold_m,
        ),
        "alternate_route_behavior_observed": _alternate_route_behavior_observed(summary),
        "task_remained_blocked": summary.get("task_status") == "blocked"
        and summary.get("final_status") == "blocked",
        "dropoff_not_reached": summary.get("dropoff_region_reached") is False,
        "top_level_hardware_physical_false": _top_level_bool(
            summary, "hardware_target_allowed"
        )
        is False
        and _top_level_bool(summary, "physical_execution_invoked") is False,
        "unsafe_authority_flags_absent": not unsafe_flags,
    }
    missing = [name for name, passed in checks.items() if not passed]
    observed = not missing
    return {
        "schema_version": SCHEMA_VERSION,
        "audit_id": (
            "mission_designer_obstacle_alternate_route_closed_loop_audit:"
            "mission_designer_collision_obstacle"
        ),
        "condition_kind": "source_bound_obstacle_alternate_route_closed_loop",
        "audit_status": "closed_loop_observed" if observed else "unsupported",
        "closed_loop_observed": observed,
        "form1_closed_loop_supported": observed,
        "artifact_dir": str(run_dir),
        "requested": {
            "collision_obstacle_requested": True,
            "alternate_landing_marker_requested": True,
            "operator_approved_sitl_alternate_route_required": True,
            "progress_threshold_m": progress_threshold_m,
            "waypoint_threshold_m": waypoint_threshold_m,
            "obstacle_placement_expected": True,
            "obstacle_placement_overridden": expected_obstacle_placement is not None,
            "obstacle_start_xy_m": effective_obstacle_placement["start_xy_m"],
            "obstacle_end_xy_m": effective_obstacle_placement["end_xy_m"],
        },
        "checks": checks,
        "unsupported_reasons": [
            f"{name}_not_observed" for name in missing if name != "unsafe_authority_flags_absent"
        ]
        + (["source_run_forbidden_authority_flags_observed"] if unsafe_flags else []),
        "observed": {
            "task_status": summary.get("task_status"),
            "final_status": summary.get("final_status"),
            "dropoff_region_reached": summary.get("dropoff_region_reached"),
            "route_geofence_violation": summary.get("route_geofence_violation"),
            "blocked_reasons": summary.get("blocked_reasons", []),
            "obstacle_application_requested_start_xy_m": obstacle_placement.get(
                "application_requested_start_xy_m"
            ),
            "obstacle_application_requested_end_xy_m": obstacle_placement.get(
                "application_requested_end_xy_m"
            ),
            "obstacle_evidence_configured_start_xy_m": obstacle_placement.get(
                "evidence_configured_start_xy_m"
            ),
            "obstacle_evidence_configured_end_xy_m": obstacle_placement.get(
                "evidence_configured_end_xy_m"
            ),
            "obstacle_sdf_pose_start_xy_m": obstacle_placement.get(
                "sdf_pose_start_xy_m"
            ),
            "obstacle_sdf_waypoint_start_xy_m": obstacle_placement.get(
                "sdf_waypoint_start_xy_m"
            ),
            "obstacle_sdf_waypoint_end_xy_m": obstacle_placement.get(
                "sdf_waypoint_end_xy_m"
            ),
            "obstacle_sdf_placement_readback_observed": obstacle_placement.get(
                "sdf_placement_readback_observed"
            ),
            "obstacle_sdf_placement_matches_configured": obstacle_placement.get(
                "sdf_placement_matches_configured"
            ),
            "obstacle_placement_matches_requested": _obstacle_placement_matches(
                summary,
                expected_obstacle_placement,
            ),
            "route_blocking_verified": (
                route_blocking_observed.get("route_blocking_verified")
            ),
            "traffic_conflict_verified": (
                route_blocking_observed.get("traffic_conflict_verified")
            ),
            "alternate_mission_uploaded": upload_receipt.get(
                "alternate_mission_uploaded"
            ),
            "mission_ack_observed": upload_receipt.get("mission_ack_observed"),
            "mission_ack_type": upload_receipt.get("mission_ack_type"),
            "alternate_route_dispatch_status": route_dispatch.get("dispatch_status"),
            "alternate_route_execution_observed": route_execution.get(
                "alternate_route_execution_observed"
            ),
            "alternate_waypoint_reached_observed": route_execution.get(
                "alternate_waypoint_reached_observed"
            ),
            "horizontal_progress_toward_alternate_waypoint_m": route_observed.get(
                "horizontal_progress_toward_alternate_waypoint_m"
            ),
            "final_distance_to_alternate_waypoint_m": route_observed.get(
                "final_distance_to_alternate_waypoint_m"
            ),
            "completion_basis": route_observed.get("completion_basis"),
            "original_dropoff_verified": route_observed.get(
                "original_dropoff_verified"
            ),
            "dropoff_verified": route_observed.get("dropoff_verified"),
            "delivery_completion_claimed": route_observed.get(
                "delivery_completion_claimed"
            ),
            "unsafe_authority_flags_observed": unsafe_flags,
        },
        "source_refs": {
            "obstacle_application": (
                "gazebo_route_corridor_obstacle_spawn_application:"
                "mission_designer_collision_obstacle"
            ),
            "route_blocking_verification": (
                "route_blocking_verification:mission_designer_collision_obstacle"
            ),
            "alternate_landing_candidate": (
                "alternate_landing_candidate_evidence:"
                "mission_designer_route_blocking"
            ),
            "alternate_mission_upload_receipt": (
                "alternate_mission_upload_receipt:"
                "mission_designer_route_blocking"
            ),
            "alternate_route_execution": (
                "alternate_route_execution_evidence:"
                "mission_designer_route_blocking"
            ),
        },
        "adds_verifier": False,
        "adds_candidate": False,
        "adds_approval_chain": False,
        "adds_gate": False,
        "uses_existing_operator_approved_sitl_route_execution": True,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "dropoff_verified": False,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def _run_horizontal_route_smoke(*, artifact_root: Path) -> Path:
    return _run_horizontal_route_smoke_with_env(artifact_root=artifact_root, env_overrides={})


def _run_horizontal_route_smoke_with_env(
    *,
    artifact_root: Path,
    env_overrides: dict[str, str],
    extra_args: list[str] | None = None,
) -> Path:
    env = os.environ.copy()
    env.update(
        {
            "RUN_PX4_GAZEBO_HORIZONTAL_ROUTE_SMOKE": "1",
            "PX4_GAZEBO_HORIZONTAL_ROUTE_ARTIFACT_ROOT": str(artifact_root),
            "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE": "true",
            "MISSION_DESIGNER_REALISM_ALTERNATE_LANDING_MARKER": "true",
        }
    )
    for key in (
        "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE_CONTACT_TOPIC",
        "MISSION_DESIGNER_REALISM_PAYLOAD_MASS_KG",
        "MISSION_DESIGNER_REALISM_WIND_MEAN_MPS",
        "MISSION_DESIGNER_REALISM_WIND_GUST_MPS",
        "MISSION_DESIGNER_REALISM_WIND_VARIANCE",
        "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE_START_X_M",
        "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE_START_Y_M",
        "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE_END_X_M",
        "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE_END_Y_M",
    ):
        env.pop(key, None)
    env.update(env_overrides)
    command = [sys.executable, "scripts/smoke_px4_gazebo_horizontal_route_delivery.py"]
    if extra_args:
        command.extend(extra_args)
    result = subprocess.run(
        command,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=360,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "horizontal route obstacle alternate-route smoke failed: "
            f"rc={result.returncode}\n"
            f"stdout_tail={result.stdout[-2000:]}\n"
            f"stderr_tail={result.stderr[-2000:]}"
        )
    try:
        summary = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "horizontal route smoke did not emit JSON summary: "
            f"{result.stdout[-2000:]}"
        ) from exc
    run_dir = Path(summary["artifact_dir"])
    if not run_dir.exists():
        raise FileNotFoundError(f"reported artifact_dir does not exist: {run_dir}")
    return run_dir


def _validate_obstacle_placement_value(name: str, value: float) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if value < OBSTACLE_PLACEMENT_MIN_M or value > OBSTACLE_PLACEMENT_MAX_M:
        raise ValueError(
            f"{name} must be between {OBSTACLE_PLACEMENT_MIN_M} and "
            f"{OBSTACLE_PLACEMENT_MAX_M}"
        )
    return value


def _obstacle_placement_from_args(args: argparse.Namespace) -> tuple[
    dict[str, str],
    dict[str, list[float]] | None,
]:
    values = {
        "obstacle_start_x_m": DEFAULT_OBSTACLE_START_X_M,
        "obstacle_start_y_m": DEFAULT_OBSTACLE_START_Y_M,
        "obstacle_end_x_m": DEFAULT_OBSTACLE_END_X_M,
        "obstacle_end_y_m": DEFAULT_OBSTACLE_END_Y_M,
    }
    env_overrides: dict[str, str] = {}
    provided = False
    for arg_name, env_name in OBSTACLE_PLACEMENT_ENV_BY_ARG:
        value = getattr(args, arg_name)
        if value is not None:
            provided = True
            checked = _validate_obstacle_placement_value(arg_name, float(value))
            values[arg_name] = checked
            env_overrides[env_name] = str(checked)
            continue
        raw_env = os.getenv(env_name)
        if raw_env is None:
            continue
        provided = True
        if raw_env == "":
            raise ValueError(f"{env_name} must be a finite float")
        try:
            env_value = float(raw_env)
        except ValueError as exc:
            raise ValueError(f"{env_name} must be a finite float") from exc
        checked = _validate_obstacle_placement_value(env_name, env_value)
        values[arg_name] = checked
        env_overrides[env_name] = str(checked)
    if not provided:
        return env_overrides, None
    return env_overrides, {
        "start_xy_m": [values["obstacle_start_x_m"], values["obstacle_start_y_m"]],
        "end_xy_m": [values["obstacle_end_x_m"], values["obstacle_end_y_m"]],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit obstacle -> alternate-route closed-loop behavior."
    )
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/mission_designer_behavior_delta_audits"),
    )
    parser.add_argument(
        "--progress-threshold-m",
        type=float,
        default=DEFAULT_PROGRESS_THRESHOLD_M,
    )
    parser.add_argument(
        "--waypoint-threshold-m",
        type=float,
        default=DEFAULT_WAYPOINT_THRESHOLD_M,
    )
    parser.add_argument("--obstacle-start-x-m", type=float)
    parser.add_argument("--obstacle-start-y-m", type=float)
    parser.add_argument("--obstacle-end-x-m", type=float)
    parser.add_argument("--obstacle-end-y-m", type=float)
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit_dir = args.output_dir / f"obstacle_alternate_route_closed_loop_{stamp}"
    audit_dir.mkdir(parents=True, exist_ok=False)
    try:
        env_overrides, expected_obstacle_placement = _obstacle_placement_from_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    run_dir = args.run_dir or _run_horizontal_route_smoke_with_env(
        artifact_root=audit_dir / "runs" / "obstacle_alternate_route",
        env_overrides=env_overrides,
    )
    artifact = _summarize_closed_loop(
        run_dir,
        progress_threshold_m=args.progress_threshold_m,
        waypoint_threshold_m=args.waypoint_threshold_m,
        expected_obstacle_placement=expected_obstacle_placement,
    )
    artifact["audit_dir"] = str(audit_dir)
    artifact["run_mode"] = "existing_run" if args.run_dir else "executed_run"
    artifact["obstacle_placement_env_overrides"] = env_overrides
    output_path = audit_dir / "mission_designer_obstacle_alternate_route_closed_loop.json"
    _write_json(output_path, artifact)
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
