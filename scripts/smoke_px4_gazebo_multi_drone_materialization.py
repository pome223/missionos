#!/usr/bin/env python3
"""Scoped Gazebo multi-drone materialization smoke for #672."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import textwrap
from typing import Any

from src.runtime.gz_sim_log_collector import parse_gz_sim_entity_pose


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = ROOT / "output" / "mission_designer_multi_drone_runs"
DEFAULT_DOCKER_IMAGE = "px4io/px4-sitl-gazebo:latest"
PRIMARY_VEHICLE_ID = "mission_designer_drone_primary"
SECONDARY_VEHICLE_ID = "mission_designer_drone_secondary"
FRAME = "gazebo_world_local"


MULTI_DRONE_WORLD_SDF = f"""\
<?xml version="1.0" ?>
<sdf version="1.9">
  <world name="default">
    <plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics"/>
    <plugin filename="gz-sim-user-commands-system" name="gz::sim::systems::UserCommands"/>
    <plugin filename="gz-sim-scene-broadcaster-system" name="gz::sim::systems::SceneBroadcaster"/>
    <model name="ground_plane">
      <static>true</static>
      <link name="link">
        <collision name="collision">
          <geometry><plane><normal>0 0 1</normal><size>20 20</size></plane></geometry>
        </collision>
        <visual name="visual">
          <geometry><plane><normal>0 0 1</normal><size>20 20</size></plane></geometry>
        </visual>
      </link>
    </model>
    <include>
      <uri>model://x500</uri>
      <name>{PRIMARY_VEHICLE_ID}</name>
      <pose>0 0 0.2 0 0 0</pose>
    </include>
    <include>
      <uri>model://x500</uri>
      <name>{SECONDARY_VEHICLE_ID}</name>
      <pose>2 1 0.2 0 0 0</pose>
    </include>
  </world>
