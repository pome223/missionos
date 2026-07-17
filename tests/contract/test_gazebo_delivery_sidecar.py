from pathlib import Path

from src.runtime.gazebo_delivery_scenario import build_gazebo_delivery_scenario
from src.runtime.gazebo_delivery_sidecar_contract import (
    attach_gazebo_delivery_sidecar_contract,
    build_gazebo_delivery_sidecar_contract,
    validate_gazebo_delivery_sidecar_contract,
)
from src.runtime.gazebo_delivery_sidecar_v0 import (
    create_and_run_gazebo_delivery_sidecar_v0_task,
)
from src.runtime.task_store import TaskStore
from tests.fixtures.delivery_artifact_chain import NOW, build_delivery_contract


SIDECAR_BOUNDARY_FIELDS = (
    "live_execution_allowed",
    "physical_execution_invoked",
    "command_payload_allowed",
    "gazebo_entity_mutation_allowed",
    "ros_dispatch_allowed",
    "mavlink_dispatch_allowed",
    "actuator_execution_allowed",
)


def test_sidecar_contract_returns_artifacts_for_missionos_validation_only(
    tmp_path: Path,
) -> None:
    contract = build_delivery_contract()
    scenario = build_gazebo_delivery_scenario(
        delivery_mission_contract=contract,
        now=NOW,
    )
    sidecar = build_gazebo_delivery_sidecar_contract(
        delivery_mission_contract=contract,
        gazebo_delivery_scenario=scenario,
        now=NOW,
    )
    validated = validate_gazebo_delivery_sidecar_contract(sidecar)
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        kind="control_supervisor",
        title="Gazebo delivery sidecar contract",
        status="running",
        artifacts={"existing": {"kept": True}},
    )
    artifacts = attach_gazebo_delivery_sidecar_contract(
        task["task_id"],
        delivery_mission_contract=contract,
        gazebo_delivery_scenario=scenario,
        now=NOW,
        task_store_factory=lambda: store,
    )
    stored = store.get(task["task_id"])
    attached = artifacts["gazebo_delivery_sidecar_contract"]

    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"kept": True}
    assert attached["sidecar_returns_artifacts_only"] is True
    assert attached["mission_os_validates_returned_artifacts"] is True
    assert attached["accepted_simulation_requests"]
    assert attached["returned_artifact_schemas"]
    assert {"approval", "promotion_package", "reuse_plan", "runtime_reuse"}.isdisjoint(
        stored["artifacts"]
    )
    assert validated.simulation_only is True
    for field in SIDECAR_BOUNDARY_FIELDS:
        assert getattr(validated, field) is False


def test_sidecar_v0_creates_completed_evidence_sequence_and_timeline(
    tmp_path: Path,
) -> None:
    contract = build_delivery_contract()
    scenario = build_gazebo_delivery_scenario(
        delivery_mission_contract=contract,
        now=NOW,
    )
    store = TaskStore(str(tmp_path / "tasks.db"))
    updated = create_and_run_gazebo_delivery_sidecar_v0_task(
        delivery_mission_contract=contract,
        gazebo_delivery_scenario=scenario,
        title="Gazebo delivery sidecar v0 contract",
        owner_session_id="session-sidecar-v0-fixture",
        owner_user_id="user-sidecar-v0-fixture",
        now=NOW,
        task_store_factory=lambda: store,
    )
    artifacts = updated["artifacts"]
    result = artifacts["simulated_delivery_runner_result"]
    sequence = artifacts["gazebo_delivery_sidecar_v0_sequence"]
    timeline = store.query_timeline(updated["task_id"])

    assert updated["status"] == "completed"
    assert result["final_task_status"] == "completed"
    assert len(sequence) == 5
    assert sequence[-1]["phase"] == "completed"
    for key in (
        "gazebo_delivery_telemetry_window",
        "hil_telemetry_evidence",
        "hil_telemetry_review",
        "delivery_mission_gate_result",
        "delivery_progress_review",
        "delivery_recovery_decision",
    ):
        assert key in artifacts
    assert any(
        event["event_type"] == "status_changed"
        and event.get("status") == "completed"
        for event in timeline["events"]
    )
    assert {"approval", "promotion_package", "reuse_plan", "runtime_reuse"}.isdisjoint(
        artifacts
    )
    for field in SIDECAR_BOUNDARY_FIELDS:
        assert result[field] is False
