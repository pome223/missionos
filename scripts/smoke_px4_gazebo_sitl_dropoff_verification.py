#!/usr/bin/env python3
"""Runtime smoke for SITL flight-fact dropoff verification (#411)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from tempfile import TemporaryDirectory

from src.runtime.delivery_mission_contract import build_delivery_mission_contract
from src.runtime.px4_gazebo_bounded_simulation_runner import (
    build_px4_gazebo_bounded_simulation_run,
)
from src.runtime.px4_gazebo_mission_scenario_designer import (
    approve_px4_gazebo_mission_scenario_for_bounded_simulation,
    run_px4_gazebo_mission_scenario_designer,
)
from src.runtime.px4_gazebo_sitl_dropoff_verification import (
    PX4_GAZEBO_SITL_DROPOFF_VERIFICATION_SCHEMA_VERSION,
    attach_px4_gazebo_sitl_dropoff_verification,
    build_px4_gazebo_sitl_dropoff_flight_fact,
    build_px4_gazebo_sitl_dropoff_verification,
    build_px4_gazebo_sitl_payload_release_event,
    dropoff_evidence_from_sitl_verification,
)
from src.runtime.px4_gazebo_telemetry import (
    build_px4_gazebo_hil_review_gate_smoke,
    sanitize_px4_gazebo_telemetry_sample,
)
from src.runtime.simulated_delivery_episode import (
    build_simulated_delivery_episode_from_bounded_gazebo_run,
)
from src.runtime.task_store import TaskStore

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _contract():
    return build_delivery_mission_contract(
        mission_id="sitl-dropoff-verification-smoke",
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
        package_constraints={"package_id": "pkg-sitl-dropoff", "max_weight_kg": 1.2},
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
        now=NOW,
    )


def _bounded_artifacts():
    designed = run_px4_gazebo_mission_scenario_designer(
        prompt="低高度の配送地点に1kgの荷物を届ける",
        now=NOW,
    )
    approved = approve_px4_gazebo_mission_scenario_for_bounded_simulation(
        proposal=designed["scenario_proposal"],
        validation=designed["validation_result"],
        now=NOW,
    )
    request = approved["bounded_simulation_request"]
    telemetry = sanitize_px4_gazebo_telemetry_sample(
        {
            "sample_id": "sitl-dropoff-verification-smoke",
            "source": {
                "source_kind": "gz_sim_harmonic_stdout_log",
                "source_id": "sitl-dropoff-verification-smoke",
                "vehicle_id": "x500_0",
            },
            "captured_at": "2026-01-01T12:00:00Z",
            "telemetry": {
                "position": "0.14,0.10,0.08",
                "battery_percent": 88.0,
                "vehicle_health": "nominal",
                "weather_snapshot": "clear",
                "landing_zone_available": True,
            },
        }
    )
    hil_gate = build_px4_gazebo_hil_review_gate_smoke(
        telemetry,
        freshness_threshold_seconds=60.0,
        now=NOW,
    )
    telemetry_ref = f"px4_gazebo_sanitized_telemetry:{telemetry.telemetry_id}"
    hil_ref = f"hil_telemetry_review:{hil_gate['hil_telemetry_review']['review_id']}"
    gate_ref = f"autonomy_gate_result:{hil_gate['autonomy_gate_result']['gate_id']}"
    run = build_px4_gazebo_bounded_simulation_run(
        request=request,
        started_at=NOW,
        finished_at=NOW,
        max_duration_seconds=300,
        max_log_lines=260,
        observed_log_line_count=34,
        telemetry_captured_at=NOW,
        max_telemetry_age_seconds=300,
        telemetry_age_seconds=0.0,
        telemetry_refs=(telemetry_ref,),
        gate_ref=gate_ref,
        hil_review_ref=hil_ref,
        provenance={
            "world_name": "empty",
            "world_ref": "/tmp/empty.sdf",
            "world_sdf_path": "/tmp/empty.sdf",
            "network_mode": "none",
            "read_only_rootfs": True,
            "privileged": False,
            "cap_drop": ["ALL"],
        },
    )
    return request, telemetry, hil_gate, run


def _release_and_fact():
    release = build_px4_gazebo_sitl_payload_release_event(
        event_source="gazebo_gripper_detach_event",
        payload_id="pkg-sitl-dropoff",
        release_position_x_m=0.15,
        release_position_y_m=0.12,
        release_position_z_m=0.08,
        observed_at=NOW,
    )
    fact = build_px4_gazebo_sitl_dropoff_flight_fact(
        vehicle_id="x500_0",
        dropoff_zone_id="dropoff-pad-b",
        position_x_m=0.14,
        position_y_m=0.1,
        position_z_m=0.08,
        dropoff_target_x_m=0.0,
        dropoff_target_y_m=0.0,
        dropoff_target_altitude_m=0.0,
        mission_item_reached_observed=True,
        mission_item_reached_seq=2,
        mission_item_reached_at=NOW,
        payload_release_event=release,
        telemetry_ref="px4_gazebo_sitl_telemetry_sample:dropoff-smoke",
        sitl_mission_upload_receipt_ref=(
            "px4_gazebo_sitl_mission_upload_receipt:smoke"
        ),
        observed_at=NOW,
    )
    return release, fact


def main() -> int:
    contract = _contract()
    request, telemetry, hil_gate, run = _bounded_artifacts()
    release, fact = _release_and_fact()
    verification = build_px4_gazebo_sitl_dropoff_verification(
        delivery_mission_contract=contract,
        dropoff_flight_fact=fact,
        payload_release_event=release,
        now=NOW,
    )
    episode_artifacts = build_simulated_delivery_episode_from_bounded_gazebo_run(
        delivery_mission_contract=contract,
        bounded_simulation_request=request,
        bounded_simulation_run=run,
        sanitized_telemetry=telemetry,
        hil_telemetry_review=hil_gate["hil_telemetry_review"],
        autonomy_gate_result=hil_gate["autonomy_gate_result"],
        dropoff_evidence=dropoff_evidence_from_sitl_verification(verification),
        now=NOW,
    )
    episode = episode_artifacts["simulated_delivery_episode"]
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="control_supervisor",
            title="SITL dropoff verification smoke",
            status="running",
            artifacts={"existing": {"kept": True}},
        )
        attached = attach_px4_gazebo_sitl_dropoff_verification(
            task_id=task["task_id"],
            delivery_mission_contract=contract,
            dropoff_flight_fact=fact,
            payload_release_event=release,
            now=NOW,
            task_store_factory=lambda: store,
        )
        stored = store.get(task["task_id"])
    summary = {
        "schema_version": verification.schema_version,
        "verification_status": verification.status.value,
        "dropoff_verified": verification.dropoff_verified,
        "pose_within_dropoff_zone": verification.pose_within_dropoff_zone,
        "altitude_within_tolerance": verification.altitude_within_tolerance,
        "mission_item_reached": verification.mission_item_reached,
        "payload_release_observed": verification.payload_release_observed,
        "release_position_within_dropoff_zone": (
            verification.release_position_within_dropoff_zone
        ),
        "release_altitude_within_tolerance": (
            verification.release_altitude_within_tolerance
        ),
        "release_within_mission_item_time_window": (
            verification.release_within_mission_item_time_window
        ),
        "payload_release_observed_at": (
            verification.payload_release_observed_at.isoformat()
            if verification.payload_release_observed_at
            else None
        ),
        "episode_dropoff_verified": episode.dropoff_verified,
        "episode_phase_history": [phase.value for phase in episode.phase_history],
        "physical_execution_invoked": verification.physical_execution_invoked,
        "hardware_target_allowed": verification.hardware_target_allowed,
        "gazebo_entity_mutation_performed": (
            verification.gazebo_entity_mutation_performed
        ),
        "task_status": stored["status"] if stored else None,
        "existing_artifact_kept": bool(
            stored and stored["artifacts"]["existing"]["kept"]
        ),
        "attached_schema_version": attached["px4_gazebo_sitl_dropoff_verification"][
            "schema_version"
        ],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))
    assert (
        summary["schema_version"] == PX4_GAZEBO_SITL_DROPOFF_VERIFICATION_SCHEMA_VERSION
    )
    assert summary["verification_status"] == "verified"
    assert summary["dropoff_verified"] is True
    assert summary["episode_dropoff_verified"] is True
    assert "dropoff_verified" in summary["episode_phase_history"]
    assert summary["payload_release_observed"] is True
    assert summary["release_position_within_dropoff_zone"] is True
    assert summary["release_altitude_within_tolerance"] is True
    assert summary["release_within_mission_item_time_window"] is True
    assert summary["payload_release_observed_at"] == NOW.isoformat()
    assert summary["physical_execution_invoked"] is False
    assert summary["hardware_target_allowed"] is False
    assert summary["gazebo_entity_mutation_performed"] is False
    assert summary["task_status"] == "running"
    assert summary["existing_artifact_kept"] is True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
