#!/usr/bin/env python3
"""Smoke simulator-only delivery command proposal/approval/receipt flow."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import tempfile

from src.runtime.delivery_episode_review import build_delivery_episode_scorecard_review
from src.runtime.delivery_mission_contract import build_delivery_mission_contract
from src.runtime.delivery_recovery_decision import (
    build_delivery_recovery_decision_from_episode_review,
)
from src.runtime.operator_minimal_delivery_simulation import (
    build_operator_minimal_delivery_simulation_status,
)
from src.runtime.px4_gazebo_bounded_simulation_runner import (
    build_px4_gazebo_bounded_simulation_run,
)
from src.runtime.px4_gazebo_mission_scenario_designer import (
    approve_px4_gazebo_mission_scenario_for_bounded_simulation,
    run_px4_gazebo_mission_scenario_designer,
)
from src.runtime.px4_gazebo_telemetry import (
    build_px4_gazebo_hil_review_gate_smoke,
    sanitize_px4_gazebo_telemetry_sample,
)
from src.runtime.simulated_delivery_command import (
    SimulatedCommandCategory,
    attach_simulator_command_execution_preflight,
    attach_simulator_command_execution_receipt,
    attach_simulated_command_rehearsal_result,
    attach_simulated_delivery_command_artifacts,
)
from src.runtime.simulated_delivery_episode import (
    build_simulated_delivery_episode_from_bounded_gazebo_run,
)
from src.runtime.task_store import TaskStore

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _contract():
    return build_delivery_mission_contract(
        mission_id="simulated-command-smoke-001",
        pickup_location={
            "location_id": "pickup-pad-a",
            "latitude": 35.681236,
            "longitude": 139.767125,
        },
        dropoff_location={
            "location_id": "mountain-summit-pad",
            "latitude": 35.700001,
            "longitude": 139.700001,
            "altitude_m": 3000.0,
        },
        delivery_window={
            "earliest_pickup_at": "2026-01-01T12:00:00Z",
            "latest_dropoff_at": "2026-01-01T12:30:00Z",
        },
        package_constraints={"package_id": "pkg-water-5kg", "max_weight_kg": 5.0},
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


def _bounded_request():
    designed = run_px4_gazebo_mission_scenario_designer(
        prompt="標高3000mの山頂に5kgの水を届ける",
        now=NOW,
    )
    approved = approve_px4_gazebo_mission_scenario_for_bounded_simulation(
        proposal=designed["scenario_proposal"],
        validation=designed["validation_result"],
        now=NOW,
    )
    return approved["bounded_simulation_request"]


def _delivery_chain():
    contract = _contract()
    request = _bounded_request()
    telemetry = sanitize_px4_gazebo_telemetry_sample(
        {
            "sample_id": "simulated-command-smoke",
            "source": {
                "source_kind": "gz_sim_harmonic_stdout_log",
                "source_id": "gz-sim-harmonic-container",
                "vehicle_id": "vehicle-delivery-episode",
            },
            "captured_at": "2026-01-01T12:00:00Z",
            "telemetry": {
                "position": "35.681236,139.767125,16.0",
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
    gate = hil_gate["autonomy_gate_result"]
    telemetry_ref = f"px4_gazebo_sanitized_telemetry:{telemetry.telemetry_id}"
    hil_ref = f"hil_telemetry_review:{hil_gate['hil_telemetry_review']['review_id']}"
    gate_ref = f"autonomy_gate_result:{gate['gate_id']}"
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
    episode_artifacts = build_simulated_delivery_episode_from_bounded_gazebo_run(
        delivery_mission_contract=contract,
        bounded_simulation_request=request,
        bounded_simulation_run=run,
        sanitized_telemetry=telemetry,
        hil_telemetry_review=hil_gate["hil_telemetry_review"],
        autonomy_gate_result=gate,
        dropoff_evidence={
            "evidence_ref": "simulated_dropoff_evidence:mountain-summit-pad",
            "dropoff_verified": True,
            "landing_error_m": 0.32,
        },
        now=NOW,
    )
    episode = episode_artifacts["simulated_delivery_episode"]
    reviewed = build_delivery_episode_scorecard_review(
        delivery_mission_contract=contract,
        simulated_delivery_episode=episode,
        delivery_replay_trace=episode_artifacts["delivery_replay_trace"],
        hil_telemetry_review=hil_gate["hil_telemetry_review"],
        autonomy_gate_result=gate,
        sanitized_telemetry=telemetry,
        now=NOW,
    )
    decision = build_delivery_recovery_decision_from_episode_review(
        delivery_mission_contract=contract,
        simulated_delivery_episode=episode,
        delivery_scorecard=reviewed["delivery_scorecard"],
        delivery_episode_review=reviewed["delivery_episode_review"],
        hil_telemetry_review=hil_gate["hil_telemetry_review"],
        autonomy_gate_result=gate,
        now=NOW,
    )
    status = build_operator_minimal_delivery_simulation_status(
        delivery_mission_contract=contract,
        simulated_delivery_episode=episode,
        delivery_scorecard=reviewed["delivery_scorecard"],
        delivery_episode_review=reviewed["delivery_episode_review"],
        delivery_recovery_decision=decision,
        hil_telemetry_review=hil_gate["hil_telemetry_review"],
        autonomy_gate_result=gate,
        now=NOW,
    )["operator_minimal_delivery_simulation_status"]
    return {
        "contract": contract,
        "request": request,
        "run": run,
        "episode": episode,
        "scorecard": reviewed["delivery_scorecard"],
        "review": reviewed["delivery_episode_review"],
        "decision": decision,
        "operator_status": status,
        "hil_review": hil_gate["hil_telemetry_review"],
        "gate": gate,
    }


def main() -> int:
    chain = _delivery_chain()
    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="control_supervisor",
            title="Simulator-only delivery command smoke",
            status="running",
            artifacts={"existing": {"schema_version": "existing.v1"}},
        )
        artifacts = attach_simulated_delivery_command_artifacts(
            task["task_id"],
            delivery_mission_contract=chain["contract"],
            simulated_delivery_episode=chain["episode"],
            delivery_scorecard=chain["scorecard"],
            delivery_episode_review=chain["review"],
            delivery_recovery_decision=chain["decision"],
            operator_minimal_delivery_simulation_status=chain["operator_status"],
            hil_telemetry_review=chain["hil_review"],
            autonomy_gate_result=chain["gate"],
            command_category=SimulatedCommandCategory.START_SIMULATED_DELIVERY,
            now=NOW,
            task_store_factory=lambda: store,
        )
        rehearsal_artifacts = attach_simulated_command_rehearsal_result(
            task["task_id"],
            simulated_command_proposal=artifacts["simulated_command_proposal"],
            simulated_command_approval=artifacts["simulated_command_approval"],
            bounded_simulation_request=chain["request"],
            bounded_simulation_run=chain["run"],
            simulated_delivery_episode=chain["episode"],
            delivery_recovery_decision=chain["decision"],
            operator_minimal_delivery_simulation_status=chain["operator_status"],
            now=NOW,
            task_store_factory=lambda: store,
        )
        preflight_artifacts = attach_simulator_command_execution_preflight(
            task["task_id"],
            simulated_command_proposal=artifacts["simulated_command_proposal"],
            simulated_command_approval=artifacts["simulated_command_approval"],
            simulated_command_receipt=artifacts["simulated_command_receipt"],
            simulated_command_rehearsal_result=rehearsal_artifacts[
                "simulated_command_rehearsal_result"
            ],
            bounded_simulation_run=chain["run"],
            simulated_delivery_episode=chain["episode"],
            delivery_scorecard=chain["scorecard"],
            delivery_episode_review=chain["review"],
            delivery_recovery_decision=chain["decision"],
            operator_minimal_delivery_simulation_status=chain["operator_status"],
            hil_telemetry_review=chain["hil_review"],
            autonomy_gate_result=chain["gate"],
            now=NOW,
            task_store_factory=lambda: store,
        )
        execution_artifacts = attach_simulator_command_execution_receipt(
            task["task_id"],
            simulator_command_execution_preflight=preflight_artifacts[
                "simulator_command_execution_preflight"
            ],
            simulated_command_proposal=artifacts["simulated_command_proposal"],
            simulated_command_approval=artifacts["simulated_command_approval"],
            simulated_command_rehearsal_result=rehearsal_artifacts[
                "simulated_command_rehearsal_result"
            ],
            bounded_simulation_run=chain["run"],
            now=NOW,
            task_store_factory=lambda: store,
        )
        stored = store.get(task["task_id"])
        assert stored is not None
        summary = {
            "task_id": task["task_id"],
            "task_status_preserved": stored["status"] == "running",
            "existing_artifact_retained": stored["artifacts"]["existing"]
            == {"schema_version": "existing.v1"},
            "proposal_schema_version": artifacts["simulated_command_proposal"][
                "schema_version"
            ],
            "command_category": artifacts["simulated_command_proposal"][
                "command_category"
            ],
            "approval_required": artifacts["simulated_command_proposal"][
                "approval_required"
            ],
            "approval_schema_version": artifacts["simulated_command_approval"][
                "schema_version"
            ],
            "operator_approved": artifacts["simulated_command_approval"][
                "operator_approved"
            ],
            "receipt_schema_version": artifacts["simulated_command_receipt"][
                "schema_version"
            ],
            "receipt_status": artifacts["simulated_command_receipt"]["receipt_status"],
            "rehearsal_schema_version": rehearsal_artifacts[
                "simulated_command_rehearsal_result"
            ]["schema_version"],
            "rehearsal_status": rehearsal_artifacts[
                "simulated_command_rehearsal_result"
            ]["rehearsal_status"],
            "rehearsal_only": rehearsal_artifacts["simulated_command_rehearsal_result"][
                "rehearsal_only"
            ],
            "preflight_schema_version": preflight_artifacts[
                "simulator_command_execution_preflight"
            ]["schema_version"],
            "preflight_status": preflight_artifacts[
                "simulator_command_execution_preflight"
            ]["status"],
            "preflight_ready_reasons": preflight_artifacts[
                "simulator_command_execution_preflight"
            ]["ready_reasons"],
            "preflight_blocked_reasons": preflight_artifacts[
                "simulator_command_execution_preflight"
            ]["blocked_reasons"],
            "approval_not_expired": preflight_artifacts[
                "simulator_command_execution_preflight"
            ]["approval_not_expired"],
            "rehearsal_passed": preflight_artifacts[
                "simulator_command_execution_preflight"
            ]["rehearsal_passed"],
            "bounded_run_completed": preflight_artifacts[
                "simulator_command_execution_preflight"
            ]["bounded_run_completed"],
            "autonomy_gate_passed": preflight_artifacts[
                "simulator_command_execution_preflight"
            ]["autonomy_gate_passed"],
            "scorecard_passed": preflight_artifacts[
                "simulator_command_execution_preflight"
            ]["scorecard_passed"],
            "episode_review_passed": preflight_artifacts[
                "simulator_command_execution_preflight"
            ]["episode_review_passed"],
            "operator_minimal_status_allows_rehearsal": preflight_artifacts[
                "simulator_command_execution_preflight"
            ]["operator_minimal_status_allows_rehearsal"],
            "execution_receipt_schema_version": execution_artifacts[
                "simulator_command_execution_receipt"
            ]["schema_version"],
            "execution_receipt_status": execution_artifacts[
                "simulator_command_execution_receipt"
            ]["receipt_status"],
            "execution_category": execution_artifacts[
                "simulator_command_execution_receipt"
            ]["execution_category"],
            "internal_state_transition_only": execution_artifacts[
                "simulator_command_execution_receipt"
            ]["internal_state_transition_only"],
            "internal_state_transition_recorded": execution_artifacts[
                "simulator_command_execution_receipt"
            ]["internal_state_transition_recorded"],
            "external_dispatch_performed": execution_artifacts[
                "simulator_command_execution_receipt"
            ]["external_dispatch_performed"],
            "gazebo_entity_mutation_performed": execution_artifacts[
                "simulator_command_execution_receipt"
            ]["gazebo_entity_mutation_performed"],
            "mavlink_dispatch_performed": execution_artifacts[
                "simulator_command_execution_receipt"
            ]["mavlink_dispatch_performed"],
            "ros_dispatch_performed": execution_artifacts[
                "simulator_command_execution_receipt"
            ]["ros_dispatch_performed"],
            "actuator_execution_performed": execution_artifacts[
                "simulator_command_execution_receipt"
            ]["actuator_execution_performed"],
            "px4_mission_upload_performed": execution_artifacts[
                "simulator_command_execution_receipt"
            ]["px4_mission_upload_performed"],
            "bounded_run_reexecuted": rehearsal_artifacts[
                "simulated_command_rehearsal_result"
            ]["bounded_run_reexecuted"],
            "dispatch_performed": execution_artifacts[
                "simulator_command_execution_receipt"
            ]["dispatch_performed"],
            "command_sent": execution_artifacts["simulator_command_execution_receipt"][
                "command_sent"
            ],
            "dry_run_no_dispatch_recorded": artifacts["simulated_command_receipt"][
                "dry_run_no_dispatch_recorded"
            ],
            "physical_execution_invoked": artifacts["simulated_command_receipt"][
                "physical_execution_invoked"
            ],
            "hardware_target_allowed": artifacts["simulated_command_receipt"][
                "hardware_target_allowed"
            ],
            "mavlink_dispatch_allowed": artifacts["simulated_command_receipt"][
                "mavlink_dispatch_allowed"
            ],
            "ros_dispatch_allowed": artifacts["simulated_command_receipt"][
                "ros_dispatch_allowed"
            ],
            "actuator_execution_allowed": artifacts["simulated_command_receipt"][
                "actuator_execution_allowed"
            ],
            "promotion_or_reuse_created": any(
                key in stored["artifacts"]
                for key in ("approval", "promotion_package", "runtime_reuse")
            ),
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
