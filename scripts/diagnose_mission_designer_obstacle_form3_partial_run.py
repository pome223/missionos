#!/usr/bin/env python3
"""Diagnose partial obstacle Form 3 runs without claiming progress."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any

from scripts.audit_mission_designer_obstacle_alternate_route_closed_loop import (
    _write_json,
)


SCHEMA_VERSION = "mission_designer_obstacle_form3_partial_run_diagnostic.v1"


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
        if line.strip():
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


def _pose_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    phases = Counter(str(row.get("phase") or "unknown") for row in rows)
    poses = [_pose(row) for row in rows]
    valid_z = [pose["z"] for pose in poses if pose["z"] is not None]
    first_pose = poses[0] if poses else None
    last_pose = poses[-1] if poses else None
    return {
        "pose_sample_count": len(rows),
        "phase_counts": dict(sorted(phases.items())),
        "first_phase": rows[0].get("phase") if rows else None,
        "last_phase": rows[-1].get("phase") if rows else None,
        "first_pose": first_pose,
        "last_pose": last_pose,
        "xy_delta_m": (
            _distance_xy(first_pose, last_pose) if first_pose and last_pose else None
        ),
        "min_pose_z_m": min(valid_z) if valid_z else None,
        "max_pose_z_m": max(valid_z) if valid_z else None,
    }


def _missing_evidence(*, summary: dict[str, Any] | None, rows: list[dict[str, Any]]) -> list[str]:
    missing: list[str] = []
    if summary is None:
        missing.append("summary_json_missing")
    if not rows:
        missing.append("pose_samples_missing")
    if not (summary or {}).get("alternate_route_execution_evidence", {}).get(
        "alternate_waypoint_reached_observed"
    ):
        missing.append("cycle1_alternate_waypoint_observation_missing")
    if not (summary or {}).get("alternate_landing_command_dispatch", {}).get(
        "emergency_dispatch_ref"
    ):
        missing.append("cycle2_post_alternate_land_dispatch_missing")
    if not (summary or {}).get("alternate_landing_behavior_observation", {}).get(
        "alternate_landing_behavior_observed"
    ):
        missing.append("cycle2_land_action_outcome_observation_missing")
    return missing


def _inferred_blocker(
    *, summary: dict[str, Any] | None, pose_summary: dict[str, Any]
) -> str:
    if summary is not None:
        route = summary.get("alternate_route_execution_evidence") or {}
        landing = summary.get("alternate_landing_behavior_observation") or {}
        dispatch = summary.get("alternate_landing_command_dispatch") or {}
        if route.get("alternate_waypoint_reached_observed") is not True:
            return "cycle1_alternate_waypoint_not_observed"
        if not dispatch.get("emergency_dispatch_ref"):
            return "cycle2_post_alternate_land_not_dispatched"
        if landing.get("alternate_landing_behavior_observed") is not True:
            return "cycle2_land_outcome_not_observed"
        return "summary_present_without_strict_form3_blocker"

    phases = set(pose_summary.get("phase_counts") or {})
    if phases and "route" not in phases and "alternate_landing" not in phases:
        return "summary_missing_before_alternate_route_outcome"
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
            "mission_designer_obstacle_form3_partial_run_diagnostic:"
            f"{run_dir.name}"
        ),
        "condition_kind": "source_bound_obstacle_form3_live_blocker",
        "causal_form": "Form 0b",
        "diagnostic_status": "live_blocker_diagnosed",
        "form3_claim_supported": False,
        "progress_counted": False,
        "artifact_dir": str(run_dir),
        "summary_json_present": summary is not None,
        "pose_samples_present": bool(pose_rows),
        "pose_trace": pose,
        "inferred_blocker": blocker,
        "ready_blocker": "live_px4_gazebo_obstacle_form3_not_observed",
        "missing_evidence": missing,
        "strict_form3_requirements": {
            "cycle1_alternate_waypoint_observation": (
                (summary or {})
                .get("alternate_route_execution_evidence", {})
                .get("alternate_waypoint_reached_observed")
                is True
            ),
            "cycle2_distinct_post_alternate_land_dispatch": bool(
                (summary or {})
                .get("alternate_landing_command_dispatch", {})
                .get("emergency_dispatch_ref")
            ),
            "cycle2_land_action_outcome_observation": (
                (summary or {})
                .get("alternate_landing_behavior_observation", {})
                .get("alternate_landing_behavior_observed")
                is True
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
        description="Diagnose a partial obstacle Form 3 live run as Form 0b evidence."
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
    diagnostic_dir = args.output_dir / f"obstacle_form3_partial_run_diagnostic_{stamp}"
    output_path = diagnostic_dir / "mission_designer_obstacle_form3_partial_run_diagnostic.json"
    _write_json(output_path, artifact)
    artifact["diagnostic_dir"] = str(diagnostic_dir)
    artifact["diagnostic_path"] = str(output_path)
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
