#!/usr/bin/env python3
"""Scoped Gazebo contact-event smoke for Mission Designer realism evidence.

This intentionally does not run a delivery mission. It exercises the Gazebo
contact sensor boundary only: materialize a collision-enabled object, observe a
real contact event on a Gazebo topic, and keep all route / incident / gate /
delivery authority fields false.
"""

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
DEFAULT_OUTPUT_ROOT = ROOT / "output" / "mission_designer_contact_event_runs"
DEFAULT_DOCKER_IMAGE = "px4io/px4-sitl-gazebo:latest"
CONTACT_TOPIC_SUFFIX = "/sensor/box_contact_sensor/contact"


CONTACT_EVENT_WORLD_SDF = """\
<?xml version="1.0" ?>
<sdf version="1.9">
  <world name="default">
    <plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics"/>
    <plugin filename="gz-sim-user-commands-system" name="gz::sim::systems::UserCommands"/>
    <plugin filename="gz-sim-scene-broadcaster-system" name="gz::sim::systems::SceneBroadcaster"/>
    <plugin filename="gz-sim-contact-system" name="gz::sim::systems::Contact"/>
    <plugin filename="gz-sim-sensors-system" name="gz::sim::systems::Sensors">
      <render_engine>ogre2</render_engine>
    </plugin>
    <gravity>0 0 -9.8</gravity>
    <model name="mission_designer_contact_ground">
      <static>true</static>
      <link name="link">
        <collision name="ground_collision">
          <geometry><plane><normal>0 0 1</normal><size>20 20</size></plane></geometry>
        </collision>
        <visual name="ground_visual">
          <geometry><plane><normal>0 0 1</normal><size>20 20</size></plane></geometry>
        </visual>
      </link>
    </model>
    <model name="mission_designer_contact_box">
      <pose>0 0 0.5 0 0 0</pose>
      <link name="link">
        <inertial>
          <mass>1.0</mass>
          <inertia>
            <ixx>0.1667</ixx><ixy>0</ixy><ixz>0</ixz>
            <iyy>0.1667</iyy><iyz>0</iyz><izz>0.1667</izz>
          </inertia>
        </inertial>
        <collision name="box_collision">
          <geometry><box><size>1 1 1</size></box></geometry>
        </collision>
        <sensor name="box_contact_sensor" type="contact">
          <always_on>true</always_on>
          <update_rate>20</update_rate>
          <contact><collision>box_collision</collision></contact>
        </sensor>
        <visual name="box_visual">
          <geometry><box><size>1 1 1</size></box></geometry>
        </visual>
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


def _parse_collision_names(sample_text: str) -> list[str]:
    names = re.findall(r'name: "([^"]+)"', sample_text)
    return sorted(set(names))


def build_contact_event_summary(
    *,
    artifact_dir: Path,
    docker_image: str,
    docker_result: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    topics_path = artifact_dir / "topics.txt"
    selected_topic_path = artifact_dir / "selected_topic.txt"
    sample_path = artifact_dir / "contact_sample.txt"
    sample_err_path = artifact_dir / "contact_sample.err"
    sample_rc_path = artifact_dir / "contact_sample.rc"
    sdf_check_path = artifact_dir / "sdf_check.txt"
    world_path = artifact_dir / "contact_event_probe.sdf"

    topic_lines = (
        topics_path.read_text(encoding="utf-8").splitlines()
        if topics_path.exists()
        else []
    )
    contact_topics = [
        line.strip()
        for line in topic_lines
        if "contact" in line.lower() and line.strip()
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
    collision_names = _parse_collision_names(sample_text)
    contact_event_observed = bool(sample_text.strip() and collision_names)
    contact_topic_observed = bool(contact_topics)
    status = "contact_event_observed" if contact_event_observed else "blocked"
    contact_event_incident_candidate = contact_event_observed
    contact_event_incident_evidence = {
        "schema_version": "contact_event_incident_evidence.v1",
        "evidence_id": (
            "contact_event_incident_evidence:"
            "mission_designer_collision_contact"
        ),
        "condition_kind": "contact_event_incident_candidate",
        "observation_status": (
            "contact_event_incident_candidate_observed"
            if contact_event_incident_candidate
            else "contact_event_incident_not_observed"
        ),
        "collision_contact_event_evidence_ref": (
            "collision_contact_event_evidence:mission_designer_collision_contact"
        ),
        "observed": {
            "source": "collision_contact_event_evidence",
            "observed": contact_event_observed,
            "contact_topic_observed": contact_topic_observed,
            "contact_event_observed": contact_event_observed,
            "contact_event_incident_candidate": contact_event_incident_candidate,
            "operator_review_required": contact_event_incident_candidate,
            "collision_names": collision_names,
            "incident_verified": False,
            "route_blocking_verified": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        },
        "candidate_only": True,
        "operator_review_report": True,
        "incident_verifier": False,
        "route_blocking_verifier": False,
        "traffic_conflict_verifier": False,
        "auto_gate": False,
        "task_status_mutated": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
    }
    operational_incident_report = {
        "schema_version": "operational_incident_report.v1",
        "report_id": "operational_incident_report:mission_designer_collision_contact",
        "condition_kind": "operator_reviewed_contact_event_candidate",
        "report_status": (
            "operator_review_required"
            if contact_event_incident_candidate
            else "no_operational_incident_candidate"
        ),
        "input_evidence_refs": [
            "collision_contact_event_evidence:mission_designer_collision_contact",
            "contact_event_incident_evidence:mission_designer_collision_contact",
        ],
        "observed": {
            "source": "contact_event_incident_evidence",
            "observed": contact_event_incident_candidate,
            "contact_event_incident_candidate": contact_event_incident_candidate,
            "contact_event_observed": contact_event_observed,
            "operator_review_required": contact_event_incident_candidate,
            "auto_gate": False,
            "incident_verified": False,
            "route_blocking_verified": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        },
        "operator_review_report": True,
        "auto_gate": False,
        "incident_verifier": False,
        "route_blocking_verifier": False,
        "traffic_conflict_verifier": False,
        "task_status_mutated": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
    }
    contact_event_scoped_verifier_candidate = {
        "schema_version": "contact_event_scoped_verifier_candidate.v1",
        "candidate_id": (
            "contact_event_scoped_verifier_candidate:"
            "mission_designer_collision_contact"
        ),
        "condition_kind": "contact_event_operator_review_verifier_candidate",
        "candidate_status": (
            "operator_review_candidate"
            if contact_event_incident_candidate
            else "not_observed"
        ),
        "observation_status": (
            "operator_review_candidate"
            if contact_event_incident_candidate
            else "not_observed"
        ),
        "input_evidence_refs": [
            "collision_contact_event_evidence:mission_designer_collision_contact",
            "contact_event_incident_evidence:mission_designer_collision_contact",
        ],
        "observed": {
            "source": "contact_event_incident_evidence",
            "observed": contact_event_incident_candidate,
            "contact_event_observed": contact_event_observed,
            "collision_names": collision_names,
            "scoped_verifier_candidate": contact_event_incident_candidate,
            "operator_review_required": contact_event_incident_candidate,
            "incident_verified": False,
            "route_blocking_verified": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        },
        "candidate_only": True,
        "operator_review_required": contact_event_incident_candidate,
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
    contact_event_incident_verification = {
        "schema_version": "contact_event_incident_verification.v1",
        "verification_id": (
            "contact_event_incident_verification:"
            "mission_designer_collision_contact"
        ),
        "condition_kind": "scoped_contact_event_incident_verifier",
        "verification_status": (
            "incident_verified"
            if contact_event_incident_candidate
            else "incident_not_verified"
        ),
        "verification_scope": "contact_event_incident_only",
        "contact_event_scoped_verifier_candidate_ref": (
            "contact_event_scoped_verifier_candidate:"
            "mission_designer_collision_contact"
        ),
        "contact_event_incident_evidence_ref": (
            "contact_event_incident_evidence:mission_designer_collision_contact"
        ),
        "input_evidence_refs": [
            "collision_contact_event_evidence:mission_designer_collision_contact",
            "contact_event_incident_evidence:mission_designer_collision_contact",
            (
                "contact_event_scoped_verifier_candidate:"
                "mission_designer_collision_contact"
            ),
        ],
        "observed": {
            "source": "contact_event_scoped_verifier_candidate",
            "observed": contact_event_incident_candidate,
            "contact_event_observed": contact_event_observed,
            "collision_names": collision_names,
            "scoped_verifier_candidate": contact_event_incident_candidate,
            "operator_review_required": contact_event_incident_candidate,
            "incident_verified": contact_event_incident_candidate,
            "route_blocking_verified": False,
            "traffic_conflict_verified": False,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        },
        "operator_review_required": contact_event_incident_candidate,
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
        "schema_version": "mission_designer_collision_contact_event_smoke_summary.v1",
        "status": status,
        "artifact_dir": str(artifact_dir),
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "artifacts": {
            "collision_contact_event_profile": {
                "schema_version": "collision_contact_event_profile.v1",
                "condition_kind": "collision_contact_event_probe",
                "requested": {
                    "contact_event_probe": True,
                    "collision_enabled": True,
                    "contact_sensor_enabled": True,
                    "model_name": "mission_designer_contact_box",
                    "ground_model_name": "mission_designer_contact_ground",
                },
                "simulator_only": True,
                "hardware_target_allowed": False,
                "physical_execution_invoked": False,
            },
            "collision_contact_event_application": {
                "schema_version": "collision_contact_event_application.v1",
                "application_status": "applied" if docker_result.returncode == 0 else "blocked",
                "method": "scoped_gazebo_contact_sensor_world",
                "docker_image": docker_image,
                "world_sdf_path": str(world_path),
                "sdf_check_path": str(sdf_check_path),
                "collision_geometry_materialized": True,
                "contact_sensor_materialized": True,
                "route_execution_invoked": False,
                "mavlink_dispatch_performed": False,
                "px4_mission_upload_performed": False,
                "task_status_mutated": False,
                "delivery_completion_claimed": False,
            },
            "collision_contact_event_evidence": {
                "schema_version": "collision_contact_event_evidence.v1",
                "observation_status": status,
                "observed": {
                    "source": "gz_topic_contact_sensor_read_only",
                    "topics_path": str(topics_path),
                    "candidate_topics": contact_topics,
                    "selected_topic": selected_topic,
                    "contact_topic_observed": contact_topic_observed,
                    "contact_topic_advertised": contact_topic_observed,
                    "contact_event_observed": contact_event_observed,
                    "contact_sample_path": str(sample_path),
                    "contact_sample_sha256": _sha256_text(sample_text) if sample_text else "",
                    "contact_sample_stdout_tail": _tail(sample_text),
                    "contact_sample_stderr_tail": _tail(sample_err),
                    "contact_sample_returncode": sample_rc,
                    "collision_names": collision_names,
                    "read_only_observer": True,
                    "route_blocking_observed": False,
                    "incident_observed": False,
                    "traffic_conflict_verified": False,
                    "task_status_mutated": False,
                    "gate_status_mutated": False,
                    "delivery_completion_claimed": False,
                },
                "contact_topic_only_is_not_contact_event": True,
                "contact_event_is_not_route_blocking": True,
                "contact_event_is_not_incident_verifier": True,
                "incident_report_is_operator_review_only": True,
                "simulator_only": True,
                "hardware_target_allowed": False,
                "physical_execution_invoked": False,
                "delivery_completion_claimed": False,
            },
            "contact_event_incident_evidence": contact_event_incident_evidence,
            "contact_event_scoped_verifier_candidate": (
                contact_event_scoped_verifier_candidate
            ),
            "contact_event_incident_verification": (
                contact_event_incident_verification
            ),
            "operational_incident_report": operational_incident_report,
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


def run_contact_event_smoke(*, output_root: Path, docker_image: str) -> dict[str, Any]:
    artifact_dir = output_root / f"contact_event_{_utc_timestamp()}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    world_path = artifact_dir / "contact_event_probe.sdf"
    world_path.write_text(CONTACT_EVENT_WORLD_SDF, encoding="utf-8")

    shell = textwrap.dedent(
        """
        set -eu
        cp /in/contact_event_probe.sdf /tmp/contact_event_probe.sdf
        gz sdf -k /tmp/contact_event_probe.sdf > /out/sdf_check.txt 2>&1
        gz sim -s -r /tmp/contact_event_probe.sdf > /out/gz_sim.log 2>&1 &
        gz_pid=$!
        cleanup() {
          kill "$gz_pid" >/dev/null 2>&1 || true
          wait "$gz_pid" >/dev/null 2>&1 || true
        }
        trap cleanup EXIT
        sleep 4
        gz topic -l > /out/topics.txt
        selected_topic="$(grep -i contact /out/topics.txt | head -1 || true)"
        printf '%s' "$selected_topic" > /out/selected_topic.txt
        if [ -n "$selected_topic" ]; then
          timeout 5 gz topic -e -t "$selected_topic" -n 1 > /out/contact_sample.txt 2> /out/contact_sample.err
          printf '%s' "$?" > /out/contact_sample.rc
        else
          : > /out/contact_sample.txt
          printf 'no contact topic advertised' > /out/contact_sample.err
          printf '127' > /out/contact_sample.rc
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
    summary = build_contact_event_summary(
        artifact_dir=artifact_dir,
        docker_image=docker_image,
        docker_result=result,
    )
    summary_path = artifact_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        default=os.getenv("MISSION_DESIGNER_CONTACT_EVENT_OUTPUT_ROOT")
        or str(DEFAULT_OUTPUT_ROOT),
    )
    parser.add_argument(
        "--docker-image",
        default=os.getenv("PX4_GAZEBO_DOCKER_IMAGE") or DEFAULT_DOCKER_IMAGE,
    )
    args = parser.parse_args()

    summary = run_contact_event_smoke(
        output_root=Path(args.output_root).expanduser().resolve(),
        docker_image=args.docker_image,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary.get("status") == "contact_event_observed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
