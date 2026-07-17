from pathlib import Path

import pytest

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
from tests.fixtures.delivery_artifact_chain import NOW, build_delivery_contract


FORBIDDEN_RUNTIME_ARTIFACTS = {
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


def _state_world_log() -> str:
    return "\n".join(
        (
            "Gazebo Sim Server v8.0.0",
            "Loading SDF world file[/worlds/delivery_state_driven.sdf]",
            "Serving full state on [/world/delivery_state_driven/state]",
        )
    )


def _pose_text(x: float) -> str:
    return f"""
pose {{
  name: "delivery_vehicle_state"
  id: 8
  position {{
    x: {x}
    y: 0.0
    z: 0.2
  }}
  orientation {{ w: 1 }}
}}
"""


@pytest.mark.parametrize(
    ("reason", "error_message"),
    (
        ("no_pose_topic_output", None),
        ("collector_timeout", "pose topic collection timed out"),
        (
            "container_exited_early",
            "container exited before pose topic became available",
        ),
    ),
)
def test_invalid_delivery_observation_is_debug_diagnostics_only(
    tmp_path: Path,
    reason: str,
    error_message: str | None,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        kind="simulated_delivery_runner",
        title=f"Gazebo delivery observation failure: {reason}",
        status="running",
        artifacts={"existing": {"case_id": reason, "kept": True}},
    )
    diagnostics = attach_gazebo_delivery_observation_diagnostics_artifact(
        task["task_id"],
        _state_world_log(),
        [],
        error_message=error_message,
        captured_at=NOW,
        reason_override=None if reason == "no_pose_topic_output" else reason,
        task_store_factory=lambda: store,
    )
    stored = store.get(task["task_id"])

    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {
        "case_id": reason,
        "kept": True,
    }
    assert FORBIDDEN_RUNTIME_ARTIFACTS.isdisjoint(stored["artifacts"])
    assert diagnostics["schema_version"] == (
        GAZEBO_DELIVERY_OBSERVATION_DIAGNOSTICS_SCHEMA_VERSION
    )
    assert diagnostics["reason"] == reason
    assert diagnostics["debug_only"] is True
    assert diagnostics["hil_artifacts_persisted"] is False
    assert diagnostics["gate_artifacts_persisted"] is False
    assert diagnostics["runner_artifacts_persisted"] is False


def test_geofence_violation_terminally_blocks_delivery_without_authority(
    tmp_path: Path,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    contract = build_delivery_contract()
    scenario = build_gazebo_delivery_scenario(
        delivery_mission_contract=contract,
        now=NOW,
    )
    telemetry = collect_gz_sim_delivery_entity_state_sanitized(
        _state_world_log(),
        [_pose_text(x) for x in (0.0, 6.0, 24.2, 25.1)],
        captured_at=NOW,
    ).model_dump(mode="json")
    telemetry["measurements"] = dict(telemetry["measurements"])
    telemetry["measurements"]["route_geofence_violation"] = True
    task = store.create(
        kind="simulated_delivery_runner",
        title="Gazebo delivery geofence violation contract",
        status="running",
        artifacts={"existing": {"kept": True}},
    )

    blocked = run_simulated_delivery_task_v0(
        task["task_id"],
        delivery_mission_contract=contract,
        gazebo_delivery_scenario=scenario,
        sanitized_telemetry=telemetry,
        now=NOW,
        task_store_factory=lambda: store,
    )
    artifacts = blocked["artifacts"]
    runner_result = artifacts["simulated_delivery_runner_result"]

    assert blocked["status"] == "blocked"
    assert runner_result["final_task_status"] == "blocked"
    assert (
        DELIVERY_PROGRESS_BUCKET_ROUTE_GEOFENCE_VIOLATION
        in runner_result["blocked_reasons"]
    )
    assert artifacts["delivery_recovery_decision"]["primary_action"] == (
        "operator_escalation_required"
    )
    assert artifacts["existing"] == {"kept": True}
    assert {"approval", "promotion_package", "reuse_plan", "runtime_reuse"}.isdisjoint(
        artifacts
    )
    for field in (
        "live_execution_allowed",
        "physical_execution_invoked",
        "command_payload_allowed",
        "ros_dispatch_allowed",
        "mavlink_dispatch_allowed",
        "actuator_execution_allowed",
    ):
        assert runner_result[field] is False
