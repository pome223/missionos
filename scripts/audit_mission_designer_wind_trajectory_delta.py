#!/usr/bin/env python3
"""Audit whether applied Mission Designer wind changes drone trajectory.

This intentionally does not add a new applicator, verifier, approval chain, or
gate. It runs or reads two horizontal-route SITL summaries, compares their
Gazebo-local pose traces, and records whether the existing wind applicator has
an observed trajectory delta.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import yaml

from src.runtime.missionos_sitl_dispatch_runtime import (
    MISSIONOS_FORM2A_SELECTED_RESPONSE_KIND_ENV,
    WIND_COMPENSATED_ROUTE_ENV,
    WIND_COMPENSATION_METHOD_ENV,
    WIND_COMPENSATION_SOURCE_RESPONSE_ENV,
    WIND_FEED_FORWARD_MPS_ENV,
    WIND_FEED_FORWARD_RAMP_END_FRACTION_ENV,
    WIND_FEED_FORWARD_RAMP_START_FRACTION_ENV,
    WIND_PREEMPTIVE_OFFSET_DIRECTION_DEG_ENV,
    WIND_PREEMPTIVE_OFFSET_M_ENV,
)


DEFAULT_WIND_A_MPS = 2.0
DEFAULT_WIND_B_MPS = 4.0
DEFAULT_WIND_DIRECTION_DEG = 90.0
DEFAULT_DELTA_THRESHOLD_M = 0.25
DEFAULT_THRESHOLD_CONFIG_PATH = Path("config/form1_runtime_audit_thresholds.yaml")
SCHEMA_VERSION = "drone_behavior_delta_under_wind.v1"
RUNTIME_INVOCATION_EVIDENCE_SCHEMA_VERSION = "runtime_invocation_evidence.v1"
ROUTE_GEOMETRY_TOLERANCE_M = 1e-6
PICKUP_POSE_JITTER_TOLERANCE_M = 0.05
WIND_VALUE_TOLERANCE_MPS = 1e-6
SMOKE_TIMEOUT_SECONDS = 240
FORM2A_WIND_COMPENSATION_ENV_KEYS = (
    MISSIONOS_FORM2A_SELECTED_RESPONSE_KIND_ENV,
    WIND_COMPENSATED_ROUTE_ENV,
    WIND_COMPENSATION_METHOD_ENV,
    WIND_FEED_FORWARD_MPS_ENV,
    WIND_FEED_FORWARD_RAMP_START_FRACTION_ENV,
    WIND_FEED_FORWARD_RAMP_END_FRACTION_ENV,
    WIND_PREEMPTIVE_OFFSET_M_ENV,
    WIND_PREEMPTIVE_OFFSET_DIRECTION_DEG_ENV,
    WIND_COMPENSATION_SOURCE_RESPONSE_ENV,
)
ASYMMETRIC_COMPENSATION_PAIRING_MODE = "asymmetric_compensation"
WIND_ONLY_PAIRING_MODE = "wind_only"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_json(payload: Any) -> str:
    return _sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _file_sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _utc_now_iso8601() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )


def load_form1_wind_threshold_config(path: Path) -> dict[str, Any]:
    """Load the pre-committed threshold for the wind Form 1 runtime audit."""

    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    threshold = (
        payload.get("thresholds", {}).get("px4_gazebo_wind_trajectory_delta", {})
        if isinstance(payload, dict)
        else {}
    )
    if not isinstance(threshold, dict):
        raise ValueError("px4_gazebo_wind_trajectory_delta_threshold_missing")
    return {
        "threshold_config_path": str(path),
        "threshold_config_sha256": _file_sha256(path),
        "schema_version": payload.get("schema_version"),
        "baseline_wind_mps": float(
            threshold.get("baseline_wind_mps", DEFAULT_WIND_A_MPS)
        ),
        "condition_wind_mps": float(
            threshold.get("condition_wind_mps", DEFAULT_WIND_B_MPS)
        ),
        "wind_direction_deg": float(
            threshold.get("wind_direction_deg", DEFAULT_WIND_DIRECTION_DEG)
        ),
        "minimum_observed_delta_m": float(
            threshold.get("minimum_observed_delta_m", DEFAULT_DELTA_THRESHOLD_M)
        ),
        "form1a_margin_ratio": float(threshold.get("form1a_margin_ratio", 2.0)),
        "observed_delta_metric": threshold.get(
            "observed_delta_metric",
            "max_observed_delta_m",
        ),
        "rationale": threshold.get("rationale", ""),
    }


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
    wind_application = summary.get("simulator_condition_application") or {}
    wind_observed = (summary.get("observed_environment_evidence") or {}).get("observed") or {}
    summary_delivery_completion_claimed = summary.get("delivery_completion_claimed")
    application_delivery_completion_claimed = wind_application.get(
        "delivery_completion_claimed"
    )
    delivery_completion_claimed_explicit_false = (
        summary_delivery_completion_claimed is False
        or application_delivery_completion_claimed is False
    )
    return {
        "label": label,
        "artifact_dir": str(run_dir),
        "task_status": summary.get("task_status"),
        "final_status": summary.get("final_status"),
        "dropoff_region_reached": summary.get("dropoff_region_reached"),
        "blocked_reasons": summary.get("blocked_reasons", []),
        "wind_application_status": wind_application.get("application_status"),
        "wind_applied_mps": (wind_application.get("applied") or {}).get("applied_mps"),
        "wind_requested_mps": (wind_application.get("applied") or {}).get("requested_mps"),
        "wind_topic_readback_observed": wind_observed.get("wind_topic_readback_observed"),
        "wind_effects_world_sdf_readback_observed": wind_observed.get(
            "wind_effects_world_sdf_readback_observed"
        ),
        "wind_effects_plugin_materialized": wind_observed.get(
            "wind_effects_plugin_materialized"
        ),
        "wind_enabled_on_vehicle_links": wind_observed.get(
            "wind_enabled_on_vehicle_links"
        ),
        "wind_world_linear_velocity_matches_requested": wind_observed.get(
            "wind_world_linear_velocity_matches_requested"
        ),
        "gazebo_runtime_world_model_readback_observed": wind_observed.get(
            "gazebo_runtime_world_model_readback_observed"
        ),
        "gazebo_runtime_world_path_observed": wind_observed.get(
            "gazebo_runtime_world_path_observed"
        ),
        "gazebo_runtime_world_ready_observed": wind_observed.get(
            "gazebo_runtime_world_ready_observed"
        ),
        "gazebo_runtime_model_bridge_observed": wind_observed.get(
            "gazebo_runtime_model_bridge_observed"
        ),
        "gazebo_runtime_vehicle_pose_observed": wind_observed.get(
            "gazebo_runtime_vehicle_pose_observed"
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
        "hardware_target_allowed": summary.get("hardware_target_allowed"),
        "physical_execution_invoked": summary.get("physical_execution_invoked"),
        "delivery_completion_claimed": bool(
            summary_delivery_completion_claimed or application_delivery_completion_claimed
        ),
        "delivery_completion_claimed_explicit_false": (
            delivery_completion_claimed_explicit_false
        ),
    }


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
            tolerance=PICKUP_POSE_JITTER_TOLERANCE_M,
        )
        and _float_equal(
            run_a.get("pickup_pose_xy_m", [None, None])[1],
            run_b.get("pickup_pose_xy_m", [None, None])[1],
            tolerance=PICKUP_POSE_JITTER_TOLERANCE_M,
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


def _run_completed_cleanly(run: dict[str, Any]) -> bool:
    return (
        run.get("task_status") == "completed"
        and run.get("final_status") == "completed"
        and run.get("dropoff_region_reached") is True
        and not run.get("blocked_reasons")
    )


def _source_boundary_flags_safe(run: dict[str, Any]) -> bool:
    return (
        run.get("hardware_target_allowed") is False
        and run.get("physical_execution_invoked") is False
        and run.get("delivery_completion_claimed") is False
        and run.get("delivery_completion_claimed_explicit_false") is True
    )


def _wind_matches_expected(run: dict[str, Any], expected: float | None) -> bool:
    if expected is None:
        return True
    return _float_equal(
        run.get("wind_requested_mps"),
        expected,
        tolerance=WIND_VALUE_TOLERANCE_MPS,
    ) and _float_equal(
        run.get("wind_applied_mps"),
        expected,
        tolerance=WIND_VALUE_TOLERANCE_MPS,
    )


def _form1_classification(
    *,
    supported_delta_observed: bool,
    max_observed_delta_m: float | None,
    delta_threshold_m: float,
    form1a_margin_ratio: float,
) -> dict[str, Any]:
    margin_ratio = (
        float(max_observed_delta_m) / float(delta_threshold_m)
        if max_observed_delta_m is not None and delta_threshold_m > 0
        else None
    )
    if not supported_delta_observed:
        return {
            "causal_form": "Form 0b",
            "progress_counted": False,
            "form1_scope": "none",
            "observed_delta_margin_ratio": margin_ratio,
        }
    return {
        "causal_form": "Form 1a"
        if margin_ratio is not None and margin_ratio >= form1a_margin_ratio
        else "Form 1b",
        "progress_counted": True,
        "form1_scope": "drone_physics_or_mission_behavior",
        "observed_delta_margin_ratio": margin_ratio,
    }


def _runtime_invocation_evidence_complete(
    runtime_invocations: list[dict[str, Any]] | None,
) -> bool:
    if runtime_invocations is None or len(runtime_invocations) != 2:
        return False
    for evidence in runtime_invocations:
        if evidence.get("schema_version") != RUNTIME_INVOCATION_EVIDENCE_SCHEMA_VERSION:
            return False
        if evidence.get("invocation_kind") != "subprocess":
            return False
        if evidence.get("invocation_exit_code") != 0:
            return False
        if not isinstance(evidence.get("process_pid"), int):
            return False
        if not isinstance(evidence.get("artifact_dir"), str):
            return False
        if not isinstance(evidence.get("command_argv_sha256"), str):
            return False
        if not isinstance(evidence.get("condition_env_sha256"), str):
            return False
        if not isinstance(evidence.get("invocation_stdout_sha256"), str):
            return False
        if not isinstance(evidence.get("invocation_stderr_sha256"), str):
            return False
    return True


def _runtime_pairing_complete(runtime_pairing: dict[str, Any] | None) -> bool:
    if not runtime_pairing:
        return False
    return (
        runtime_pairing.get("command_argv_equal") is True
        and runtime_pairing.get("command_argv_sha256_equal") is True
        and (
            runtime_pairing.get("condition_only_env_delta") is True
            or runtime_pairing.get("asymmetric_compensation_pairing_observed") is True
        )
    )


def build_drone_behavior_delta_under_wind(
    run_a_dir: Path,
    run_b_dir: Path,
    *,
    delta_threshold_m: float = DEFAULT_DELTA_THRESHOLD_M,
    expected_wind_a_mps: float | None = None,
    expected_wind_b_mps: float | None = None,
    threshold_config: dict[str, Any] | None = None,
    form1a_margin_ratio: float = 2.0,
    runtime_invocations: list[dict[str, Any]] | None = None,
    runtime_pairing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_a = _summarize_run(run_a_dir, label="wind_a")
    run_b = _summarize_run(run_b_dir, label="wind_b")
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
    delta_candidates = [
        value
        for value in (
            completed_delta,
            paired.get("max_paired_route_xy_delta_m"),
            max_cross_track_delta,
        )
        if value is not None
    ]
    max_observed_delta = max(delta_candidates) if delta_candidates else None
    trajectory_delta_observed = (
        max_observed_delta is not None and max_observed_delta >= delta_threshold_m
    )
    wind_applications_observed = all(
        run.get("wind_application_status") in {"applied", "applied_with_approximations"}
        and run.get("wind_topic_readback_observed") is True
        and run.get("wind_effects_world_sdf_readback_observed") is True
        and run.get("wind_effects_plugin_materialized") is True
        and run.get("wind_enabled_on_vehicle_links") is True
        and run.get("wind_world_linear_velocity_matches_requested") is True
        and run.get("gazebo_runtime_world_model_readback_observed") is True
        and run.get("gazebo_runtime_world_path_observed") is True
        and run.get("gazebo_runtime_world_ready_observed") is True
        and run.get("gazebo_runtime_model_bridge_observed") is True
        and run.get("gazebo_runtime_vehicle_pose_observed") is True
        for run in (run_a, run_b)
    )
    route_geometry_match = _route_geometry_match(run_a, run_b)
    source_runs_completed = all(_run_completed_cleanly(run) for run in (run_a, run_b))
    source_boundary_flags_safe = all(
        _source_boundary_flags_safe(run) for run in (run_a, run_b)
    )
    expected_wind_values_match = _wind_matches_expected(
        run_a, expected_wind_a_mps
    ) and _wind_matches_expected(run_b, expected_wind_b_mps)
    runtime_invocation_evidence_complete = _runtime_invocation_evidence_complete(
        runtime_invocations
    )
    runtime_pairing_complete = _runtime_pairing_complete(runtime_pairing)
    unsupported_reasons: list[str] = []
    if not wind_applications_observed:
        unsupported_reasons.append("wind_application_or_terminal_physics_not_observed")
    if not route_geometry_match:
        unsupported_reasons.append("source_route_geometry_mismatch")
    if not source_runs_completed:
        unsupported_reasons.append("source_runs_not_completed_cleanly")
    if not source_boundary_flags_safe:
        unsupported_reasons.append("source_run_forbidden_authority_flags_observed")
    if not expected_wind_values_match:
        unsupported_reasons.append("expected_wind_values_not_observed")
    if not runtime_invocation_evidence_complete:
        unsupported_reasons.append("runtime_invocation_evidence_missing_or_incomplete")
    if not runtime_pairing_complete:
        unsupported_reasons.append("runtime_pairing_not_condition_only_or_asymmetric_compensation")
    if not paired["paired_route_sample_count"]:
        unsupported_reasons.append("paired_route_pose_samples_not_observed")
    if max_observed_delta is None:
        unsupported_reasons.append("trajectory_delta_not_computable")
    supported_delta_observed = trajectory_delta_observed and not unsupported_reasons
    status = (
        "trajectory_delta_observed"
        if supported_delta_observed
        else "trajectory_delta_below_threshold"
        if not unsupported_reasons
        else "unsupported"
    )
    form1 = _form1_classification(
        supported_delta_observed=supported_delta_observed,
        max_observed_delta_m=max_observed_delta,
        delta_threshold_m=delta_threshold_m,
        form1a_margin_ratio=form1a_margin_ratio,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "audit_id": "drone_behavior_delta_under_wind:mission_designer_wind_speed",
        "condition_kind": "wind_speed_drone_behavior_delta",
        "audit_status": status,
        "requested": {
            "expected_wind_a_mps": expected_wind_a_mps,
            "expected_wind_b_mps": expected_wind_b_mps,
            "observed_wind_a_mps": run_a.get("wind_requested_mps"),
            "observed_wind_b_mps": run_b.get("wind_requested_mps"),
            "delta_threshold_m": delta_threshold_m,
        },
        "runs": [run_a, run_b],
        "source_binding": {
            "route_geometry_match": route_geometry_match,
            "source_runs_completed": source_runs_completed,
            "source_boundary_flags_safe": source_boundary_flags_safe,
            "expected_wind_values_match": expected_wind_values_match,
            "runtime_invocation_evidence_complete": (
                runtime_invocation_evidence_complete
            ),
            "runtime_pairing_complete": runtime_pairing_complete,
            "route_geometry_tolerance_m": ROUTE_GEOMETRY_TOLERANCE_M,
            "pickup_pose_jitter_tolerance_m": PICKUP_POSE_JITTER_TOLERANCE_M,
            "wind_value_tolerance_mps": WIND_VALUE_TOLERANCE_MPS,
        },
        "metrics": {
            **paired,
            "completed_pose_xy_delta_m": completed_delta,
            "max_route_cross_track_error_delta_m": max_cross_track_delta,
            "max_observed_delta_m": max_observed_delta,
            "delta_threshold_m": delta_threshold_m,
        },
        "drone_physics_affected": supported_delta_observed,
        "trajectory_delta_observed": supported_delta_observed,
        "raw_trajectory_delta_above_threshold": trajectory_delta_observed,
        "form1_claim_supported": supported_delta_observed,
        **form1,
        "threshold_config": threshold_config or {},
        "baseline_runtime_invocation_evidence": (
            runtime_invocations[0] if runtime_invocations else {}
        ),
        "condition_runtime_invocation_evidence": (
            runtime_invocations[1] if runtime_invocations and len(runtime_invocations) > 1 else {}
        ),
        "runtime_invocation_evidence_refs": [
            evidence.get("invocation_id")
            for evidence in (runtime_invocations or [])
            if evidence.get("invocation_id")
        ],
        "runtime_pairing": runtime_pairing or {},
        "form4_reclassification_candidate": (
            not supported_delta_observed and not unsupported_reasons
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


def _run_horizontal_route_smoke(
    *,
    label: str,
    wind_mps: float,
    wind_direction_deg: float,
    artifact_root: Path,
    include_form2a_compensation: bool = True,
) -> dict[str, Any]:
    command_argv = [
        sys.executable,
        "scripts/smoke_px4_gazebo_horizontal_route_delivery.py",
    ]
    condition_env = {
        "RUN_PX4_GAZEBO_HORIZONTAL_ROUTE_SMOKE": "1",
        "PX4_GAZEBO_HORIZONTAL_ROUTE_ARTIFACT_ROOT": str(artifact_root),
        "MISSION_DESIGNER_REALISM_WIND_MEAN_MPS": str(wind_mps),
        "MISSION_DESIGNER_REALISM_WIND_DIRECTION_DEG": str(wind_direction_deg),
    }
    if include_form2a_compensation:
        for key in FORM2A_WIND_COMPENSATION_ENV_KEYS:
            value = os.getenv(key)
            if value:
                condition_env[key] = value
    env = os.environ.copy()
    env.update(condition_env)
    env.pop("MISSION_DESIGNER_REALISM_WIND_GUST_MPS", None)
    env.pop("MISSION_DESIGNER_REALISM_WIND_VARIANCE", None)
    started_at = _utc_now_iso8601()
    process = subprocess.Popen(
        command_argv,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = process.communicate(timeout=SMOKE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        stdout, stderr = process.communicate()
        completed_at = _utc_now_iso8601()
        raise RuntimeError(
            "horizontal route smoke timed out "
            f"for {label} wind={wind_mps}: timeout={SMOKE_TIMEOUT_SECONDS}s\n"
            f"stdout_tail={stdout[-2000:]}\n"
            f"stderr_tail={stderr[-2000:]}"
        ) from exc
    completed_at = _utc_now_iso8601()
    evidence = {
        "schema_version": RUNTIME_INVOCATION_EVIDENCE_SCHEMA_VERSION,
        "invocation_id": f"runtime_invocation_evidence:{label}",
        "invocation_kind": "subprocess",
        "invocation_target": "scripts/smoke_px4_gazebo_horizontal_route_delivery.py",
        "invocation_started_at": started_at,
        "invocation_completed_at": completed_at,
        "invocation_stdout_sha256": _sha256_text(stdout),
        "invocation_stderr_sha256": _sha256_text(stderr),
        "invocation_exit_code": int(process.returncode),
        "command_argv": command_argv,
        "command_argv_sha256": _sha256_json(command_argv),
        "process_pid": int(process.pid),
        "condition_env": condition_env,
        "condition_env_sha256": _sha256_json(condition_env),
        "backend_target": "px4_gazebo_sitl",
        "opt_in_env": True,
        "docker_container_name": "boiled-claw-px4-gazebo-horizontal-route-smoke",
    }
    if process.returncode != 0:
        raise RuntimeError(
            "horizontal route smoke failed "
            f"for {label} wind={wind_mps}: rc={process.returncode}\n"
            f"stdout_tail={stdout[-2000:]}\n"
            f"stderr_tail={stderr[-2000:]}"
        )
    try:
        summary = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "horizontal route smoke did not emit JSON summary: "
            f"{stdout[-2000:]}"
        ) from exc
    run_dir = Path(summary["artifact_dir"])
    if not run_dir.exists():
        raise FileNotFoundError(f"reported artifact_dir does not exist: {run_dir}")
    evidence["artifact_dir"] = str(run_dir)
    evidence["runtime_summary_path"] = str(run_dir / "summary.json")
    return {"run_dir": run_dir, "evidence": evidence}


def _runtime_pairing(
    baseline: dict[str, Any],
    condition: dict[str, Any],
    *,
    pairing_mode: str = WIND_ONLY_PAIRING_MODE,
) -> dict[str, Any]:
    baseline_env = baseline.get("condition_env", {})
    condition_env = condition.get("condition_env", {})
    all_keys = sorted(set(baseline_env) | set(condition_env))
    diff_keys = [
        key for key in all_keys if baseline_env.get(key) != condition_env.get(key)
    ]
    asymmetric_expected_diff_keys = sorted(
        {"MISSION_DESIGNER_REALISM_WIND_MEAN_MPS", *FORM2A_WIND_COMPENSATION_ENV_KEYS}
    )
    form2a_compensation_absent_from_baseline = all(
        baseline_env.get(key) in {None, ""} for key in FORM2A_WIND_COMPENSATION_ENV_KEYS
    )
    form2a_compensation_present_in_condition = all(
        bool(condition_env.get(key)) for key in FORM2A_WIND_COMPENSATION_ENV_KEYS
    )
    asymmetric_compensation_pairing_observed = bool(
        pairing_mode == ASYMMETRIC_COMPENSATION_PAIRING_MODE
        and diff_keys == asymmetric_expected_diff_keys
        and form2a_compensation_absent_from_baseline
        and form2a_compensation_present_in_condition
    )
    return {
        "pairing_mode": pairing_mode,
        "command_argv_sha256_equal": baseline.get("command_argv_sha256")
        == condition.get("command_argv_sha256"),
        "command_argv_equal": baseline.get("command_argv")
        == condition.get("command_argv"),
        "condition_env_diff_keys": diff_keys,
        "condition_only_env_delta": diff_keys
        == ["MISSION_DESIGNER_REALISM_WIND_MEAN_MPS"],
        "asymmetric_compensation_pairing_observed": asymmetric_compensation_pairing_observed,
        "asymmetric_compensation_expected_env_diff_keys": asymmetric_expected_diff_keys,
        "form2a_compensation_absent_from_baseline": form2a_compensation_absent_from_baseline,
        "form2a_compensation_present_in_condition": form2a_compensation_present_in_condition,
        "baseline_artifact_root": baseline_env.get(
            "PX4_GAZEBO_HORIZONTAL_ROUTE_ARTIFACT_ROOT"
        ),
        "condition_artifact_root": condition_env.get(
            "PX4_GAZEBO_HORIZONTAL_ROUTE_ARTIFACT_ROOT"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit drone trajectory delta under two applied wind speeds."
    )
    parser.add_argument("--wind-a-mps", type=float)
    parser.add_argument("--wind-b-mps", type=float)
    parser.add_argument(
        "--wind-direction-deg", type=float
    )
    parser.add_argument(
        "--delta-threshold-m", type=float
    )
    parser.add_argument(
        "--threshold-config",
        type=Path,
        default=DEFAULT_THRESHOLD_CONFIG_PATH,
    )
    parser.add_argument("--run-a-dir", type=Path)
    parser.add_argument("--run-b-dir", type=Path)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/mission_designer_wind_trajectory_delta_audits"),
    )
    parser.add_argument(
        "--asymmetric-compensation",
        action="store_true",
        help=(
            "For Form 2a post-action measurement, run the baseline without "
            "Form 2a compensation env and the wind condition with compensation env."
        ),
    )
    args = parser.parse_args()

    threshold_config = (
        load_form1_wind_threshold_config(args.threshold_config)
        if args.threshold_config.exists()
        else {}
    )
    wind_a_mps = (
        args.wind_a_mps
        if args.wind_a_mps is not None
        else threshold_config.get("baseline_wind_mps", DEFAULT_WIND_A_MPS)
    )
    wind_b_mps = (
        args.wind_b_mps
        if args.wind_b_mps is not None
        else threshold_config.get("condition_wind_mps", DEFAULT_WIND_B_MPS)
    )
    wind_direction_deg = (
        args.wind_direction_deg
        if args.wind_direction_deg is not None
        else threshold_config.get("wind_direction_deg", DEFAULT_WIND_DIRECTION_DEG)
    )
    delta_threshold_m = (
        args.delta_threshold_m
        if args.delta_threshold_m is not None
        else threshold_config.get("minimum_observed_delta_m", DEFAULT_DELTA_THRESHOLD_M)
    )
    form1a_margin_ratio = float(threshold_config.get("form1a_margin_ratio", 2.0))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit_dir = args.output_dir / f"wind_trajectory_delta_{stamp}"
    audit_dir.mkdir(parents=True, exist_ok=False)
    run_root = audit_dir / "runs"
    runtime_invocations: list[dict[str, Any]] = []
    runtime_pairing: dict[str, Any] = {}
    if args.run_a_dir:
        run_a_dir = args.run_a_dir
    else:
        baseline_run = _run_horizontal_route_smoke(
            label="baseline",
            wind_mps=wind_a_mps,
            wind_direction_deg=wind_direction_deg,
            artifact_root=run_root,
            include_form2a_compensation=not args.asymmetric_compensation,
        )
        run_a_dir = baseline_run["run_dir"]
        runtime_invocations.append(baseline_run["evidence"])
    if args.run_b_dir:
        run_b_dir = args.run_b_dir
    else:
        condition_run = _run_horizontal_route_smoke(
            label="condition",
            wind_mps=wind_b_mps,
            wind_direction_deg=wind_direction_deg,
            artifact_root=run_root,
            include_form2a_compensation=True,
        )
        run_b_dir = condition_run["run_dir"]
        runtime_invocations.append(condition_run["evidence"])
    if len(runtime_invocations) == 2:
        runtime_pairing = _runtime_pairing(
            runtime_invocations[0],
            runtime_invocations[1],
            pairing_mode=ASYMMETRIC_COMPENSATION_PAIRING_MODE
            if args.asymmetric_compensation
            else WIND_ONLY_PAIRING_MODE,
        )
    artifact = build_drone_behavior_delta_under_wind(
        run_a_dir,
        run_b_dir,
        delta_threshold_m=delta_threshold_m,
        expected_wind_a_mps=wind_a_mps,
        expected_wind_b_mps=wind_b_mps,
        threshold_config=threshold_config,
        form1a_margin_ratio=form1a_margin_ratio,
        runtime_invocations=runtime_invocations,
        runtime_pairing=runtime_pairing,
    )
    artifact["audit_dir"] = str(audit_dir)
    artifact["run_mode"] = (
        "existing_runs" if args.run_a_dir and args.run_b_dir else "executed_runs"
    )
    output_path = audit_dir / "drone_behavior_delta_under_wind.json"
    _write_json(output_path, artifact)
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
