#!/usr/bin/env python3
"""Diagnose partial wind Form 3 runtime runs without claiming progress.

The strict Form 3 audit needs two response-triggered bounded actions and their
action-outcome observations. This diagnostic reads a partial run directory,
including a `pose_samples.jsonl` when `summary.json` was never emitted, and
records the current live blocker as Form 0b evidence.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any

from scripts.audit_mission_designer_wind_drift_recovery_closed_loop import (
    _write_json,
)


SCHEMA_VERSION = "mission_designer_wind_form3_partial_run_diagnostic.v1"


def _as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _read_pose_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def _pose(row: dict[str, Any]) -> dict[str, float | None]:
    sample = row.get("sample") or {}
    return {
        "x": _as_float(sample.get("x")),
        "y": _as_float(sample.get("y")),
        "z": _as_float(sample.get("z")),
    }


def _distance_xy(first: dict[str, float | None], last: dict[str, float | None]) -> float | None:
    if first["x"] is None or first["y"] is None:
        return None
    if last["x"] is None or last["y"] is None:
        return None
    return math.hypot(last["x"] - first["x"], last["y"] - first["y"])


def _delta_z(first: dict[str, float | None], last: dict[str, float | None]) -> float | None:
    if first["z"] is None or last["z"] is None:
        return None
    return last["z"] - first["z"]


def _pose_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    phases = Counter(str(row.get("phase") or "unknown") for row in rows)
    poses = [_pose(row) for row in rows]
    valid_z = [pose["z"] for pose in poses if pose["z"] is not None]
    first_pose = poses[0] if poses else None
    last_pose = poses[-1] if poses else None
    xy_delta = _distance_xy(first_pose, last_pose) if first_pose and last_pose else None
    z_delta = _delta_z(first_pose, last_pose) if first_pose and last_pose else None
    return {
        "pose_sample_count": len(rows),
        "phase_counts": dict(sorted(phases.items())),
        "first_phase": rows[0].get("phase") if rows else None,
        "last_phase": rows[-1].get("phase") if rows else None,
        "first_pose": first_pose,
        "last_pose": last_pose,
        "xy_delta_m": xy_delta,
        "z_delta_m": z_delta,
        "min_pose_z_m": min(valid_z) if valid_z else None,
        "max_pose_z_m": max(valid_z) if valid_z else None,
    }


def _missing_evidence(*, summary: dict[str, Any] | None, rows: list[dict[str, Any]]) -> list[str]:
    missing: list[str] = []
    if summary is None:
        missing.append("summary_json_missing")
    if not rows:
        missing.append("pose_samples_missing")
    if not (summary or {}).get("recovery_state_observed"):
        missing.append("cycle1_rtl_state_observation_missing")
    if not (summary or {}).get("post_recovery_dispatch_ref"):
        missing.append("cycle2_land_dispatch_missing")
    if not (summary or {}).get("post_recovery_completion_ref"):
        missing.append("cycle2_land_outcome_observation_missing")
    return missing


def _inferred_blocker(
    *, summary: dict[str, Any] | None, pose_summary: dict[str, Any]
) -> str:
    if summary is not None:
        label = summary.get("recovery_state_label")
        if label == "hold_command_unsupported":
            return "cycle1_hold_command_unsupported"
        if summary.get("recovery_state_observed") is not True:
            return "cycle1_recovery_state_not_observed"
        if summary.get("post_recovery_completed") is not True:
            return "cycle2_land_outcome_not_observed"
        return "summary_present_without_strict_form3_blocker"

    phases = set(pose_summary.get("phase_counts") or {})
    if phases and phases <= {"pickup", "climb"}:
        return "pre_recovery_climb_stall_or_takeoff_not_observed"
    if "recovery" not in phases and "post_recovery" not in phases:
        return "summary_missing_before_recovery_state_observation"
    return "summary_missing_after_partial_pose_trace"


def build_diagnostic(run_dir: Path) -> dict[str, Any]:
    summary = _read_json(run_dir / "summary.json")
    pose_rows = _read_pose_rows(run_dir / "pose_samples.jsonl")
    pose = _pose_summary(pose_rows)
    missing = _missing_evidence(summary=summary, rows=pose_rows)
    blocker = _inferred_blocker(summary=summary, pose_summary=pose)
    return {
        "schema_version": SCHEMA_VERSION,
        "diagnostic_id": (
            "mission_designer_wind_form3_partial_run_diagnostic:"
            f"{run_dir.name}"
        ),
        "condition_kind": "source_bound_wind_drift_form3_live_blocker",
        "causal_form": "Form 0b",
        "diagnostic_status": "live_blocker_diagnosed",
        "form3_claim_supported": False,
        "progress_counted": False,
        "artifact_dir": str(run_dir),
        "summary_json_present": summary is not None,
        "pose_samples_present": bool(pose_rows),
        "pose_trace": pose,
        "inferred_blocker": blocker,
        "ready_blocker": "live_px4_gazebo_wind_form3_not_observed",
        "missing_evidence": missing,
        "strict_form3_requirements": {
            "cycle1_rtl_state_observation": (summary or {}).get(
                "recovery_state_observed"
            )
            is True,
            "cycle2_bounded_land_dispatch": bool(
                (summary or {}).get("post_recovery_dispatch_ref")
            ),
            "cycle2_land_action_outcome_observation": bool(
                (summary or {}).get("post_recovery_completion_ref")
            ),
            "boundary_recheck_only": False,
        },
        "safety_boundary": {
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "ai_judgment_is_gate_verdict": False,
            "ai_judgment_created_dispatch_authority": False,
            "llm_gate_judge_used": False,
            "diagnostic_created_dispatch_authority": False,
        },
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose a partial wind Form 3 live run as Form 0b evidence."
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/mission_designer_behavior_delta_audits"),
    )
    args = parser.parse_args()
    artifact = build_diagnostic(args.run_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    diagnostic_dir = args.output_dir / f"wind_form3_partial_run_diagnostic_{stamp}"
    output_path = diagnostic_dir / "mission_designer_wind_form3_partial_run_diagnostic.json"
    _write_json(output_path, artifact)
    artifact["diagnostic_dir"] = str(diagnostic_dir)
    artifact["diagnostic_path"] = str(output_path)
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
