#!/usr/bin/env python3
"""Runtime smoke for operator-supervised Gazebo delivery simulation control v0."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from src.runtime.delivery_mission_contract import build_delivery_mission_contract
from src.runtime.delivery_mission_gate import build_delivery_mission_gate_artifacts
from src.runtime.delivery_mission_policy_review import (
    build_delivery_mission_policy_review,
)
from src.runtime.gazebo_delivery_scenario import build_gazebo_delivery_scenario
from src.runtime.gazebo_delivery_simulation_control import (
    run_gazebo_delivery_simulation_control_v0_task,
)
from src.runtime.px4_gazebo_telemetry import (
    build_px4_gazebo_hil_review_gate_smoke,
    sanitize_px4_gazebo_telemetry_sample,
)
from src.runtime.task_store import TaskStore

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _contract():
    return build_delivery_mission_contract(
        mission_id="gazebo-delivery-sim-control-smoke",
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
        package_constraints={"package_id": "pkg-sim-control", "max_weight_kg": 1.2},
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


def _gate(contract, *, battery_percent: float = 88.0):
    telemetry = sanitize_px4_gazebo_telemetry_sample(
        {
            "sample_id": f"sim-control-smoke-{battery_percent}",
            "source": {
                "source_kind": "gz_sim_delivery_entity_state_pose",
                "source_id": "gz-sim-sim-control-smoke",
                "vehicle_id": "vehicle-sim-control-smoke",
            },
            "captured_at": "2026-01-01T12:00:00Z",
            "telemetry": {
                "position": "0.0,0.0,0.2",
                "battery_percent": battery_percent,
                "vehicle_health": "nominal",
                "weather_snapshot": "clear",
            },
        }
    )
    hil_review = build_px4_gazebo_hil_review_gate_smoke(
        telemetry,
        freshness_threshold_seconds=10.0,
        now=NOW,
    )["hil_telemetry_review"]
    policy = build_delivery_mission_policy_review(
        delivery_mission_contract=contract,
        sanitized_telemetry=telemetry,
        hil_telemetry_review=hil_review,
        now=NOW,
    )
    return build_delivery_mission_gate_artifacts(
        delivery_mission_contract=contract,
        delivery_mission_policy_review=policy,
        now=NOW,
    )["delivery_mission_gate_result"]


def main() -> int:
    contract = _contract()
    scenario = build_gazebo_delivery_scenario(
        delivery_mission_contract=contract,
        now=NOW,
    )
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        happy_task = store.create(
            kind="gazebo_delivery_simulation_control_v0",
            title="simulation control happy path smoke",
            status="running",
            artifacts={"existing": {"case_id": "happy", "kept": True}},
        )
        completed = run_gazebo_delivery_simulation_control_v0_task(
            happy_task["task_id"],
            delivery_mission_contract=contract,
            gazebo_delivery_scenario=scenario,
            delivery_mission_gate_result=_gate(contract),
            operator_approval_performed=True,
            now=NOW,
            task_store_factory=lambda: store,
        )

        missing_approval_task = store.create(
            kind="gazebo_delivery_simulation_control_v0",
            title="simulation control missing approval smoke",
            status="running",
            artifacts={"existing": {"case_id": "missing_approval", "kept": True}},
        )
        blocked = run_gazebo_delivery_simulation_control_v0_task(
            missing_approval_task["task_id"],
            delivery_mission_contract=contract,
            gazebo_delivery_scenario=scenario,
            delivery_mission_gate_result=_gate(contract),
            operator_approval_performed=False,
            now=NOW,
            task_store_factory=lambda: store,
        )

        blocked_gate_task = store.create(
            kind="gazebo_delivery_simulation_control_v0",
            title="simulation control blocked gate smoke",
            status="running",
            artifacts={"existing": {"case_id": "blocked_gate", "kept": True}},
        )
        blocked_gate = run_gazebo_delivery_simulation_control_v0_task(
            blocked_gate_task["task_id"],
            delivery_mission_contract=contract,
            gazebo_delivery_scenario=scenario,
            delivery_mission_gate_result=_gate(contract, battery_percent=20.0),
            operator_approval_performed=True,
            now=NOW,
            task_store_factory=lambda: store,
        )

        mismatched_contract_task = store.create(
            kind="gazebo_delivery_simulation_control_v0",
            title="simulation control mismatched gate contract smoke",
            status="running",
            artifacts={"existing": {"case_id": "mismatched_contract", "kept": True}},
        )
        mismatched_contract_gate = dict(_gate(contract))
        mismatched_contract_gate["delivery_mission_contract_id"] = (
            "delivery_mission_contract:other"
        )
        mismatched_contract = run_gazebo_delivery_simulation_control_v0_task(
            mismatched_contract_task["task_id"],
            delivery_mission_contract=contract,
            gazebo_delivery_scenario=scenario,
            delivery_mission_gate_result=mismatched_contract_gate,
            operator_approval_performed=True,
            now=NOW,
            task_store_factory=lambda: store,
        )

        mismatched_mission_task = store.create(
            kind="gazebo_delivery_simulation_control_v0",
            title="simulation control mismatched gate mission smoke",
            status="running",
            artifacts={"existing": {"case_id": "mismatched_mission", "kept": True}},
        )
        mismatched_mission_gate = dict(_gate(contract))
        mismatched_mission_gate["delivery_mission_id"] = "other-mission"
        mismatched_mission = run_gazebo_delivery_simulation_control_v0_task(
            mismatched_mission_task["task_id"],
            delivery_mission_contract=contract,
            gazebo_delivery_scenario=scenario,
            delivery_mission_gate_result=mismatched_mission_gate,
            operator_approval_performed=True,
            now=NOW,
            task_store_factory=lambda: store,
        )

    completed_audit = completed["artifacts"]["gazebo_delivery_simulation_control_audit"]
    blocked_audit = blocked["artifacts"]["gazebo_delivery_simulation_control_audit"]
    blocked_gate_audit = blocked_gate["artifacts"][
        "gazebo_delivery_simulation_control_audit"
    ]
    mismatched_contract_audit = mismatched_contract["artifacts"][
        "gazebo_delivery_simulation_control_audit"
    ]
    mismatched_mission_audit = mismatched_mission["artifacts"][
        "gazebo_delivery_simulation_control_audit"
    ]
    runner = completed["artifacts"]["simulated_delivery_runner_result"]
    summary = {
        "completed_task_status": completed["status"],
        "missing_approval_task_status": blocked["status"],
        "blocked_gate_task_status": blocked_gate["status"],
        "mismatched_contract_task_status": mismatched_contract["status"],
        "mismatched_mission_task_status": mismatched_mission["status"],
        "approval_schema": completed["artifacts"][
            "gazebo_delivery_simulation_approval"
        ]["schema_version"],
        "audit_schema": completed_audit["schema_version"],
        "requested_actions": completed_audit["requested_simulation_actions"],
        "sidecar_result_ref_count": len(completed_audit["sidecar_result_refs"]),
        "returned_artifact_ref_count": len(completed_audit["returned_artifact_refs"]),
        "runner_final_task_status": runner["final_task_status"],
        "missing_approval_blocked": (
            "simulation_operator_approval_missing" in blocked_audit["blocked_reasons"]
        ),
        "pre_gate_not_passed_blocked": (
            "pre_gate_not_passed" in blocked_gate_audit["blocked_reasons"]
        ),
        "battery_abort_recommended_blocked": (
            "battery_abort_recommended" in blocked_gate_audit["blocked_reasons"]
        ),
        "pre_gate_contract_mismatch_blocked": (
            "pre_gate_contract_mismatch" in mismatched_contract_audit["blocked_reasons"]
        ),
        "pre_gate_mission_mismatch_blocked": (
            "pre_gate_mission_mismatch" in mismatched_mission_audit["blocked_reasons"]
        ),
        "sidecar_sequence_skipped_on_missing_approval": (
            "gazebo_delivery_sidecar_v0_sequence" not in blocked["artifacts"]
        ),
        "sidecar_sequence_skipped_on_blocked_gate": (
            "gazebo_delivery_sidecar_v0_sequence" not in blocked_gate["artifacts"]
        ),
        "sidecar_sequence_skipped_on_mismatched_contract": (
            "gazebo_delivery_sidecar_v0_sequence"
            not in mismatched_contract["artifacts"]
        ),
        "sidecar_sequence_skipped_on_mismatched_mission": (
            "gazebo_delivery_sidecar_v0_sequence" not in mismatched_mission["artifacts"]
        ),
        "runner_result_skipped_on_missing_approval": (
            "simulated_delivery_runner_result" not in blocked["artifacts"]
        ),
        "runner_result_skipped_on_blocked_gate": (
            "simulated_delivery_runner_result" not in blocked_gate["artifacts"]
        ),
        "runner_result_skipped_on_mismatched_contract": (
            "simulated_delivery_runner_result" not in mismatched_contract["artifacts"]
        ),
        "runner_result_skipped_on_mismatched_mission": (
            "simulated_delivery_runner_result" not in mismatched_mission["artifacts"]
        ),
        "existing_artifacts_retained": (
            completed["artifacts"]["existing"]["kept"]
            and blocked["artifacts"]["existing"]["kept"]
            and blocked_gate["artifacts"]["existing"]["kept"]
            and mismatched_contract["artifacts"]["existing"]["kept"]
            and mismatched_mission["artifacts"]["existing"]["kept"]
        ),
        "approval_promotion_reuse_created": any(
            key in completed["artifacts"]
            or key in blocked["artifacts"]
            or key in blocked_gate["artifacts"]
            or key in mismatched_contract["artifacts"]
            or key in mismatched_mission["artifacts"]
            for key in {
                "approval",
                "promotion_package",
                "reuse_plan",
                "runtime_reuse",
            }
        ),
        "live_execution_allowed": completed_audit["live_execution_allowed"],
        "physical_execution_invoked": completed_audit["physical_execution_invoked"],
        "command_payload_allowed": completed_audit["command_payload_allowed"],
        "gazebo_entity_mutation_allowed": completed_audit[
            "gazebo_entity_mutation_allowed"
        ],
        "ros_dispatch_allowed": completed_audit["ros_dispatch_allowed"],
        "mavlink_dispatch_allowed": completed_audit["mavlink_dispatch_allowed"],
        "actuator_execution_allowed": completed_audit["actuator_execution_allowed"],
    }

    assert summary["completed_task_status"] == "completed"
    assert summary["missing_approval_task_status"] == "blocked"
    assert summary["blocked_gate_task_status"] == "blocked"
    assert summary["mismatched_contract_task_status"] == "blocked"
    assert summary["mismatched_mission_task_status"] == "blocked"
    assert summary["sidecar_result_ref_count"] == 5
    assert summary["runner_final_task_status"] == "completed"
    assert summary["missing_approval_blocked"] is True
    assert summary["pre_gate_not_passed_blocked"] is True
    assert summary["battery_abort_recommended_blocked"] is True
    assert summary["pre_gate_contract_mismatch_blocked"] is True
    assert summary["pre_gate_mission_mismatch_blocked"] is True
    assert summary["sidecar_sequence_skipped_on_missing_approval"] is True
    assert summary["sidecar_sequence_skipped_on_blocked_gate"] is True
    assert summary["sidecar_sequence_skipped_on_mismatched_contract"] is True
    assert summary["sidecar_sequence_skipped_on_mismatched_mission"] is True
    assert summary["runner_result_skipped_on_missing_approval"] is True
    assert summary["runner_result_skipped_on_blocked_gate"] is True
    assert summary["runner_result_skipped_on_mismatched_contract"] is True
    assert summary["runner_result_skipped_on_mismatched_mission"] is True
    assert summary["existing_artifacts_retained"] is True
    assert summary["approval_promotion_reuse_created"] is False
    assert summary["live_execution_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["command_payload_allowed"] is False
    assert summary["gazebo_entity_mutation_allowed"] is False
    assert summary["ros_dispatch_allowed"] is False
    assert summary["mavlink_dispatch_allowed"] is False
    assert summary["actuator_execution_allowed"] is False

    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
