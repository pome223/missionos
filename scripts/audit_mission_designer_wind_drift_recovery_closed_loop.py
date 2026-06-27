#!/usr/bin/env python3
"""Audit the wind -> drift -> bounded recovery closed loop.

This does not add a new verifier, candidate, approval chain, gate, or delivery
completion authority. It runs or reads one horizontal-route SITL summary and
records whether an already source-bound wind applicator can drive a scoped
route-drift response into an operator-approved bounded recovery action.
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


SCHEMA_VERSION = "mission_designer_wind_drift_recovery_closed_loop_audit.v1"
DEFAULT_WIND_MPS = 4.0
DEFAULT_WIND_DIRECTION_DEG = 90.0
DEFAULT_DRIFT_THRESHOLD_M = 0.85
DEFAULT_RECOVERY_ACTION = "land"
WIND_VALUE_TOLERANCE_MPS = 1e-6
UNSAFE_AUTHORITY_KEYS = (
    "auto_gate",
    "task_status_mutated",
    "gate_status_mutated",
    "dropoff_verified",
    "delivery_completion_claimed",
    "hardware_target_allowed",
    "physical_execution_invoked",
    "approval_free_dispatch_allowed",
    "approval_free_recovery_dispatch_allowed",
    "actuator_execution_performed",
    "physical_actuator_execution_allowed",
    "real_world_authority_granted",
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


def _as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _float_matches(value: Any, expected: float, *, tolerance: float = 1e-6) -> bool:
    actual = _as_float(value)
    return actual is not None and abs(actual - expected) <= tolerance


def _wind_application_source_bound(
    summary: dict[str, Any],
    *,
    expected_wind_mps: float,
    expected_direction_deg: float,
) -> bool:
    application = summary.get("simulator_condition_application") or {}
    applied = application.get("applied") or {}
    observed = (summary.get("observed_environment_evidence") or {}).get("observed") or {}
    return (
        application.get("schema_version") == "simulator_condition_application.v1"
        and application.get("application_id")
        == "simulator_condition_application:mission_designer_wind_gust"
        and application.get("condition_kind") == "wind_gust"
        and application.get("application_status") == "applied"
        and (summary.get("observed_environment_evidence") or {}).get("evidence_id")
        == "observed_environment_evidence:mission_designer_wind_gust"
        and (summary.get("observed_environment_evidence") or {}).get("application_ref")
        == application.get("application_id")
        and _float_matches(
            applied.get("requested_mps"),
            expected_wind_mps,
            tolerance=WIND_VALUE_TOLERANCE_MPS,
        )
        and _float_matches(
            applied.get("applied_mps"),
            expected_wind_mps,
            tolerance=WIND_VALUE_TOLERANCE_MPS,
        )
        and _float_matches(
            applied.get("requested_direction_deg"),
            expected_direction_deg,
            tolerance=WIND_VALUE_TOLERANCE_MPS,
        )
        and _float_matches(
            applied.get("applied_direction_deg"),
            expected_direction_deg,
            tolerance=WIND_VALUE_TOLERANCE_MPS,
        )
        and observed.get("wind_topic_readback_observed") is True
        and observed.get("wind_effects_world_sdf_readback_observed") is True
        and observed.get("wind_effects_plugin_materialized") is True
        and observed.get("wind_enabled_on_vehicle_links") is True
        and observed.get("wind_world_linear_velocity_matches_requested") is True
        and observed.get("gazebo_runtime_world_model_readback_observed") is True
        and observed.get("gazebo_runtime_world_path_observed") is True
        and observed.get("gazebo_runtime_world_ready_observed") is True
        and observed.get("gazebo_runtime_model_bridge_observed") is True
        and observed.get("gazebo_runtime_vehicle_pose_observed") is True
    )


def _wind_drift_observed(
    summary: dict[str, Any],
    *,
    drift_threshold_m: float,
) -> bool:
    samples = summary.get("deviation_samples")
    if not isinstance(samples, list) or not samples:
        return False
    first = samples[0] if isinstance(samples[0], dict) else {}
    deviation_xy = _as_float(first.get("deviation_xy_m"))
    threshold_xy = _as_float(first.get("threshold_xy_m"))
    return (
        summary.get("pose_deviation_gate_active") is True
        and summary.get("pose_deviation_aborted") is True
        and deviation_xy is not None
        and deviation_xy >= drift_threshold_m
        and threshold_xy is not None
        and abs(threshold_xy - drift_threshold_m) <= 1e-6
        and summary.get("route_stream_terminated_before_recovery_dispatch") is True
        and summary.get("route_stream_stop_reason")
        in ("pose_deviation", "pose_deviation_forced_kill")
    )


def _bounded_recovery_observed(
    summary: dict[str, Any],
    *,
    expected_action: str,
) -> bool:
    if expected_action == "land":
        recovery_pose_z_m = _as_float(summary.get("recovery_pose_z_m"))
        return (
            summary.get("recovery_action_taken") == "land"
            and summary.get("recovery_completed") is True
            and summary.get("recovery_state_observed") is True
            and summary.get("recovery_completion_basis")
            in (
                "state_observed_after_dispatch_timeout",
                "accepted_ack_and_state_observed",
            )
            and summary.get("recovery_dispatch_status") in ("accepted", "timeout")
            and recovery_pose_z_m is not None
            and recovery_pose_z_m <= 0.15
        )
    if expected_action == "rtl":
        return (
            summary.get("recovery_action_taken") == "rtl"
            and summary.get("recovery_completed") is True
            and summary.get("recovery_state_observed") is True
            and summary.get("recovery_state_label")
            == "return_to_launch_state_observed"
            and summary.get("recovery_dispatch_status") in ("accepted", "timeout")
        )
    return False


def _source_refs_observed(summary: dict[str, Any]) -> bool:
    return (
        str(summary.get("deviation_abort_ref", "")).startswith(
            "px4_gazebo_route_deviation_abort:"
        )
        and str(summary.get("recovery_completion_ref", "")).startswith(
            "px4_gazebo_route_recovery_completion:"
        )
        and str(summary.get("recovery_dispatch_ref", "")).startswith(
            "px4_gazebo_emergency_command_dispatch_result:"
        )
    )


def _summarize_closed_loop(
    run_dir: Path,
    *,
    expected_wind_mps: float,
    expected_direction_deg: float,
    drift_threshold_m: float,
    expected_recovery_action: str,
) -> dict[str, Any]:
    summary = _read_json(_summary_path(run_dir))
    deviation_sample = (
        summary.get("deviation_samples", [{}])[0]
        if isinstance(summary.get("deviation_samples"), list)
        and summary.get("deviation_samples")
        else {}
    )
    unsafe_flags = sorted(set(_nested_true_keys(summary, set(UNSAFE_AUTHORITY_KEYS))))
    checks = {
        "horizontal_route_smoke_observed": summary.get(
            "actual_px4_gazebo_horizontal_smoke_observed"
        )
        is True,
        "wind_application_source_bound": _wind_application_source_bound(
            summary,
            expected_wind_mps=expected_wind_mps,
            expected_direction_deg=expected_direction_deg,
        ),
        "wind_drift_observed": _wind_drift_observed(
            summary,
            drift_threshold_m=drift_threshold_m,
        ),
        "operator_approved_bounded_recovery_observed": _bounded_recovery_observed(
            summary,
            expected_action=expected_recovery_action,
        ),
        "source_refs_observed": _source_refs_observed(summary),
        "task_completed_by_recovery_not_delivery": summary.get("task_status")
        == "completed"
        and str(summary.get("final_status", "")).startswith(
            f"recovered_{expected_recovery_action}"
        ),
        "dropoff_not_claimed": summary.get("dropoff_region_reached") is False
        and summary.get("dropoff_verified") is False
        and summary.get("delivery_completion_claimed") is False,
        "top_level_hardware_physical_false": summary.get("hardware_target_allowed")
        is False
        and summary.get("physical_execution_invoked") is False,
        "unsafe_authority_flags_absent": not unsafe_flags,
    }
    missing = [name for name, passed in checks.items() if not passed]
    observed = not missing
    return {
        "schema_version": SCHEMA_VERSION,
        "audit_id": (
            "mission_designer_wind_drift_recovery_closed_loop_audit:"
            "mission_designer_wind_speed"
        ),
        "condition_kind": "source_bound_wind_drift_recovery_closed_loop",
        "audit_status": "closed_loop_observed" if observed else "unsupported",
        "closed_loop_observed": observed,
        "form1_closed_loop_supported": observed,
        "artifact_dir": str(run_dir),
        "requested": {
            "wind_mean_mps": expected_wind_mps,
            "wind_direction_deg": expected_direction_deg,
            "drift_threshold_m": drift_threshold_m,
            "operator_approved_recovery_action": expected_recovery_action,
            "recovery_action_is_not_delivery_completion": True,
        },
        "checks": checks,
        "unsupported_reasons": [
            f"{name}_not_observed"
            for name in missing
            if name != "unsafe_authority_flags_absent"
        ]
        + (["source_run_forbidden_authority_flags_observed"] if unsafe_flags else []),
        "observed": {
            "task_status": summary.get("task_status"),
            "final_status": summary.get("final_status"),
            "dropoff_region_reached": summary.get("dropoff_region_reached"),
            "blocked_reasons": summary.get("blocked_reasons", []),
            "wind_application_status": (
                summary.get("simulator_condition_application") or {}
            ).get("application_status"),
            "wind_topic_readback_observed": (
                (summary.get("observed_environment_evidence") or {}).get(
                    "observed"
                )
                or {}
            ).get("wind_topic_readback_observed"),
            "gazebo_runtime_world_model_readback_observed": (
                (summary.get("observed_environment_evidence") or {}).get(
                    "observed"
                )
                or {}
            ).get("gazebo_runtime_world_model_readback_observed"),
            "pose_deviation_aborted": summary.get("pose_deviation_aborted"),
            "route_stream_stop_reason": summary.get("route_stream_stop_reason"),
            "route_stream_terminated_before_recovery_dispatch": summary.get(
                "route_stream_terminated_before_recovery_dispatch"
            ),
            "wind_drift_deviation_xy_m": (
                deviation_sample.get("deviation_xy_m")
                if isinstance(deviation_sample, dict)
                else None
            ),
            "wind_drift_threshold_xy_m": (
                deviation_sample.get("threshold_xy_m")
                if isinstance(deviation_sample, dict)
                else None
            ),
            "recovery_action_taken": summary.get("recovery_action_taken"),
            "recovery_dispatch_status": summary.get("recovery_dispatch_status"),
            "recovery_command_ack_observed": summary.get(
                "recovery_command_ack_observed"
            ),
            "recovery_completion_basis": summary.get("recovery_completion_basis"),
            "recovery_completed": summary.get("recovery_completed"),
            "recovery_state_observed": summary.get("recovery_state_observed"),
            "recovery_pose_z_m": summary.get("recovery_pose_z_m"),
            "dropoff_verified": summary.get("dropoff_verified"),
            "delivery_completion_claimed": summary.get("delivery_completion_claimed"),
            "unsafe_authority_flags_observed": unsafe_flags,
        },
        "source_refs": {
            "wind_application": "simulator_condition_application:mission_designer_wind_gust",
            "wind_observed_environment_evidence": (
                "observed_environment_evidence:mission_designer_wind_gust"
            ),
            "route_deviation_abort": summary.get("deviation_abort_ref"),
            "recovery_dispatch": summary.get("recovery_dispatch_ref"),
            "recovery_completion": summary.get("recovery_completion_ref"),
        },
        "adds_verifier": False,
        "adds_candidate": False,
        "adds_approval_chain": False,
        "adds_gate": False,
        "uses_existing_operator_approved_recovery": True,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "dropoff_verified": False,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def _run_horizontal_route_smoke(
    *,
    wind_mps: float,
    wind_direction_deg: float,
    drift_threshold_m: float,
    recovery_action: str,
    artifact_root: Path,
) -> Path:
    env = os.environ.copy()
    env.update(
        {
            "RUN_PX4_GAZEBO_HORIZONTAL_ROUTE_SMOKE": "1",
            "PX4_GAZEBO_HORIZONTAL_ROUTE_ARTIFACT_ROOT": str(artifact_root),
            "MISSION_DESIGNER_REALISM_WIND_MEAN_MPS": str(wind_mps),
            "MISSION_DESIGNER_REALISM_WIND_DIRECTION_DEG": str(wind_direction_deg),
        }
    )
    env.pop("MISSION_DESIGNER_REALISM_WIND_GUST_MPS", None)
    env.pop("MISSION_DESIGNER_REALISM_WIND_VARIANCE", None)
    result = subprocess.run(
        [
            sys.executable,
            "scripts/smoke_px4_gazebo_horizontal_route_delivery.py",
            "--on-deviation-action",
            recovery_action,
            "--max-pose-deviation-xy-m",
            str(drift_threshold_m),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "horizontal route wind drift recovery smoke failed: "
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit wind-drift -> bounded recovery closed-loop behavior."
    )
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--wind-mps", type=float, default=DEFAULT_WIND_MPS)
    parser.add_argument(
        "--wind-direction-deg", type=float, default=DEFAULT_WIND_DIRECTION_DEG
    )
    parser.add_argument(
        "--drift-threshold-m", type=float, default=DEFAULT_DRIFT_THRESHOLD_M
    )
    parser.add_argument(
        "--recovery-action",
        choices=("land", "rtl"),
        default=DEFAULT_RECOVERY_ACTION,
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/mission_designer_behavior_delta_audits"),
    )
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit_dir = args.output_dir / f"wind_drift_recovery_closed_loop_{stamp}"
    audit_dir.mkdir(parents=True, exist_ok=False)
    run_dir = args.run_dir or _run_horizontal_route_smoke(
        wind_mps=args.wind_mps,
        wind_direction_deg=args.wind_direction_deg,
        drift_threshold_m=args.drift_threshold_m,
        recovery_action=args.recovery_action,
        artifact_root=audit_dir / "runs" / "wind_drift_recovery",
    )
    artifact = _summarize_closed_loop(
        run_dir,
        expected_wind_mps=args.wind_mps,
        expected_direction_deg=args.wind_direction_deg,
        drift_threshold_m=args.drift_threshold_m,
        expected_recovery_action=args.recovery_action,
    )
    artifact["audit_dir"] = str(audit_dir)
    artifact["run_mode"] = "existing_run" if args.run_dir else "executed_run"
    output_path = audit_dir / "mission_designer_wind_drift_recovery_closed_loop.json"
    _write_json(output_path, artifact)
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
