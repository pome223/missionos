#!/usr/bin/env python3
"""Opt-in same-session epic-exit smoke for the PX4/Gazebo SITL delivery chain.

This smoke composes the already-reviewed runtime boundaries:

- real PX4/Gazebo SITL mission upload (#410)
- real PX4/Gazebo horizontal flight evidence
- Gazebo detachable-joint payload release evidence
- SITL dropoff verification from observed flight facts

It records a single same-session summary and artifact manifest for PR review.
Payload release is observed from the Gazebo detachable-joint payload model; no
synthetic release event is passed to the dropoff verifier.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from src.runtime.delivery_mission_contract import build_delivery_mission_contract
from src.runtime.px4_gazebo_sitl_dropoff_verification import (
    build_px4_gazebo_sitl_dropoff_flight_fact,
    build_px4_gazebo_sitl_dropoff_verification,
    build_px4_gazebo_sitl_payload_release_event,
)
from src.runtime.px4_gazebo_sitl_e2e_delivery_smoke import (
    PX4_GAZEBO_SITL_E2E_DELIVERY_EPIC_EXIT_RESULT_SCHEMA_VERSION,
    build_px4_gazebo_sitl_e2e_delivery_epic_exit_result,
)

OPT_IN_ENV = "RUN_PX4_GAZEBO_SITL_E2E_DELIVERY_SMOKE"
ROOT_DIR = Path(__file__).resolve().parents[1]
ARTIFACT_ROOT_ENV = "PX4_GAZEBO_SITL_E2E_ARTIFACT_ROOT"
PROMPT = "標高3000mの山小屋に5kgの荷物を届けて"


def _artifact_root() -> Path:
    root = Path(os.getenv(ARTIFACT_ROOT_ENV, "artifacts/px4_gazebo_sitl_e2e"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _new_run_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = _artifact_root()
    candidate = root / f"sitl_e2e_delivery_{stamp}"
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = root / f"sitl_e2e_delivery_{stamp}_{suffix}"
    candidate.mkdir(parents=True)
    return candidate


def _run_script(
    script: str,
    *,
    env: dict[str, str] | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    merged_env = {**os.environ, **(env or {})}
    return subprocess.run(
        [sys.executable, script],
        cwd=ROOT_DIR,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=merged_env,
        check=False,
    )


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _run_horizontal_flight_smoke(run_dir: Path) -> dict[str, Any]:
    horizontal_root = run_dir / "horizontal_route"
    horizontal_root.mkdir(parents=True, exist_ok=True)
    before = set(horizontal_root.glob("horizontal_route_*"))
    result = _run_script(
        "scripts/smoke_px4_gazebo_horizontal_route_delivery.py",
        env={
            "RUN_PX4_GAZEBO_HORIZONTAL_ROUTE_SMOKE": "1",
            "PX4_GAZEBO_HORIZONTAL_ROUTE_PREUPLOAD_MISSION": "1",
            "PX4_GAZEBO_HORIZONTAL_ROUTE_SKIP_EMERGENCY_MAVLINK": "1",
            "PX4_GAZEBO_HORIZONTAL_ROUTE_PAYLOAD_RELEASE_MODEL": "1",
            "PX4_GAZEBO_HORIZONTAL_ROUTE_ARTIFACT_ROOT": str(horizontal_root),
        },
        timeout=360,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "PX4/Gazebo horizontal flight smoke failed:\n"
            + result.stdout[-3000:]
            + result.stderr[-3000:]
        )
    after = set(horizontal_root.glob("horizontal_route_*"))
    created = sorted(after - before, key=lambda path: path.stat().st_mtime)
    if not created:
        created = sorted(
            horizontal_root.glob("horizontal_route_*"),
            key=lambda path: path.stat().st_mtime,
        )
    if not created:
        raise RuntimeError("horizontal route smoke did not create an artifact dir")
    artifact_dir = created[-1]
    summary = json.loads((artifact_dir / "summary.json").read_text(encoding="utf-8"))
    summary["artifact_dir"] = str(artifact_dir)
    summary["px4_docker_log_path"] = str(artifact_dir / "px4_docker.log")
    summary["pose_samples_path"] = str(artifact_dir / "pose_samples.jsonl")
    summary["mission_artifacts_path"] = str(artifact_dir / "mission_artifacts.json")
    return summary


def _contract():
    return build_delivery_mission_contract(
        mission_id="sitl-e2e-delivery-epic-exit",
        pickup_location={
            "location_id": "pickup-pad-a",
            "latitude": 35.681236,
            "longitude": 139.767125,
        },
        dropoff_location={
            "location_id": "dropoff-pad-b",
            "latitude": 35.689487,
            "longitude": 139.691706,
        },
        delivery_window={
            "earliest_pickup_at": "2026-01-01T12:00:00Z",
            "latest_dropoff_at": "2026-01-01T12:30:00Z",
        },
        package_constraints={"package_id": "pkg-sitl-dropoff", "max_weight_kg": 5.0},
        weather_constraints={
            "max_wind_speed_mps": 6.0,
            "max_precipitation_mm_per_hour": 0.0,
            "min_visibility_m": 1500.0,
        },
        battery_policy={
            "minimum_takeoff_percent": 80,
            "return_to_home_percent": 35,
            "reserve_landing_percent": 25,
        },
        landing_zone_policy={
            "min_clear_radius_m": 3.0,
            "max_slope_degrees": 5.0,
            "accepted_surface_kinds": ["marked_pad"],
        },
        telemetry_requirements={
            "required_measurements": [
                "position",
                "battery_percent",
                "vehicle_health",
                "weather_snapshot",
            ],
            "max_freshness_seconds": 2.0,
        },
    )


def _dropoff_verification_from_horizontal(horizontal: dict[str, Any]):
    release_observed_at = datetime.fromisoformat(
        horizontal["payload_release_observed_at"].replace("Z", "+00:00")
    )
    release = build_px4_gazebo_sitl_payload_release_event(
        event_source="gazebo_detachable_joint_detach_event",
        payload_id="pkg-sitl-dropoff",
        release_position_x_m=float(horizontal["payload_release_position_x_m"]),
        release_position_y_m=float(horizontal["payload_release_position_y_m"]),
        release_position_z_m=float(horizontal["payload_release_position_z_m"]),
        observed_at=release_observed_at,
        metadata={
            "observed_from": "gazebo_detachable_joint_payload_model",
            "detach_topic": horizontal["payload_release_summary"][
                "payload_detach_topic"
            ],
            "pose_topic": "/world/default/pose/info",
        },
    )
    fact = build_px4_gazebo_sitl_dropoff_flight_fact(
        vehicle_id="x500_0",
        dropoff_zone_id="dropoff-pad-b",
        position_x_m=float(horizontal["completed_pose_xy_m"][0]),
        position_y_m=float(horizontal["completed_pose_xy_m"][1]),
        position_z_m=float(horizontal["completed_pose_z_m"]),
        dropoff_target_x_m=float(horizontal["route_target_x_m"]),
        dropoff_target_y_m=float(horizontal["route_target_y_m"]),
        dropoff_target_altitude_m=0.0,
        mission_item_reached_observed=True,
        mission_item_reached_seq=2,
        mission_item_reached_at=release_observed_at,
        payload_release_event=release,
        telemetry_ref=f"sitl_pose_trace:{horizontal['pose_samples_path']}",
        sitl_mission_upload_receipt_ref="px4_gazebo_sitl_mission_upload_receipt:same-session-smoke",
        observed_at=release_observed_at,
        metadata={"source": "same_session_horizontal_route_smoke"},
    )
    verification = build_px4_gazebo_sitl_dropoff_verification(
        delivery_mission_contract=_contract(),
        dropoff_flight_fact=fact,
        payload_release_event=release,
        dropoff_zone_radius_m=1.0,
        altitude_tolerance_m=0.5,
        release_time_window_seconds=5.0,
        expected_mission_item_seq=2,
        now=release_observed_at,
    )
    return release, fact, verification


def main() -> int:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(f"Set {OPT_IN_ENV}=1 to run the SITL E2E delivery smoke.")
    run_dir = _new_run_dir()

    horizontal = _run_horizontal_flight_smoke(run_dir)
    release, flight_fact, verification = _dropoff_verification_from_horizontal(
        horizontal
    )
    artifact_manifest = {
        "run_dir": str(run_dir),
        "sitl_telemetry_log_path": horizontal.get("px4_docker_log_path", ""),
        "gazebo_pose_trace_path": horizontal.get("pose_samples_path", ""),
        "horizontal_route_artifact_dir": horizontal.get("artifact_dir", ""),
        "gazebo_viewer_recording_path": "",
        "gazebo_viewer_recording_note": (
            "headless smoke records PX4/Gazebo logs and pose trace; viewer video "
            "is not generated in this environment"
        ),
        "payload_release_event_ref": (
            f"px4_gazebo_sitl_payload_release_event:{release.event_id}"
        ),
        "dropoff_flight_fact_ref": (
            f"px4_gazebo_sitl_dropoff_flight_fact:{flight_fact.fact_id}"
        ),
        "dropoff_verification_ref": (
            f"px4_gazebo_sitl_dropoff_verification:{verification.verification_id}"
        ),
    }
    result = build_px4_gazebo_sitl_e2e_delivery_epic_exit_result(
        prompt=PROMPT,
        horizontal_summary=horizontal,
        payload_release_event_ref=(
            f"px4_gazebo_sitl_payload_release_event:{release.event_id}"
        ),
        dropoff_verification_ref=(
            f"px4_gazebo_sitl_dropoff_verification:{verification.verification_id}"
        ),
        artifact_manifest=artifact_manifest,
    )
    summary = {
        "schema_version": result.schema_version,
        "result_id": result.result_id,
        "prompt": PROMPT,
        "epic_issue": "#407",
        "exit_issue": "#413",
        "payload_release_issue": "#423",
        "result_status": result.result_status,
        "artifact_manifest": artifact_manifest,
        "actual_sitl_mission_upload_observed": result.mission_upload_observed,
        "mission_ack_observed": result.mission_ack_observed,
        "mission_ack_type": result.mission_ack_type,
        "mission_request_sequences": list(result.mission_request_sequences),
        "actual_px4_gazebo_horizontal_flight_observed": horizontal[
            "actual_px4_gazebo_horizontal_smoke_observed"
        ],
        "actual_takeoff_observed": result.actual_takeoff_observed,
        "actual_dropoff_region_reached": result.actual_dropoff_region_reached,
        "actual_land_observed": result.actual_land_observed,
        "payload_release_observed": result.payload_release_observed,
        "payload_release_verified": result.payload_release_verified,
        "payload_release_event_source": result.payload_release_event_source,
        "payload_release_event_ref": result.payload_release_event_ref,
        "dropoff_verification_ref": result.dropoff_verification_ref,
        "dropoff_verification_status": verification.status.value,
        "dropoff_verified": verification.dropoff_verified,
        "release_position_within_dropoff_zone": (
            verification.release_position_within_dropoff_zone
        ),
        "release_altitude_within_tolerance": (
            verification.release_altitude_within_tolerance
        ),
        "release_within_mission_item_time_window": (
            verification.release_within_mission_item_time_window
        ),
        "epic_exit_complete": result.epic_exit_complete,
        "blocked_reasons": list(result.blocked_reasons),
        "invariants": {
            "external_dispatch_performed": result.external_dispatch_performed,
            "external_dispatch_scope": result.external_dispatch_scope,
            "mavlink_dispatch_performed": result.mavlink_dispatch_performed,
            "px4_mission_upload_performed": result.px4_mission_upload_performed,
            "gazebo_simulator_command_performed": (
                result.gazebo_simulator_command_performed
            ),
            "gazebo_detachable_joint_release_performed": (
                result.gazebo_detachable_joint_release_performed
            ),
            "gazebo_detachable_joint_release_observed": (
                result.gazebo_detachable_joint_release_observed
            ),
            "gazebo_entity_mutation_performed": (
                result.gazebo_entity_mutation_performed
            ),
            "hardware_target_allowed": result.hardware_target_allowed,
            "physical_execution_invoked": result.physical_execution_invoked,
        },
        "executed_in_same_sitl_session": result.executed_in_same_sitl_session,
        "same_session_note": (
            "mission upload and horizontal flight evidence are collected from the "
            "same PX4/Gazebo SITL container session; payload release is observed "
            "from the Gazebo detachable-joint payload model and verified by the "
            "SITL dropoff verifier"
        ),
    }
    _write_json(run_dir / "summary.json", summary)
    _write_json(run_dir / "e2e_epic_exit_result.json", result.model_dump(mode="json"))
    _write_json(run_dir / "payload_release_event.json", release.model_dump(mode="json"))
    _write_json(
        run_dir / "dropoff_flight_fact.json", flight_fact.model_dump(mode="json")
    )
    _write_json(
        run_dir / "dropoff_verification.json", verification.model_dump(mode="json")
    )
    _write_json(run_dir / "horizontal_flight_summary.json", horizontal)

    print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))

    assert summary["schema_version"] == (
        PX4_GAZEBO_SITL_E2E_DELIVERY_EPIC_EXIT_RESULT_SCHEMA_VERSION
    )
    assert summary["actual_sitl_mission_upload_observed"] is True
    assert summary["mission_ack_observed"] is True
    assert summary["mission_ack_type"] == 0
    assert summary["mission_request_sequences"] == [0, 1, 2, 3]
    assert summary["actual_px4_gazebo_horizontal_flight_observed"] is True
    assert summary["actual_takeoff_observed"] is True
    assert summary["actual_dropoff_region_reached"] is True
    assert summary["actual_land_observed"] is True
    assert summary["payload_release_observed"] is True
    assert summary["payload_release_verified"] is True
    assert (
        summary["payload_release_event_source"]
        == "gazebo_detachable_joint_detach_event"
    )
    assert summary["dropoff_verification_status"] == "verified"
    assert summary["dropoff_verified"] is True
    assert summary["release_position_within_dropoff_zone"] is True
    assert summary["release_altitude_within_tolerance"] is True
    assert summary["release_within_mission_item_time_window"] is True
    assert summary["epic_exit_complete"] is True
    assert summary["blocked_reasons"] == []
    assert summary["invariants"]["external_dispatch_performed"] is True
    assert (
        summary["invariants"]["external_dispatch_scope"]
        == "same_session_sitl_mission_upload_and_detachable_joint_release"
    )
    assert summary["invariants"]["gazebo_detachable_joint_release_performed"] is True
    assert summary["invariants"]["gazebo_detachable_joint_release_observed"] is True
    assert summary["invariants"]["gazebo_entity_mutation_performed"] is False
    assert summary["invariants"]["hardware_target_allowed"] is False
    assert summary["invariants"]["physical_execution_invoked"] is False
    assert Path(summary["artifact_manifest"]["sitl_telemetry_log_path"]).exists()
    assert Path(summary["artifact_manifest"]["gazebo_pose_trace_path"]).exists()
    assert summary["executed_in_same_sitl_session"] is True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
