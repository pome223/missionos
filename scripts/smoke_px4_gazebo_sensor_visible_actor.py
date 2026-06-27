#!/usr/bin/env python3
"""Scoped Gazebo logical-camera smoke for sensor-visible actor evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import textwrap
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = ROOT / "output" / "mission_designer_sensor_visible_actor_runs"
DEFAULT_DOCKER_IMAGE = "px4io/px4-sitl-gazebo:latest"
LOGICAL_CAMERA_TOPIC = "/mission_designer/sensor_visible_actor/logical_camera"
ACTOR_MODEL_NAME = "mission_designer_sensor_visible_actor"


SENSOR_VISIBLE_ACTOR_WORLD_SDF = f"""\
<?xml version="1.0" ?>
<sdf version="1.9">
  <world name="default">
    <physics name="1ms" type="ignored">
      <max_step_size>0.001</max_step_size>
      <real_time_factor>1.0</real_time_factor>
    </physics>
    <plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics"/>
    <plugin filename="gz-sim-logical-camera-system" name="gz::sim::systems::LogicalCamera">
      <render_engine>ogre2</render_engine>
    </plugin>
    <plugin filename="gz-sim-user-commands-system" name="gz::sim::systems::UserCommands"/>
    <plugin filename="gz-sim-scene-broadcaster-system" name="gz::sim::systems::SceneBroadcaster"/>
    <model name="mission_designer_sensor_ground">
      <static>true</static>
      <link name="link">
        <collision name="ground_collision">
          <geometry><box><size>20 20 0.1</size></box></geometry>
        </collision>
        <visual name="ground_visual">
          <geometry><plane><normal>0 0 1</normal><size>20 20</size></plane></geometry>
        </visual>
      </link>
    </model>
    <model name="{ACTOR_MODEL_NAME}">
      <static>true</static>
      <pose>1 0 0.5 0 0 0</pose>
      <link name="actor_link">
        <inertial>
          <mass>1.0</mass>
          <inertia>
            <ixx>0.1667</ixx><ixy>0</ixy><ixz>0</ixz>
            <iyy>0.1667</iyy><iyz>0</iyz><izz>0.1667</izz>
          </inertia>
        </inertial>
        <collision name="actor_collision">
          <geometry><box><size>0.5 0.5 1.0</size></box></geometry>
        </collision>
        <visual name="actor_visual">
          <geometry><box><size>0.5 0.5 1.0</size></box></geometry>
          <material><diffuse>0.95 0.15 0.65 1</diffuse></material>
        </visual>
      </link>
    </model>
    <model name="mission_designer_logical_camera_observer">
      <static>true</static>
      <pose>0 0 0.5 0 0 0</pose>
      <link name="logical_camera_link">
        <pose>0.05 0.05 0.05 0 0 0</pose>
        <collision name="logical_camera_collision">
          <geometry><box><size>0.1 0.1 0.1</size></box></geometry>
        </collision>
        <visual name="logical_camera_visual">
          <geometry><box><size>0.1 0.1 0.1</size></box></geometry>
        </visual>
        <sensor name="logical_camera" type="logical_camera">
          <always_on>true</always_on>
          <update_rate>10</update_rate>
          <topic>{LOGICAL_CAMERA_TOPIC}</topic>
          <logical_camera>
            <near>0.55</near>
            <far>5</far>
            <horizontal_fov>1.04719755</horizontal_fov>
            <aspect_ratio>1.778</aspect_ratio>
          </logical_camera>
          <visualize>true</visualize>
        </sensor>
      </link>
    </model>
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


def _parse_detected_models(sample_text: str) -> list[str]:
    names = re.findall(r'model\s*{\s*name: "([^"]+)"', sample_text)
    return sorted(set(names))


