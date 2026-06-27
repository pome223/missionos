#!/usr/bin/env python3
"""Runtime smoke for Gazebo delivery entity-state failure hardening.

The smoke exercises the persistence boundary introduced for Gazebo
entity-state delivery failures without starting Docker. It verifies that
invalid Gazebo delivery observations are stored only as debug diagnostics and
that runner v0 terminally blocks unsafe delivery telemetry with
machine-readable reasons.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from src.runtime.delivery_mission_contract import build_delivery_mission_contract
from src.runtime.delivery_progress_review import (
    DELIVERY_PROGRESS_BUCKET_ROUTE_GEOFENCE_VIOLATION,
)
from src.runtime.gazebo_delivery_scenario import build_gazebo_delivery_scenario
from src.runtime.gz_sim_log_collector import (
    GAZEBO_DELIVERY_OBSERVATION_DIAGNOSTICS_SCHEMA_VERSION,
    attach_gazebo_delivery_observation_diagnostics_artifact,
    collect_gz_sim_delivery_entity_state_sanitized,
)
from src.runtime.simulated_delivery_runner import run_simulated_delivery_task_v0
from src.runtime.task_store import TaskStore

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _base_gz_sim_state_world_log() -> str:
    return "\n".join(
        [
            "Gazebo Sim Server v8.0.0",
            "Loading SDF world file[/worlds/delivery_state_driven.sdf]",
            "Serving full state on [/world/delivery_state_driven/state]",
        ]
    )


def _pose_text(x: float, *, entity_name: str = "delivery_vehicle_state") -> str:
    return f"""