</sdf>
"""


def _utc_timestamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def _run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _tail(value: str, limit: int = 1200) -> str:
    return value[-limit:]


def _pose_or_none(sample_text: str, entity_name: str) -> dict[str, float] | None:
    try:
        pose = parse_gz_sim_entity_pose(sample_text, entity_name=entity_name)
    except Exception:
        return None
    return {key: float(pose[key]) for key in ("x", "y", "z")}


def build_multi_drone_materialization_summary(
    *,
    artifact_dir: Path,
    docker_image: str,
    docker_result: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    pose_path = artifact_dir / "pose_info.txt"
    topics_path = artifact_dir / "topics.txt"
    pose_rc_path = artifact_dir / "pose_info.rc"
    world_path = artifact_dir / "multi_drone_probe.sdf"

    pose_text = pose_path.read_text(encoding="utf-8") if pose_path.exists() else ""
    topics = (
        topics_path.read_text(encoding="utf-8").splitlines()
        if topics_path.exists()
        else []
    )
    pose_rc = pose_rc_path.read_text(encoding="utf-8").strip() if pose_rc_path.exists() else ""
    primary_pose = _pose_or_none(pose_text, PRIMARY_VEHICLE_ID)
    secondary_pose = _pose_or_none(pose_text, SECONDARY_VEHICLE_ID)
    both_observed = primary_pose is not None and secondary_pose is not None
    separation_xy_m = (
        math.hypot(
            float(secondary_pose["x"]) - float(primary_pose["x"]),
            float(secondary_pose["y"]) - float(primary_pose["y"]),
        )
        if both_observed
        else None
    )
    status = "multi_drone_materialized" if both_observed else "blocked"

    return {
        "schema_version": "mission_designer_multi_drone_materialization_smoke_summary.v1",
        "status": status,
        "artifact_dir": str(artifact_dir),
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "artifacts": {
            "multi_vehicle_frame_contract": {
                "schema_version": "multi_vehicle_frame_contract.v1",
                "primary_vehicle_id": PRIMARY_VEHICLE_ID,
                "additional_vehicle_ids": [SECONDARY_VEHICLE_ID],
                "frame": FRAME,
                "multi_vehicle_enabled": both_observed,
                "px4_multi_autopilot_enabled": False,
                "conflict_verifier_enabled": False,
                "hardware_target_allowed": False,
                "physical_execution_invoked": False,
            },
            "multi_drone_materialization_application": {
                "schema_version": "multi_drone_materialization_application.v1",
                "application_status": "applied" if docker_result.returncode == 0 else "blocked",
                "method": "scoped_gazebo_two_x500_world",
                "docker_image": docker_image,
                "world_sdf_path": str(world_path),
                "primary_vehicle_materialized": primary_pose is not None,
                "secondary_vehicle_materialized": secondary_pose is not None,
                "route_execution_invoked": False,
                "mavlink_dispatch_performed": False,
                "px4_mission_upload_performed": False,
                "px4_multi_autopilot_enabled": False,
                "task_status_mutated": False,
                "delivery_completion_claimed": False,
            },
            "multi_drone_materialization_evidence": {
                "schema_version": "multi_drone_materialization_evidence.v1",
                "observation_status": status,
                "observed": {
                    "source": "gz_topic_pose_info_read_only",
                    "topic": "/world/default/pose/info",
                    "topics_path": str(topics_path),
                    "pose_info_path": str(pose_path),
                    "pose_info_returncode": pose_rc,
                    "pose_info_sha256": _sha256_text(pose_text) if pose_text else "",
                    "pose_info_stdout_tail": _tail(pose_text),
                    "topics": [line.strip() for line in topics if line.strip()],
                    "primary_vehicle_id": PRIMARY_VEHICLE_ID,
                    "secondary_vehicle_id": SECONDARY_VEHICLE_ID,
                    "primary_pose_xyz_m": primary_pose,
                    "secondary_pose_xyz_m": secondary_pose,
                    "frame": FRAME,
                    "multi_drone_pose_observed": both_observed,
                    "vehicle_count_observed": 2 if both_observed else int(primary_pose is not None)
                    + int(secondary_pose is not None),
                    "separation_xy_m": separation_xy_m,
                    "read_only_observer": True,
                    "traffic_conflict_verified": False,
                    "route_blocking_observed": False,
                    "incident_observed": False,
                    "task_status_mutated": False,
                    "gate_status_mutated": False,
                    "delivery_completion_claimed": False,
                },
                "second_vehicle_materialized_is_not_conflict": True,
                "gazebo_multi_drone_is_not_px4_multi_autopilot": True,
                "simulator_only": True,
                "hardware_target_allowed": False,
                "physical_execution_invoked": False,
                "delivery_completion_claimed": False,
            },
            "scenario_cleanup_receipt": {
                "schema_version": "scenario_cleanup_receipt.v1",
                "cleanup_status": "isolated_container_teardown_observed"
                if docker_result.returncode == 0
                else "container_run_failed_or_timeout",
                "docker_run_rm": True,
                "gazebo_process_stopped": True,
                "route_execution_invoked": False,
                "task_status_mutated": False,
                "hardware_target_allowed": False,
                "physical_execution_invoked": False,
            },
        },
        "docker": {
            "returncode": docker_result.returncode,
            "stdout_tail": _tail(docker_result.stdout),
            "stderr_tail": _tail(docker_result.stderr),
        },
    }


def run_multi_drone_materialization_smoke(
    *, output_root: Path, docker_image: str
) -> dict[str, Any]:
    artifact_dir = output_root / f"multi_drone_{_utc_timestamp()}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "multi_drone_probe.sdf").write_text(
        MULTI_DRONE_WORLD_SDF,
        encoding="utf-8",
    )
    shell = textwrap.dedent(
        """
        set -eu
        cp /in/multi_drone_probe.sdf /tmp/multi_drone_probe.sdf
        gz sim -s -r /tmp/multi_drone_probe.sdf > /out/gz_sim.log 2>&1 &
        gz_pid=$!
        cleanup() {
          kill "$gz_pid" >/dev/null 2>&1 || true
          wait "$gz_pid" >/dev/null 2>&1 || true
        }
        trap cleanup EXIT
        sleep 5
        gz topic -l > /out/topics.txt
        timeout 5 gz topic -e -t /world/default/pose/info -n 1 > /out/pose_info.txt 2> /out/pose_info.err
        printf '%s' "$?" > /out/pose_info.rc
        """
    ).strip()
    result = _run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "-v",
            f"{artifact_dir}:/out",
            "-v",
            f"{artifact_dir}:/in:ro",
            docker_image,
            "-lc",
            shell,
        ],
        timeout=90,
    )
    summary = build_multi_drone_materialization_summary(
        artifact_dir=artifact_dir,
        docker_image=docker_image,
        docker_result=result,
    )
    (artifact_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        default=os.getenv("MISSION_DESIGNER_MULTI_DRONE_OUTPUT_ROOT")
        or str(DEFAULT_OUTPUT_ROOT),
    )
    parser.add_argument(
        "--docker-image",
        default=os.getenv("PX4_GAZEBO_DOCKER_IMAGE") or DEFAULT_DOCKER_IMAGE,
    )
    args = parser.parse_args()
    summary = run_multi_drone_materialization_smoke(
        output_root=Path(args.output_root).expanduser().resolve(),
        docker_image=args.docker_image,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary.get("status") == "multi_drone_materialized" else 1


if __name__ == "__main__":
    raise SystemExit(main())