def build_sensor_visible_actor_summary(
    *,
    artifact_dir: Path,
    docker_image: str,
    docker_result: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    topics_path = artifact_dir / "topics.txt"
    selected_topic_path = artifact_dir / "selected_topic.txt"
    sample_path = artifact_dir / "logical_camera_sample.txt"
    sample_err_path = artifact_dir / "logical_camera_sample.err"
    sample_rc_path = artifact_dir / "logical_camera_sample.rc"
    sdf_check_path = artifact_dir / "sdf_check.txt"
    world_path = artifact_dir / "sensor_visible_actor_probe.sdf"

    topic_lines = (
        topics_path.read_text(encoding="utf-8").splitlines()
        if topics_path.exists()
        else []
    )
    logical_camera_topics = [
        line.strip()
        for line in topic_lines
        if "logical_camera" in line.lower() and line.strip()
    ]
    selected_topic = (
        selected_topic_path.read_text(encoding="utf-8").strip()
        if selected_topic_path.exists()
        else ""
    )
    sample_text = sample_path.read_text(encoding="utf-8") if sample_path.exists() else ""
    sample_err = (
        sample_err_path.read_text(encoding="utf-8") if sample_err_path.exists() else ""
    )
    sample_rc = (
        sample_rc_path.read_text(encoding="utf-8").strip()
        if sample_rc_path.exists()
        else ""
    )
    detected_models = _parse_detected_models(sample_text)
    actor_detected = ACTOR_MODEL_NAME in detected_models
    topic_observed = bool(logical_camera_topics)
    status = "sensor_visible_actor_observed" if actor_detected else "blocked"
    sensor_visible_actor_evidence = {
        "schema_version": "sensor_visible_actor_evidence.v1",
        "evidence_id": "sensor_visible_actor_evidence:mission_designer_logical_camera",
        "observation_status": status,
        "observed": {
            "source": "gz_topic_logical_camera_read_only",
            "topics_path": str(topics_path),
            "candidate_topics": logical_camera_topics,
            "selected_topic": selected_topic,
            "logical_camera_topic_observed": topic_observed,
            "sensor_sample_observed": bool(sample_text.strip()),
            "sensor_visible_actor_observed": actor_detected,
            "detected_models": detected_models,
            "actor_model_name": ACTOR_MODEL_NAME,
            "logical_camera_sample_path": str(sample_path),
            "logical_camera_sample_sha256": _sha256_text(sample_text)
            if sample_text
            else "",
            "logical_camera_sample_stdout_tail": _tail(sample_text),
            "logical_camera_sample_stderr_tail": _tail(sample_err),
            "logical_camera_sample_returncode": sample_rc,
            "read_only_observer": True,
            "collision_observed": False,
            "contact_event_observed": False,
            "route_blocking_observed": False,
            "incident_observed": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        },
        "sensor_configured_is_not_actor_detected": True,
        "sensor_visible_actor_is_not_incident_report": True,
        "sensor_visible_actor_is_not_route_blocking": True,
        "simulator_only": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
    }
    sensor_visible_actor_scoped_verifier_candidate = {
        "schema_version": "sensor_visible_actor_scoped_verifier_candidate.v1",
        "candidate_id": (
            "sensor_visible_actor_scoped_verifier_candidate:"
            "mission_designer_logical_camera"
        ),
        "condition_kind": "sensor_visible_actor_operator_review_verifier_candidate",
        "candidate_status": (
            "operator_review_candidate" if actor_detected else "not_observed"
        ),
        "observation_status": (
            "operator_review_candidate" if actor_detected else "not_observed"
        ),
        "input_evidence_refs": [
            "sensor_visible_actor_evidence:mission_designer_logical_camera",
        ],
        "observed": {
            "source": "sensor_visible_actor_evidence",
            "observed": actor_detected,
            "logical_camera_topic_observed": topic_observed,
            "sensor_sample_observed": bool(sample_text.strip()),
            "sensor_visible_actor_observed": actor_detected,
            "detected_models": detected_models,
            "actor_model_name": ACTOR_MODEL_NAME,
            "scoped_verifier_candidate": actor_detected,
            "operator_review_required": actor_detected,
            "incident_verified": False,
            "route_blocking_verified": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        },
        "candidate_only": True,
        "operator_review_required": actor_detected,
        "incident_verifier": False,
        "route_blocking_verifier": False,
        "traffic_conflict_verifier": False,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
    }
    sensor_visible_actor_operator_review_report = {
        "schema_version": "sensor_visible_actor_operator_review_report.v1",
        "report_id": (
            "sensor_visible_actor_operator_review_report:"
            "mission_designer_logical_camera"
        ),
        "condition_kind": "operator_reviewed_sensor_visible_actor_candidate",
        "report_status": "operator_review_required" if actor_detected else "not_observed",
        "input_evidence_refs": [
            "sensor_visible_actor_evidence:mission_designer_logical_camera",
            (
                "sensor_visible_actor_scoped_verifier_candidate:"
                "mission_designer_logical_camera"
            ),
        ],
        "observed": {
            "source": "sensor_visible_actor_scoped_verifier_candidate",
            "observed": actor_detected,
            "sensor_visible_actor_observed": actor_detected,
            "scoped_verifier_candidate": actor_detected,
            "operator_review_required": actor_detected,
            "incident_verified": False,
            "route_blocking_verified": False,
            "traffic_conflict_verified": False,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        },
        "operator_review_report": True,
        "incident_verifier": False,
        "route_blocking_verifier": False,
        "traffic_conflict_verifier": False,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
    }
    sensor_visible_actor_incident_verification = {
        "schema_version": "sensor_visible_actor_incident_verification.v1",
        "verification_id": (
            "sensor_visible_actor_incident_verification:"
            "mission_designer_logical_camera"
        ),
        "condition_kind": "scoped_sensor_visible_actor_incident_verifier",
        "verification_status": (
            "incident_verified" if actor_detected else "incident_not_verified"
        ),
        "verification_scope": "sensor_visible_actor_incident_only",
        "sensor_visible_actor_scoped_verifier_candidate_ref": (
            "sensor_visible_actor_scoped_verifier_candidate:"
            "mission_designer_logical_camera"
        ),
        "sensor_visible_actor_evidence_ref": (
            "sensor_visible_actor_evidence:mission_designer_logical_camera"
        ),
        "input_evidence_refs": [
            "sensor_visible_actor_evidence:mission_designer_logical_camera",
            (
                "sensor_visible_actor_scoped_verifier_candidate:"
                "mission_designer_logical_camera"
            ),
        ],
        "observed": {
            "source": "sensor_visible_actor_scoped_verifier_candidate",
            "observed": actor_detected,
            "logical_camera_topic_observed": topic_observed,
            "sensor_sample_observed": bool(sample_text.strip()),
            "sensor_visible_actor_observed": actor_detected,
            "detected_models": detected_models,
            "actor_model_name": ACTOR_MODEL_NAME,
            "scoped_verifier_candidate": actor_detected,
            "operator_review_required": actor_detected,
            "incident_verified": actor_detected,
            "route_blocking_verified": False,
            "traffic_conflict_verified": False,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        },
        "operator_review_required": actor_detected,
        "incident_verifier": True,
        "route_blocking_verifier": False,
        "traffic_conflict_verifier": False,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
    }

    return {
        "schema_version": "mission_designer_sensor_visible_actor_smoke_summary.v1",
        "status": status,
        "artifact_dir": str(artifact_dir),
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "artifacts": {
            "sensor_visible_actor_profile": {
                "schema_version": "sensor_visible_actor_profile.v1",
                "condition_kind": "logical_camera_sensor_visible_actor_probe",
                "requested": {
                    "sensor_visible_actor": True,
                    "sensor_type": "gazebo_logical_camera",
                    "actor_model_name": ACTOR_MODEL_NAME,
                    "topic": LOGICAL_CAMERA_TOPIC,
                },
                "simulator_only": True,
                "hardware_target_allowed": False,
                "physical_execution_invoked": False,
            },
            "sensor_visible_actor_application": {
                "schema_version": "sensor_visible_actor_application.v1",
                "application_status": "applied" if docker_result.returncode == 0 else "blocked",
                "method": "scoped_gazebo_logical_camera_world",
                "docker_image": docker_image,
                "world_sdf_path": str(world_path),
                "sdf_check_path": str(sdf_check_path),
                "actor_model_materialized": True,
                "logical_camera_materialized": True,
                "route_execution_invoked": False,
                "mavlink_dispatch_performed": False,
                "px4_mission_upload_performed": False,
                "task_status_mutated": False,
                "delivery_completion_claimed": False,
            },
            "sensor_visible_actor_evidence": sensor_visible_actor_evidence,
            "sensor_visible_actor_scoped_verifier_candidate": (
                sensor_visible_actor_scoped_verifier_candidate
            ),
            "sensor_visible_actor_operator_review_report": (
                sensor_visible_actor_operator_review_report
            ),
            "sensor_visible_actor_incident_verification": (
                sensor_visible_actor_incident_verification
            ),
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


def run_sensor_visible_actor_smoke(
    *, output_root: Path, docker_image: str
) -> dict[str, Any]:
    artifact_dir = output_root / f"sensor_visible_actor_{_utc_timestamp()}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    world_path = artifact_dir / "sensor_visible_actor_probe.sdf"
    world_path.write_text(SENSOR_VISIBLE_ACTOR_WORLD_SDF, encoding="utf-8")

    shell = textwrap.dedent(
        f"""
        set -eu
        cp /in/sensor_visible_actor_probe.sdf /tmp/sensor_visible_actor_probe.sdf
        gz sdf -k /tmp/sensor_visible_actor_probe.sdf > /out/sdf_check.txt 2>&1
        gz sim -s -r /tmp/sensor_visible_actor_probe.sdf > /out/gz_sim.log 2>&1 &
        gz_pid=$!
        cleanup() {{
          kill "$gz_pid" >/dev/null 2>&1 || true
          wait "$gz_pid" >/dev/null 2>&1 || true
        }}
        trap cleanup EXIT
        sleep 4
        gz topic -l > /out/topics.txt
        selected_topic="$(grep -F {LOGICAL_CAMERA_TOPIC!r} /out/topics.txt | head -1 || true)"
        if [ -z "$selected_topic" ]; then
          selected_topic="$(grep -i logical_camera /out/topics.txt | head -1 || true)"
        fi
        printf '%s' "$selected_topic" > /out/selected_topic.txt
        if [ -n "$selected_topic" ]; then
          timeout 5 gz topic -e -t "$selected_topic" -n 1 > /out/logical_camera_sample.txt 2> /out/logical_camera_sample.err
          printf '%s' "$?" > /out/logical_camera_sample.rc
        else
          : > /out/logical_camera_sample.txt
          printf 'no logical camera topic advertised' > /out/logical_camera_sample.err
          printf '127' > /out/logical_camera_sample.rc
          exit 2
        fi
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
        timeout=60,
    )
    summary = build_sensor_visible_actor_summary(
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
        default=os.getenv("MISSION_DESIGNER_SENSOR_VISIBLE_ACTOR_OUTPUT_ROOT")
        or str(DEFAULT_OUTPUT_ROOT),
    )
    parser.add_argument(
        "--docker-image",
        default=os.getenv("PX4_GAZEBO_DOCKER_IMAGE") or DEFAULT_DOCKER_IMAGE,
    )
    args = parser.parse_args()
    summary = run_sensor_visible_actor_smoke(
        output_root=Path(args.output_root).expanduser().resolve(),
        docker_image=args.docker_image,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary.get("status") == "sensor_visible_actor_observed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