pose {{
  name: "{entity_name}"
  id: 8
  position {{
    x: {x}
    y: 0.0
    z: 0.2
  }}
  orientation {{ w: 1 }}
}}
"""


def _completed_pose_samples() -> list[str]:
    return [_pose_text(0.0), _pose_text(6.0), _pose_text(24.2), _pose_text(25.1)]


def _contract():
    return build_delivery_mission_contract(
        mission_id="gazebo-delivery-failure-hardening-smoke",
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
        package_constraints={
            "package_id": "pkg-failure-hardening-smoke",
            "max_weight_kg": 1.2,
        },
        geofence_constraints={"allowed_regions": ["sim-delivery-corridor"]},
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


def _assert_no_runtime_artifacts(artifacts: dict) -> None:
    forbidden = {
        "px4_gazebo_sanitized_telemetry",
        "hil_telemetry_envelope",
        "hil_telemetry_evidence",
        "hil_telemetry_review",
        "delivery_mission_gate_result",
        "simulated_delivery_runner_result",
        "approval",
        "promotion_package",
        "reuse_plan",
        "runtime_reuse",
    }
    unexpected = sorted(forbidden.intersection(artifacts))
    if unexpected:
        raise AssertionError(f"unexpected runtime artifacts persisted: {unexpected}")


def main() -> int:
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")

        diagnostics_task = store.create(
            kind="simulated_delivery_runner",
            title="Gazebo delivery observation diagnostics smoke",
            status="running",
            artifacts={"existing": {"case_id": "no_pose_topic_output", "kept": True}},
        )
        diagnostics = attach_gazebo_delivery_observation_diagnostics_artifact(
            diagnostics_task["task_id"],
            _base_gz_sim_state_world_log(),
            [],
            captured_at=NOW,
            task_store_factory=lambda: store,
        )
        diagnostics_updated = store.get(diagnostics_task["task_id"])
        assert diagnostics_updated is not None
        assert diagnostics_updated["status"] == "running"
        assert diagnostics_updated["artifacts"]["existing"]["kept"] is True
        _assert_no_runtime_artifacts(diagnostics_updated["artifacts"])

        runtime_failure_reasons: list[str] = []
        for reason, message in (
            ("collector_timeout", "pose topic collection timed out"),
            (
                "container_exited_early",
                "container exited before pose topic became available",
            ),
        ):
            failure_task = store.create(
                kind="simulated_delivery_runner",
                title=f"Gazebo delivery observation {reason} smoke",
                status="running",
                artifacts={"existing": {"case_id": reason, "kept": True}},
            )
            runtime_failure = attach_gazebo_delivery_observation_diagnostics_artifact(
                failure_task["task_id"],
                _base_gz_sim_state_world_log(),
                [],
                error_message=message,
                captured_at=NOW,
                reason_override=reason,
                task_store_factory=lambda: store,
            )
            failure_updated = store.get(failure_task["task_id"])
            assert failure_updated is not None
            assert failure_updated["status"] == "running"
            assert failure_updated["artifacts"]["existing"]["kept"] is True
            _assert_no_runtime_artifacts(failure_updated["artifacts"])
            assert runtime_failure["reason"] == reason
            assert runtime_failure["debug_only"] is True
            assert runtime_failure["hil_artifacts_persisted"] is False
            assert runtime_failure["gate_artifacts_persisted"] is False
            assert runtime_failure["runner_artifacts_persisted"] is False
            runtime_failure_reasons.append(runtime_failure["reason"])

        contract = _contract()
        scenario = build_gazebo_delivery_scenario(
            delivery_mission_contract=contract,
            now=NOW,
        )
        telemetry = collect_gz_sim_delivery_entity_state_sanitized(
            _base_gz_sim_state_world_log(),
            _completed_pose_samples(),
            captured_at=NOW,
        ).model_dump(mode="json")
        telemetry["measurements"] = dict(telemetry["measurements"])
        telemetry["measurements"]["route_geofence_violation"] = True

        runner_task = store.create(
            kind="simulated_delivery_runner",
            title="Gazebo delivery blocked runner smoke",
            status="running",
            artifacts={"existing": {"case_id": "route_geofence_violation", "kept": True}},
        )
        blocked = run_simulated_delivery_task_v0(
            runner_task["task_id"],
            delivery_mission_contract=contract,
            gazebo_delivery_scenario=scenario,
            sanitized_telemetry=telemetry,
            now=NOW,
            task_store_factory=lambda: store,
        )
        runner_result = blocked["artifacts"]["simulated_delivery_runner_result"]

    summary = {
        "diagnostics_schema_version": diagnostics["schema_version"],
        "diagnostics_reason": diagnostics["reason"],
        "diagnostics_task_status": diagnostics_updated["status"],
        "diagnostics_existing_artifact_kept": diagnostics_updated["artifacts"][
            "existing"
        ]["kept"],
        "diagnostics_hil_artifacts_persisted": diagnostics["hil_artifacts_persisted"],
        "diagnostics_gate_artifacts_persisted": diagnostics["gate_artifacts_persisted"],
        "diagnostics_runner_artifacts_persisted": diagnostics[
            "runner_artifacts_persisted"
        ],
        "runtime_failure_diagnostics_reasons": sorted(runtime_failure_reasons),
        "collector_timeout_recorded": "collector_timeout"
        in runtime_failure_reasons,
        "container_exited_early_recorded": "container_exited_early"
        in runtime_failure_reasons,
        "blocked_task_status": blocked["status"],
        "runner_final_task_status": runner_result["final_task_status"],
        "blocked_reasons": runner_result["blocked_reasons"],
        "route_geofence_violation_blocked": (
            DELIVERY_PROGRESS_BUCKET_ROUTE_GEOFENCE_VIOLATION
            in runner_result["blocked_reasons"]
        ),
        "recovery_primary_action": blocked["artifacts"]["delivery_recovery_decision"][
            "primary_action"
        ],
        "runner_existing_artifact_kept": blocked["artifacts"]["existing"]["kept"],
        "approval_promotion_reuse_created": any(
            key in blocked["artifacts"]
            for key in {
                "approval",
                "promotion_package",
                "reuse_plan",
                "runtime_reuse",
            }
        ),
        "live_execution_allowed": runner_result["live_execution_allowed"],
        "physical_execution_invoked": runner_result["physical_execution_invoked"],
        "command_payload_allowed": runner_result["command_payload_allowed"],
        "ros_dispatch_allowed": runner_result["ros_dispatch_allowed"],
        "mavlink_dispatch_allowed": runner_result["mavlink_dispatch_allowed"],
        "actuator_execution_allowed": runner_result["actuator_execution_allowed"],
    }

    assert summary["diagnostics_schema_version"] == (
        GAZEBO_DELIVERY_OBSERVATION_DIAGNOSTICS_SCHEMA_VERSION
    )
    assert summary["diagnostics_reason"] == "no_pose_topic_output"
    assert summary["diagnostics_task_status"] == "running"
    assert summary["diagnostics_existing_artifact_kept"] is True
    assert summary["diagnostics_hil_artifacts_persisted"] is False
    assert summary["diagnostics_gate_artifacts_persisted"] is False
    assert summary["diagnostics_runner_artifacts_persisted"] is False
    assert summary["collector_timeout_recorded"] is True
    assert summary["container_exited_early_recorded"] is True
    assert summary["blocked_task_status"] == "blocked"
    assert summary["runner_final_task_status"] == "blocked"
    assert summary["route_geofence_violation_blocked"] is True
    assert summary["recovery_primary_action"] == "operator_escalation_required"
    assert summary["runner_existing_artifact_kept"] is True
    assert summary["approval_promotion_reuse_created"] is False
    assert summary["live_execution_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["command_payload_allowed"] is False
    assert summary["ros_dispatch_allowed"] is False
    assert summary["mavlink_dispatch_allowed"] is False
    assert summary["actuator_execution_allowed"] is False

    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
