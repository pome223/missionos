#!/usr/bin/env python3
"""Audit whether obstacle and payload applicators change drone behavior.

This does not add a new applicator, verifier, approval chain, gate, dispatch, or
delivery-completion authority. It runs or reads paired horizontal-route SITL
summaries and records whether existing obstacle / payload applicators produce
source-bound behavior deltas.
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


SCHEMA_VERSION = "mission_designer_obstacle_payload_behavior_delta_audit.v1"
OBSTACLE_SCHEMA_VERSION = "drone_behavior_delta_under_obstacle.v1"
PAYLOAD_SCHEMA_VERSION = "drone_behavior_delta_under_payload_mass.v1"
PAYLOAD_ADVISORY_SCHEMA_VERSION = "payload_feasibility_advisory.v1"
DEFAULT_DELTA_THRESHOLD_M = 0.25
DEFAULT_PAYLOAD_LIGHT_KG = 0.25
DEFAULT_PAYLOAD_HEAVY_KG = 1.25
DEFAULT_PAYLOAD_CLIMB_TIME_DELTA_THRESHOLD_SECONDS = 1.25
DEFAULT_PAYLOAD_DIRECT_TRIGGER_DECISIVE_MARGIN = 2.0
DEFAULT_PAYLOAD_ADVISORY_ORPHAN_WARNING_HOURS = 24.0
PAYLOAD_CLIMB_TARGET_Z_M = 1.0
ROUTE_GEOMETRY_TOLERANCE_M = 1e-6
PAYLOAD_VALUE_TOLERANCE_KG = 1e-6
OBSTACLE_APPLICATION_SCHEMA = "gazebo_route_corridor_obstacle_spawn_application.v1"
OBSTACLE_APPLICATION_ID = (
    "gazebo_route_corridor_obstacle_spawn_application:"
    "mission_designer_collision_obstacle"
)
OBSTACLE_CONDITION_KIND = "gazebo_route_corridor_collision_obstacle_spawn"
PAYLOAD_APPLICATION_SCHEMA = "simulator_condition_application.v1"
PAYLOAD_APPLICATION_ID = "simulator_condition_application:mission_designer_payload_mass"
PAYLOAD_EVIDENCE_SCHEMA = "observed_vehicle_condition_evidence.v1"
PAYLOAD_EVIDENCE_ID = "observed_vehicle_condition_evidence:mission_designer_payload_mass"
PAYLOAD_CONDITION_KIND = "payload_mass"
PAYLOAD_CONDITION_REF = "vehicle_condition_profile:mission_designer_payload_mass"
UNSAFE_SOURCE_AUTHORITY_KEYS = (
    "auto_gate",
    "task_status_mutated",
    "gate_status_mutated",
    "dropoff_verified",
    "delivery_completion_claimed",
    "hardware_target_allowed",
    "physical_execution_invoked",
    "actuator_execution_performed",
)


PAYLOAD_ADVISORY_REQUIRED_ACTION = (
    "review_climb_delay_against_remaining_mission_fuel_and_landing_options"
)
PAYLOAD_ADVISORY_FORBIDDEN_ACTION = "automatic_dispatch_to_recovery"
PAYLOAD_ADVISORY_REASON = "marginal_payload_climb_delay"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _pose_sample(row: dict[str, Any]) -> dict[str, float] | None:
    sample = row.get("sample")
    if not isinstance(sample, dict):
        return None
    try:
        return {
            "x": float(sample["x"]),
            "y": float(sample["y"]),
            "z": float(sample.get("z", 0.0)),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _load_pose_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        sample = _pose_sample(payload)
        if sample is None:
            continue
        rows.append(
            {
                "phase": payload.get("phase"),
                "sample_index": payload.get("sample_index"),
                "observed_at": payload.get("observed_at"),
                "sample": sample,
            }
        )
    return rows


def _observed_at_epoch_seconds(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _xy_distance(a: dict[str, float], b: dict[str, float]) -> float:
    return math.hypot(float(a["x"]) - float(b["x"]), float(a["y"]) - float(b["y"]))


def _cross_track_error_m(
    sample: dict[str, float],
    start_xy: tuple[float, float],
    target_xy: tuple[float, float],
) -> float:
    sx, sy = start_xy
    tx, ty = target_xy
    px, py = float(sample["x"]), float(sample["y"])
    dx = tx - sx
    dy = ty - sy
    denom = math.hypot(dx, dy)
    if denom <= 1e-9:
        return math.hypot(px - sx, py - sy)
    return abs(dy * px - dx * py + tx * sy - ty * sx) / denom


def _summary_path(run_dir: Path) -> Path:
    path = run_dir / "summary.json"
    if not path.exists():
        raise FileNotFoundError(f"summary.json not found under {run_dir}")
    return path


def _summary_xy(summary: dict[str, Any], key: str) -> tuple[float, float] | None:
    raw = summary.get(key)
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    try:
        return (float(raw[0]), float(raw[1]))
    except (TypeError, ValueError):
        return None


def _summary_target_xy(summary: dict[str, Any]) -> tuple[float, float] | None:
    if "route_target_x_m" not in summary or "route_target_y_m" not in summary:
        return None
    try:
        return (float(summary["route_target_x_m"]), float(summary["route_target_y_m"]))
    except (TypeError, ValueError):
        return None


def _nested_delivery_false_observed(summary: dict[str, Any]) -> bool:
    if summary.get("delivery_completion_claimed") is False:
        return True
    candidate_keys = (
        "simulator_condition_application",
        "payload_simulator_condition_application",
        "observed_vehicle_condition_evidence",
        "gazebo_route_corridor_obstacle_spawn_application",
        "route_blocking_verification",
        "traffic_conflict_verification",
        "horizontal_route_incident_informed_route_blocking_verification",
        "horizontal_route_incident_informed_traffic_conflict_verification",
    )
    return any(
        isinstance(summary.get(key), dict)
        and summary[key].get("delivery_completion_claimed") is False
        for key in candidate_keys
    )


def _nested_delivery_true_observed(summary: dict[str, Any]) -> bool:
    if summary.get("delivery_completion_claimed") is True:
        return True
    return any(
        isinstance(value, dict) and value.get("delivery_completion_claimed") is True
        for value in summary.values()
    )


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


def _summarize_run(run_dir: Path, *, label: str) -> dict[str, Any]:
    summary = _read_json(_summary_path(run_dir))
    pose_path = run_dir / "pose_samples.jsonl"
    if not pose_path.exists():
        raise FileNotFoundError(f"pose_samples.jsonl not found under {run_dir}")
    rows = _load_pose_rows(pose_path)
    route_rows = [row for row in rows if row.get("phase") == "route"]
    completed_rows = [row for row in rows if row.get("phase") == "completed"]
    pickup_xy = _summary_xy(summary, "pickup_pose_xy_m")
    target_xy = _summary_target_xy(summary)
    route_geometry_observed = pickup_xy is not None and target_xy is not None
    route_cross_track = [
        _cross_track_error_m(row["sample"], pickup_xy, target_xy)
        for row in route_rows
        if route_geometry_observed
    ]
    obstacle_application = summary.get("gazebo_route_corridor_obstacle_spawn_application") or {}
    payload_application = summary.get("payload_simulator_condition_application") or {}
    payload_evidence = summary.get("observed_vehicle_condition_evidence") or {}
    unsafe_authority_flags = sorted(
        set(_nested_true_keys(summary, set(UNSAFE_SOURCE_AUTHORITY_KEYS)))
    )
    return {
        "label": label,
        "artifact_dir": str(run_dir),
        "task_status": summary.get("task_status"),
        "final_status": summary.get("final_status"),
        "actual_px4_gazebo_horizontal_smoke_observed": summary.get(
            "actual_px4_gazebo_horizontal_smoke_observed"
        ),
        "dropoff_region_reached": summary.get("dropoff_region_reached"),
        "blocked_reasons": summary.get("blocked_reasons", []),
        "route_blocking_verified": (
            (summary.get("route_blocking_verification") or {})
            .get("observed", {})
            .get("route_blocking_verified")
            is True
        )
        or (
            (
                summary.get(
                    "horizontal_route_incident_informed_route_blocking_verification"
                )
                or {}
            )
            .get("observed", {})
            .get("route_blocking_verified")
            is True
        ),
        "traffic_conflict_verified": (
            (summary.get("traffic_conflict_verification") or {})
            .get("observed", {})
            .get("traffic_conflict_verified")
            is True
        )
        or (
            (
                summary.get(
                    "horizontal_route_incident_informed_traffic_conflict_verification"
                )
                or {}
            )
            .get("observed", {})
            .get("traffic_conflict_verified")
            is True
        ),
        "obstacle_application_status": obstacle_application.get("application_status"),
        "obstacle_application_schema_version": obstacle_application.get("schema_version"),
        "obstacle_application_id": obstacle_application.get("application_id"),
        "obstacle_condition_kind": obstacle_application.get("condition_kind"),
        "obstacle_simulator_applicator": obstacle_application.get("simulator_applicator"),
        "obstacle_requested_present": obstacle_application.get("requested_present"),
        "obstacle_world_sdf_hash_match": (obstacle_application.get("observed") or {}).get(
            "world_sdf_hash_match"
        ),
        "obstacle_model_materialized": (obstacle_application.get("observed") or {}).get(
            "model_materialized"
        ),
        "obstacle_collision_geometry_materialized": (
            obstacle_application.get("observed") or {}
        ).get("collision_geometry_materialized"),
        "obstacle_trajectory_follower_materialized": (
            obstacle_application.get("observed") or {}
        ).get("trajectory_follower_materialized"),
        "payload_application_status": payload_application.get("application_status"),
        "payload_application_schema_version": payload_application.get("schema_version"),
        "payload_application_id": payload_application.get("application_id"),
        "payload_condition_kind": payload_application.get("condition_kind"),
        "payload_requested_condition_ref": payload_application.get(
            "requested_condition_ref"
        ),
        "payload_simulator_only": payload_application.get("simulator_only"),
        "payload_evidence_schema_version": payload_evidence.get("schema_version"),
        "payload_evidence_id": payload_evidence.get("evidence_id"),
        "payload_evidence_condition_kind": payload_evidence.get("condition_kind"),
        "payload_evidence_application_ref": payload_evidence.get("application_ref"),
        "payload_evidence_requested_condition_ref": payload_evidence.get(
            "requested_condition_ref"
        ),
        "payload_requested_mass_kg": (payload_evidence.get("observed") or {}).get(
            "requested_payload_mass_kg"
        ),
        "payload_applied_mass_kg": (payload_application.get("applied") or {}).get(
            "payload_mass_kg"
        ),
        "payload_mass_materialized": (payload_evidence.get("observed") or {}).get(
            "payload_mass_materialized"
        ),
        "payload_world_sdf_hash_match": (payload_evidence.get("observed") or {}).get(
            "world_sdf_hash_match"
        ),
        "route_geometry_observed": route_geometry_observed,
        "pickup_pose_xy_m": [pickup_xy[0], pickup_xy[1]] if pickup_xy else None,
        "route_target_xy_m": [target_xy[0], target_xy[1]] if target_xy else None,
        "pose_trace_path": str(pose_path),
        "pose_sample_count": len(rows),
        "route_sample_count": len(route_rows),
        "completed_pose": completed_rows[-1]["sample"] if completed_rows else None,
        "max_route_cross_track_error_m": max(route_cross_track) if route_cross_track else None,
        "mean_route_cross_track_error_m": (
            sum(route_cross_track) / len(route_cross_track) if route_cross_track else None
        ),
        "horizontal_progress_m": summary.get("horizontal_progress_m"),
        "hardware_target_allowed": summary.get("hardware_target_allowed"),
        "physical_execution_invoked": summary.get("physical_execution_invoked"),
        "delivery_completion_claimed": _nested_delivery_true_observed(summary),
        "delivery_completion_claimed_explicit_false": _nested_delivery_false_observed(
            summary
        ),
        "unsafe_source_authority_flags_observed": unsafe_authority_flags,
    }


def _float_equal(a: Any, b: Any, *, tolerance: float) -> bool:
    try:
        return abs(float(a) - float(b)) <= tolerance
    except (TypeError, ValueError):
        return False


def _route_geometry_match(run_a: dict[str, Any], run_b: dict[str, Any]) -> bool:
    if not run_a.get("route_geometry_observed") or not run_b.get(
        "route_geometry_observed"
    ):
        return False
    return (
        _float_equal(
            run_a.get("pickup_pose_xy_m", [None, None])[0],
            run_b.get("pickup_pose_xy_m", [None, None])[0],
            tolerance=ROUTE_GEOMETRY_TOLERANCE_M,
        )
        and _float_equal(
            run_a.get("pickup_pose_xy_m", [None, None])[1],
            run_b.get("pickup_pose_xy_m", [None, None])[1],
            tolerance=ROUTE_GEOMETRY_TOLERANCE_M,
        )
        and _float_equal(
            run_a.get("route_target_xy_m", [None, None])[0],
            run_b.get("route_target_xy_m", [None, None])[0],
            tolerance=ROUTE_GEOMETRY_TOLERANCE_M,
        )
        and _float_equal(
            run_a.get("route_target_xy_m", [None, None])[1],
            run_b.get("route_target_xy_m", [None, None])[1],
            tolerance=ROUTE_GEOMETRY_TOLERANCE_M,
        )
    )


def _run_has_interpretable_outcome(run: dict[str, Any]) -> bool:
    return (
        run.get("actual_px4_gazebo_horizontal_smoke_observed") is True
        and run.get("task_status") in {"completed", "blocked"}
        and run.get("final_status") in {"completed", "blocked"}
    )


def _source_boundary_flags_safe(run: dict[str, Any]) -> bool:
    return (
        run.get("hardware_target_allowed") is False
        and run.get("physical_execution_invoked") is False
        and run.get("delivery_completion_claimed") is False
        and run.get("delivery_completion_claimed_explicit_false") is True
        and not run.get("unsafe_source_authority_flags_observed")
    )


def _paired_route_delta_m(run_a: dict[str, Any], run_b: dict[str, Any]) -> dict[str, Any]:
    rows_a = [
        row
        for row in _load_pose_rows(Path(run_a["pose_trace_path"]))
        if row.get("phase") == "route"
    ]
    rows_b = [
        row
        for row in _load_pose_rows(Path(run_b["pose_trace_path"]))
        if row.get("phase") == "route"
    ]
    pair_count = min(len(rows_a), len(rows_b))
    deltas = [
        _xy_distance(rows_a[index]["sample"], rows_b[index]["sample"])
        for index in range(pair_count)
    ]
    return {
        "pairing_method": "route_sample_order",
        "paired_route_sample_count": pair_count,
        "max_paired_route_xy_delta_m": max(deltas) if deltas else None,
        "mean_paired_route_xy_delta_m": (
            sum(deltas) / len(deltas) if deltas else None
        ),
    }


def _first_climb_sample_index_at_z(
    run: dict[str, Any],
    *,
    target_z_m: float = PAYLOAD_CLIMB_TARGET_Z_M,
) -> int | None:
    rows = [
        row
        for row in _load_pose_rows(Path(run["pose_trace_path"]))
        if row.get("phase") == "climb"
    ]
    for fallback_index, row in enumerate(rows):
        if float(row["sample"]["z"]) >= target_z_m:
            raw_index = row.get("sample_index")
            try:
                return int(raw_index)
            except (TypeError, ValueError):
                return fallback_index
    return None


def _payload_climb_metrics(
    light: dict[str, Any],
    heavy: dict[str, Any],
    *,
    target_z_m: float = PAYLOAD_CLIMB_TARGET_Z_M,
) -> dict[str, Any]:
    light_rows = [
        row
        for row in _load_pose_rows(Path(light["pose_trace_path"]))
        if row.get("phase") == "climb"
    ]
    heavy_rows = [
        row
        for row in _load_pose_rows(Path(heavy["pose_trace_path"]))
        if row.get("phase") == "climb"
    ]
    light_index = _first_climb_sample_index_at_z(light, target_z_m=target_z_m)
    heavy_index = _first_climb_sample_index_at_z(heavy, target_z_m=target_z_m)
    sample_delta = (
        float(heavy_index) - float(light_index)
        if light_index is not None and heavy_index is not None
        else None
    )
    light_timing = _climb_timing_to_target_z(light_rows, target_z_m=target_z_m)
    heavy_timing = _climb_timing_to_target_z(heavy_rows, target_z_m=target_z_m)
    elapsed_delta = (
        heavy_timing["elapsed_seconds_to_target_z"]
        - light_timing["elapsed_seconds_to_target_z"]
        if light_timing["elapsed_seconds_to_target_z"] is not None
        and heavy_timing["elapsed_seconds_to_target_z"] is not None
        else None
    )
    return {
        "climb_target_z_m": target_z_m,
        "climb_metric_basis": "pose_samples_phase_climb_observed_at_elapsed_seconds",
        "light_climb_sample_count": len(light_rows),
        "heavy_climb_sample_count": len(heavy_rows),
        "light_first_climb_sample_index_at_target_z": light_index,
        "heavy_first_climb_sample_index_at_target_z": heavy_index,
        "climb_sample_index_delta_at_target_z": sample_delta,
        "light_climb_elapsed_seconds_to_target_z": light_timing[
            "elapsed_seconds_to_target_z"
        ],
        "heavy_climb_elapsed_seconds_to_target_z": heavy_timing[
            "elapsed_seconds_to_target_z"
        ],
        "climb_elapsed_seconds_delta_at_target_z": elapsed_delta,
        "light_climb_timing_observed": light_timing["timing_observed"],
        "heavy_climb_timing_observed": heavy_timing["timing_observed"],
        "light_climb_sample_index_monotonic": light_timing[
            "sample_index_monotonic"
        ],
        "heavy_climb_sample_index_monotonic": heavy_timing[
            "sample_index_monotonic"
        ],
        "light_climb_observed_at_monotonic": light_timing["observed_at_monotonic"],
        "heavy_climb_observed_at_monotonic": heavy_timing["observed_at_monotonic"],
    }


def _climb_timing_to_target_z(
    rows: list[dict[str, Any]], *, target_z_m: float
) -> dict[str, Any]:
    observed_times = [_observed_at_epoch_seconds(row.get("observed_at")) for row in rows]
    indexes: list[int] = []
    for row in rows:
        try:
            indexes.append(int(row.get("sample_index")))
        except (TypeError, ValueError):
            indexes.append(-1)
    observed_at_monotonic = all(
        observed_times[index] is not None
        and observed_times[index + 1] is not None
        and observed_times[index + 1] >= observed_times[index]
        for index in range(len(observed_times) - 1)
    )
    sample_index_monotonic = all(
        indexes[index] >= 0
        and indexes[index + 1] >= 0
        and indexes[index + 1] > indexes[index]
        for index in range(len(indexes) - 1)
    )
    if not rows or not observed_times or observed_times[0] is None:
        return {
            "elapsed_seconds_to_target_z": None,
            "timing_observed": False,
            "observed_at_monotonic": observed_at_monotonic,
            "sample_index_monotonic": sample_index_monotonic,
        }
    start_time = observed_times[0]
    for row, observed_time in zip(rows, observed_times, strict=False):
        if observed_time is None:
            return {
                "elapsed_seconds_to_target_z": None,
                "timing_observed": False,
                "observed_at_monotonic": observed_at_monotonic,
                "sample_index_monotonic": sample_index_monotonic,
            }
        if float(row["sample"]["z"]) >= target_z_m:
            return {
                "elapsed_seconds_to_target_z": observed_time - start_time,
                "timing_observed": observed_at_monotonic
                and sample_index_monotonic
                and observed_time >= start_time,
                "observed_at_monotonic": observed_at_monotonic,
                "sample_index_monotonic": sample_index_monotonic,
            }
    return {
        "elapsed_seconds_to_target_z": None,
        "timing_observed": False,
        "observed_at_monotonic": observed_at_monotonic,
        "sample_index_monotonic": sample_index_monotonic,
    }


def _behavior_delta_metrics(
    run_a: dict[str, Any],
    run_b: dict[str, Any],
) -> dict[str, Any]:
    paired = _paired_route_delta_m(run_a, run_b)
    completed_delta = (
        _xy_distance(run_a["completed_pose"], run_b["completed_pose"])
        if run_a.get("completed_pose") and run_b.get("completed_pose")
        else None
    )
    max_cross_track_delta = (
        abs(
            float(run_a["max_route_cross_track_error_m"])
            - float(run_b["max_route_cross_track_error_m"])
        )
        if run_a.get("max_route_cross_track_error_m") is not None
        and run_b.get("max_route_cross_track_error_m") is not None
        else None
    )
    progress_delta = (
        abs(float(run_a["horizontal_progress_m"]) - float(run_b["horizontal_progress_m"]))
        if run_a.get("horizontal_progress_m") is not None
        and run_b.get("horizontal_progress_m") is not None
        else None
    )
    outcome_changed = (
        run_a.get("task_status") != run_b.get("task_status")
        or run_a.get("final_status") != run_b.get("final_status")
        or run_a.get("dropoff_region_reached") != run_b.get("dropoff_region_reached")
        or bool(run_a.get("route_blocking_verified"))
        != bool(run_b.get("route_blocking_verified"))
    )
    delta_bases = [
        name
        for name, value in (
            ("completed_pose_xy_delta", completed_delta),
            ("paired_route_xy_delta", paired.get("max_paired_route_xy_delta_m")),
            ("route_cross_track_error_delta", max_cross_track_delta),
            ("horizontal_progress_delta", progress_delta),
        )
        if value is not None
    ]
    delta_candidates = [
        value
        for value in (
            completed_delta,
            paired.get("max_paired_route_xy_delta_m"),
            max_cross_track_delta,
            progress_delta,
        )
        if value is not None
    ]
    return {
        **paired,
        "completed_pose_xy_delta_m": completed_delta,
        "max_route_cross_track_error_delta_m": max_cross_track_delta,
        "horizontal_progress_delta_m": progress_delta,
        "max_observed_delta_m": max(delta_candidates) if delta_candidates else None,
        "delta_bases": delta_bases,
        "outcome_changed": outcome_changed,
    }


def _source_binding(
    run_a: dict[str, Any],
    run_b: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    route_geometry_match = _route_geometry_match(run_a, run_b)
    source_runs_interpretable = all(
        _run_has_interpretable_outcome(run) for run in (run_a, run_b)
    )
    source_boundary_flags_safe = all(
        _source_boundary_flags_safe(run) for run in (run_a, run_b)
    )
    reasons: list[str] = []
    if not route_geometry_match:
        reasons.append("source_route_geometry_mismatch")
    if not source_runs_interpretable:
        reasons.append("source_runs_not_interpretable")
    if not source_boundary_flags_safe:
        reasons.append("source_run_forbidden_authority_flags_observed")
    return (
        {
            "route_geometry_match": route_geometry_match,
            "source_runs_interpretable": source_runs_interpretable,
            "source_boundary_flags_safe": source_boundary_flags_safe,
            "source_unsafe_authority_flags_observed": sorted(
                set(run_a.get("unsafe_source_authority_flags_observed", []))
                | set(run_b.get("unsafe_source_authority_flags_observed", []))
            ),
            "route_geometry_tolerance_m": ROUTE_GEOMETRY_TOLERANCE_M,
        },
        reasons,
    )


def _obstacle_application_observed(run: dict[str, Any]) -> bool:
    return (
        run.get("obstacle_application_schema_version") == OBSTACLE_APPLICATION_SCHEMA
        and run.get("obstacle_application_id") == OBSTACLE_APPLICATION_ID
        and run.get("obstacle_condition_kind") == OBSTACLE_CONDITION_KIND
        and run.get("obstacle_simulator_applicator") is True
        and run.get("obstacle_application_status") == "applied"
        and run.get("obstacle_requested_present") is True
        and run.get("obstacle_world_sdf_hash_match") is True
        and run.get("obstacle_model_materialized") is True
        and run.get("obstacle_collision_geometry_materialized") is True
        and run.get("obstacle_trajectory_follower_materialized") is True
    )


def _obstacle_not_requested(run: dict[str, Any]) -> bool:
    return run.get("obstacle_application_status") in {None, "not_requested"} and (
        run.get("obstacle_requested_present") in {None, False}
    )


def build_obstacle_behavior_delta(
    baseline_dir: Path,
    obstacle_dir: Path,
    *,
    delta_threshold_m: float = DEFAULT_DELTA_THRESHOLD_M,
) -> dict[str, Any]:
    baseline = _summarize_run(baseline_dir, label="obstacle_baseline")
    obstacle = _summarize_run(obstacle_dir, label="obstacle_applied")
    metrics = _behavior_delta_metrics(baseline, obstacle)
    source_binding, unsupported_reasons = _source_binding(baseline, obstacle)
    if not _obstacle_not_requested(baseline):
        unsupported_reasons.append("baseline_obstacle_not_absent")
    if not _obstacle_application_observed(obstacle):
        unsupported_reasons.append("obstacle_application_not_source_bound_observed")
    raw_delta_observed = bool(
        metrics["outcome_changed"]
        or (
            metrics["max_observed_delta_m"] is not None
            and metrics["max_observed_delta_m"] >= delta_threshold_m
        )
        or obstacle.get("route_blocking_verified") is True
    )
    behavior_effect_basis = []
    if metrics["outcome_changed"]:
        behavior_effect_basis.append("mission_outcome_changed")
    if (
        metrics["max_observed_delta_m"] is not None
        and metrics["max_observed_delta_m"] >= delta_threshold_m
    ):
        behavior_effect_basis.append("pose_trace_delta_above_threshold")
    if obstacle.get("route_blocking_verified") is True:
        behavior_effect_basis.append("route_blocking_verified")
    supported = raw_delta_observed and not unsupported_reasons
    status = (
        "obstacle_behavior_delta_observed"
        if supported
        else "obstacle_behavior_delta_below_threshold"
        if not unsupported_reasons
        else "unsupported"
    )
    return {
        "schema_version": OBSTACLE_SCHEMA_VERSION,
        "audit_id": "drone_behavior_delta_under_obstacle:mission_designer_collision_obstacle",
        "condition_kind": "collision_obstacle_drone_behavior_delta",
        "audit_status": status,
        "requested": {
            "baseline_obstacle_requested": False,
            "condition_obstacle_requested": True,
            "delta_threshold_m": delta_threshold_m,
        },
        "runs": [baseline, obstacle],
        "source_binding": source_binding,
        "metrics": {**metrics, "delta_threshold_m": delta_threshold_m},
        "behavior_effect_basis": behavior_effect_basis,
        "drone_behavior_affected": supported,
        "obstacle_behavior_delta_observed": supported,
        "raw_behavior_delta_above_threshold": raw_delta_observed,
        "form1_claim_supported": supported,
        "form4_reclassification_candidate": (
            not supported and not unsupported_reasons
        ),
        "unsupported_reasons": unsupported_reasons,
        "verifier": False,
        "candidate": False,
        "approval_chain": False,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def _payload_application_observed(run: dict[str, Any], expected_kg: float) -> bool:
    return (
        run.get("payload_application_schema_version") == PAYLOAD_APPLICATION_SCHEMA
        and run.get("payload_application_id") == PAYLOAD_APPLICATION_ID
        and run.get("payload_condition_kind") == PAYLOAD_CONDITION_KIND
        and run.get("payload_requested_condition_ref") == PAYLOAD_CONDITION_REF
        and run.get("payload_simulator_only") is True
        and run.get("payload_evidence_schema_version") == PAYLOAD_EVIDENCE_SCHEMA
        and run.get("payload_evidence_id") == PAYLOAD_EVIDENCE_ID
        and run.get("payload_evidence_condition_kind") == PAYLOAD_CONDITION_KIND
        and run.get("payload_evidence_application_ref") == PAYLOAD_APPLICATION_ID
        and run.get("payload_evidence_requested_condition_ref") == PAYLOAD_CONDITION_REF
        and run.get("payload_application_status") == "applied"
        and run.get("payload_mass_materialized") is True
        and run.get("payload_world_sdf_hash_match") is True
        and _float_equal(
            run.get("payload_requested_mass_kg"),
            expected_kg,
            tolerance=PAYLOAD_VALUE_TOLERANCE_KG,
        )
        and _float_equal(
            run.get("payload_applied_mass_kg"),
            expected_kg,
            tolerance=PAYLOAD_VALUE_TOLERANCE_KG,
        )
    )


def build_payload_behavior_delta(
    light_dir: Path,
    heavy_dir: Path,
    *,
    light_payload_kg: float = DEFAULT_PAYLOAD_LIGHT_KG,
    heavy_payload_kg: float = DEFAULT_PAYLOAD_HEAVY_KG,
    delta_threshold_m: float = DEFAULT_DELTA_THRESHOLD_M,
    climb_time_delta_threshold_seconds: float = (
        DEFAULT_PAYLOAD_CLIMB_TIME_DELTA_THRESHOLD_SECONDS
    ),
) -> dict[str, Any]:
    light = _summarize_run(light_dir, label="payload_light")
    heavy = _summarize_run(heavy_dir, label="payload_heavy")
    metrics = _behavior_delta_metrics(light, heavy)
    climb_metrics = _payload_climb_metrics(light, heavy)
    source_binding, unsupported_reasons = _source_binding(light, heavy)
    if not _payload_application_observed(light, light_payload_kg):
        unsupported_reasons.append("light_payload_application_not_source_bound_observed")
    if not _payload_application_observed(heavy, heavy_payload_kg):
        unsupported_reasons.append("heavy_payload_application_not_source_bound_observed")
    raw_delta_observed = bool(
        metrics["outcome_changed"]
        or (
            metrics["completed_pose_xy_delta_m"] is not None
            and metrics["completed_pose_xy_delta_m"] >= delta_threshold_m
        )
        or (
            metrics["horizontal_progress_delta_m"] is not None
            and metrics["horizontal_progress_delta_m"] >= delta_threshold_m
        )
        or (
            metrics["max_route_cross_track_error_delta_m"] is not None
            and metrics["max_route_cross_track_error_delta_m"] >= delta_threshold_m
        )
        or (
            climb_metrics["light_climb_timing_observed"] is True
            and climb_metrics["heavy_climb_timing_observed"] is True
            and climb_metrics["climb_elapsed_seconds_delta_at_target_z"] is not None
            and climb_metrics["climb_elapsed_seconds_delta_at_target_z"]
            >= climb_time_delta_threshold_seconds
        )
    )
    behavior_effect_basis = []
    if metrics["outcome_changed"]:
        behavior_effect_basis.append("mission_outcome_changed")
    if (
        metrics["completed_pose_xy_delta_m"] is not None
        and metrics["completed_pose_xy_delta_m"] >= delta_threshold_m
    ):
        behavior_effect_basis.append("completed_pose_delta_above_threshold")
    if (
        metrics["horizontal_progress_delta_m"] is not None
        and metrics["horizontal_progress_delta_m"] >= delta_threshold_m
    ):
        behavior_effect_basis.append("horizontal_progress_delta_above_threshold")
    if (
        metrics["max_route_cross_track_error_delta_m"] is not None
        and metrics["max_route_cross_track_error_delta_m"] >= delta_threshold_m
    ):
        behavior_effect_basis.append("route_cross_track_delta_above_threshold")
    if (
        climb_metrics["light_climb_timing_observed"] is True
        and climb_metrics["heavy_climb_timing_observed"] is True
        and climb_metrics["climb_elapsed_seconds_delta_at_target_z"] is not None
        and climb_metrics["climb_elapsed_seconds_delta_at_target_z"]
        >= climb_time_delta_threshold_seconds
    ):
        behavior_effect_basis.append("climb_elapsed_time_delta_at_target_z_above_threshold")
    supported = raw_delta_observed and not unsupported_reasons
    status = (
        "payload_behavior_delta_observed"
        if supported
        else "payload_behavior_delta_below_threshold"
        if not unsupported_reasons
        else "unsupported"
    )
    return {
        "schema_version": PAYLOAD_SCHEMA_VERSION,
        "audit_id": "drone_behavior_delta_under_payload_mass:mission_designer_payload_mass",
        "condition_kind": "payload_mass_drone_behavior_delta",
        "audit_status": status,
        "requested": {
            "light_payload_kg": light_payload_kg,
            "heavy_payload_kg": heavy_payload_kg,
            "delta_threshold_m": delta_threshold_m,
            "climb_time_delta_threshold_seconds": climb_time_delta_threshold_seconds,
        },
        "runs": [light, heavy],
        "source_binding": {
            **source_binding,
            "payload_value_tolerance_kg": PAYLOAD_VALUE_TOLERANCE_KG,
        },
        "metrics": {
            **metrics,
            **climb_metrics,
            "delta_threshold_m": delta_threshold_m,
            "climb_time_delta_threshold_seconds": climb_time_delta_threshold_seconds,
        },
        "behavior_effect_basis": behavior_effect_basis,
        "drone_behavior_affected": supported,
        "payload_behavior_delta_observed": supported,
        "raw_behavior_delta_above_threshold": raw_delta_observed,
        "form1_claim_supported": supported,
        "form4_reclassification_candidate": (
            not supported and not unsupported_reasons
        ),
        "unsupported_reasons": unsupported_reasons,
        "verifier": False,
        "candidate": False,
        "approval_chain": False,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def _payload_behavior_delta_margin(payload_delta: dict[str, Any]) -> float | None:
    metrics = payload_delta.get("metrics")
    if not isinstance(metrics, dict):
        return None
    delta = metrics.get("climb_elapsed_seconds_delta_at_target_z")
    threshold = metrics.get("climb_time_delta_threshold_seconds")
    try:
        delta_value = float(delta)
        threshold_value = float(threshold)
    except (TypeError, ValueError):
        return None
    if threshold_value <= 0:
        return None
    return delta_value / threshold_value


def build_payload_feasibility_advisory(
    payload_delta: dict[str, Any],
    *,
    decisive_margin: float = DEFAULT_PAYLOAD_DIRECT_TRIGGER_DECISIVE_MARGIN,
    orphan_warning_hours: float = DEFAULT_PAYLOAD_ADVISORY_ORPHAN_WARNING_HOURS,
) -> dict[str, Any]:
    margin = _payload_behavior_delta_margin(payload_delta)
    source_observed_at = payload_delta.get("observed_at")
    advisory_source_refs = {
        "climb_delay_audit_ref": payload_delta.get("audit_id"),
        "climb_delay_schema_version": payload_delta.get("schema_version"),
        "behavior_delta_observed_at": source_observed_at,
        "audit_status": payload_delta.get("audit_status"),
        "source_binding": payload_delta.get("source_binding"),
        "behavior_effect_basis": payload_delta.get("behavior_effect_basis", []),
        "source_run_artifact_dirs": [
            run.get("artifact_dir")
            for run in payload_delta.get("runs", [])
            if isinstance(run, dict) and run.get("artifact_dir")
        ],
    }
    eligible_for_advisory = (
        payload_delta.get("form1_claim_supported") is True
        and payload_delta.get("drone_behavior_affected") is True
        and margin is not None
        and margin >= 1.0
        and margin < decisive_margin
        and not payload_delta.get("unsupported_reasons")
    )
    advisory_status = (
        "operator_review_required" if eligible_for_advisory else "not_requested"
    )
    reason = (
        PAYLOAD_ADVISORY_REASON
        if eligible_for_advisory
        else "payload_climb_delay_not_marginal_form1"
    )
    return {
        "schema_version": PAYLOAD_ADVISORY_SCHEMA_VERSION,
        "advisory_id": "payload_feasibility_advisory:mission_designer_payload_mass",
        "advisory_ref": "payload_feasibility_advisory:mission_designer_payload_mass",
        "condition_kind": "payload_mass_feasibility",
        "causal_form": "Form 2b",
        "form2_subtype": "Form 2b",
        "trigger_level": "level_2_inferred",
        "progress_counted": eligible_for_advisory,
        "advisory_status": advisory_status,
        "mission_response_kind": "advisory",
        "operator_review_required": eligible_for_advisory,
        "automatic_dispatch_suppressed": True,
        "eligible_for_direct_trigger": False,
        "eligible_for_advisory_only": eligible_for_advisory,
        "required_action": (
            PAYLOAD_ADVISORY_REQUIRED_ACTION if eligible_for_advisory else None
        ),
        "forbidden_action": PAYLOAD_ADVISORY_FORBIDDEN_ACTION,
        "mission_response_advisory_reason": reason,
        "behavior_delta_margin": margin,
        "marginal_threshold": 1.0,
        "decisive_threshold": decisive_margin,
        "reason": reason,
        "advisory_source_refs": advisory_source_refs,
        "advisory_lifecycle_state": "pending_operator_review"
        if eligible_for_advisory
        else "not_requested",
        "advisory_consumed_by_ref": None,
        "orphan_advisory_warning_hours": orphan_warning_hours,
        "orphan_advisory_warning": False,
        "same_audit_artifact_observed": bool(payload_delta.get("audit_id")),
        "same_session_evidence": {
            "audit_artifact_id": payload_delta.get("audit_id"),
            "audit_observed_at": source_observed_at,
            "source_run_count": len(advisory_source_refs["source_run_artifact_dirs"]),
            "source_run_artifact_dirs": advisory_source_refs[
                "source_run_artifact_dirs"
            ],
        },
        "verifier": False,
        "candidate": False,
        "approval_chain": False,
        "dispatch": False,
        "recovery": False,
        "rth_commanded": False,
        "land_commanded": False,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def payload_feasibility_advisory_requested(advisory: dict[str, Any] | None) -> bool:
    return bool(
        advisory
        and advisory.get("advisory_status") == "operator_review_required"
        and advisory.get("mission_response_kind") == "advisory"
        and advisory.get("operator_review_required") is True
        and advisory.get("automatic_dispatch_suppressed") is True
        and advisory.get("eligible_for_advisory_only") is True
    )


def payload_feasibility_advisory_orphan_warning(
    advisory: dict[str, Any],
    *,
    now: datetime,
    max_age_hours: float = DEFAULT_PAYLOAD_ADVISORY_ORPHAN_WARNING_HOURS,
) -> bool:
    if advisory.get("advisory_lifecycle_state") != "pending_operator_review":
        return False
    if advisory.get("advisory_consumed_by_ref"):
        return False
    observed_at = advisory.get("observed_at")
    if not isinstance(observed_at, str) or not observed_at:
        return True
    try:
        created_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    now_value = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    age_seconds = (now_value - created_at).total_seconds()
    return age_seconds >= max_age_hours * 3600.0


def _run_horizontal_route_smoke(
    *,
    artifact_root: Path,
    env_overrides: dict[str, str],
) -> Path:
    env = os.environ.copy()
    env.update(
        {
            "RUN_PX4_GAZEBO_HORIZONTAL_ROUTE_SMOKE": "1",
            "PX4_GAZEBO_HORIZONTAL_ROUTE_ARTIFACT_ROOT": str(artifact_root),
        }
    )
    for key in (
        "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE",
        "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE_CONTACT_TOPIC",
        "MISSION_DESIGNER_REALISM_PAYLOAD_MASS_KG",
        "MISSION_DESIGNER_REALISM_WIND_MEAN_MPS",
        "MISSION_DESIGNER_REALISM_WIND_GUST_MPS",
        "MISSION_DESIGNER_REALISM_WIND_VARIANCE",
    ):
        env.pop(key, None)
    env.update(env_overrides)
    result = subprocess.run(
        [sys.executable, "scripts/smoke_px4_gazebo_horizontal_route_delivery.py"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "horizontal route smoke failed "
            f"for env={env_overrides}: rc={result.returncode}\n"
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit obstacle and payload behavior deltas."
    )
    parser.add_argument("--delta-threshold-m", type=float, default=DEFAULT_DELTA_THRESHOLD_M)
    parser.add_argument(
        "--payload-climb-time-delta-threshold-seconds",
        type=float,
        default=DEFAULT_PAYLOAD_CLIMB_TIME_DELTA_THRESHOLD_SECONDS,
    )
    parser.add_argument("--payload-light-kg", type=float, default=DEFAULT_PAYLOAD_LIGHT_KG)
    parser.add_argument("--payload-heavy-kg", type=float, default=DEFAULT_PAYLOAD_HEAVY_KG)
    parser.add_argument("--obstacle-baseline-dir", type=Path)
    parser.add_argument("--obstacle-run-dir", type=Path)
    parser.add_argument("--payload-light-dir", type=Path)
    parser.add_argument("--payload-heavy-dir", type=Path)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/mission_designer_behavior_delta_audits"),
    )
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit_dir = args.output_dir / f"obstacle_payload_behavior_delta_{stamp}"
    audit_dir.mkdir(parents=True, exist_ok=False)
    run_root = audit_dir / "runs"

    obstacle_baseline_dir = args.obstacle_baseline_dir or _run_horizontal_route_smoke(
        artifact_root=run_root / "obstacle_baseline",
        env_overrides={},
    )
    obstacle_run_dir = args.obstacle_run_dir or _run_horizontal_route_smoke(
        artifact_root=run_root / "obstacle_applied",
        env_overrides={"MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE": "true"},
    )
    payload_light_dir = args.payload_light_dir or _run_horizontal_route_smoke(
        artifact_root=run_root / "payload_light",
        env_overrides={
            "MISSION_DESIGNER_REALISM_PAYLOAD_MASS_KG": str(args.payload_light_kg)
        },
    )
    payload_heavy_dir = args.payload_heavy_dir or _run_horizontal_route_smoke(
        artifact_root=run_root / "payload_heavy",
        env_overrides={
            "MISSION_DESIGNER_REALISM_PAYLOAD_MASS_KG": str(args.payload_heavy_kg)
        },
    )

    obstacle_audit = build_obstacle_behavior_delta(
        obstacle_baseline_dir,
        obstacle_run_dir,
        delta_threshold_m=args.delta_threshold_m,
    )
    payload_audit = build_payload_behavior_delta(
        payload_light_dir,
        payload_heavy_dir,
        light_payload_kg=args.payload_light_kg,
        heavy_payload_kg=args.payload_heavy_kg,
        delta_threshold_m=args.delta_threshold_m,
        climb_time_delta_threshold_seconds=(
            args.payload_climb_time_delta_threshold_seconds
        ),
    )
    payload_advisory = build_payload_feasibility_advisory(payload_audit)
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "audit_id": "mission_designer_behavior_delta_audit:obstacle_payload",
        "audit_dir": str(audit_dir),
        "run_mode": "existing_runs"
        if all(
            (
                args.obstacle_baseline_dir,
                args.obstacle_run_dir,
                args.payload_light_dir,
                args.payload_heavy_dir,
            )
        )
        else "executed_runs"
        if not any(
            (
                args.obstacle_baseline_dir,
                args.obstacle_run_dir,
                args.payload_light_dir,
                args.payload_heavy_dir,
            )
        )
        else "mixed_existing_and_executed_runs",
        "obstacle_behavior_delta": obstacle_audit,
        "payload_behavior_delta": payload_audit,
        "verifier": False,
        "candidate": False,
        "approval_chain": False,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    if payload_feasibility_advisory_requested(payload_advisory):
        artifact["payload_feasibility_advisory"] = payload_advisory
    output_path = audit_dir / "mission_designer_obstacle_payload_behavior_delta.json"
    _write_json(output_path, artifact)
    if payload_feasibility_advisory_requested(payload_advisory):
        _write_json(audit_dir / "payload_feasibility_advisory.json", payload_advisory)
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
