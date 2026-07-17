from pathlib import Path
from typing import Callable

import pytest

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
from tests.fixtures.delivery_artifact_chain import NOW, build_delivery_contract


def _gate(contract, *, battery_percent: float = 88.0) -> dict:
    telemetry = sanitize_px4_gazebo_telemetry_sample(
        {
            "sample_id": f"sim-control-{battery_percent}",
            "source": {
                "source_kind": "gz_sim_delivery_entity_state_pose",
                "source_id": "gz-sim-sim-control-fixture",
                "vehicle_id": "vehicle-sim-control-fixture",
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


def _identity(gate: dict) -> dict:
    return gate


def _wrong_contract(gate: dict) -> dict:
    return {**gate, "delivery_mission_contract_id": "delivery_mission_contract:other"}


def _wrong_mission(gate: dict) -> dict:
    return {**gate, "delivery_mission_id": "other-mission"}


@pytest.mark.parametrize(
    (
        "case_id",
        "battery_percent",
        "approved",
        "gate_transform",
        "expected_status",
        "blocked_reason",
    ),
    (
        ("happy", 88.0, True, _identity, "completed", None),
        (
            "missing_approval",
            88.0,
            False,
            _identity,
            "blocked",
            "simulation_operator_approval_missing",
        ),
        (
            "blocked_gate",
            20.0,
            True,
            _identity,
            "blocked",
            "battery_abort_recommended",
        ),
        (
            "mismatched_contract",
            88.0,
            True,
            _wrong_contract,
            "blocked",
            "pre_gate_contract_mismatch",
        ),
        (
            "mismatched_mission",
            88.0,
            True,
            _wrong_mission,
            "blocked",
            "pre_gate_mission_mismatch",
        ),
    ),
)
def test_simulation_control_requires_gate_identity_and_operator_approval(
    tmp_path: Path,
    case_id: str,
    battery_percent: float,
    approved: bool,
    gate_transform: Callable[[dict], dict],
    expected_status: str,
    blocked_reason: str | None,
) -> None:
    contract = build_delivery_contract()
    scenario = build_gazebo_delivery_scenario(
        delivery_mission_contract=contract,
        now=NOW,
    )
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        kind="gazebo_delivery_simulation_control_v0",
        title=f"Simulation control contract: {case_id}",
        status="running",
        artifacts={"existing": {"case_id": case_id, "kept": True}},
    )
    updated = run_gazebo_delivery_simulation_control_v0_task(
        task["task_id"],
        delivery_mission_contract=contract,
        gazebo_delivery_scenario=scenario,
        delivery_mission_gate_result=gate_transform(
            _gate(contract, battery_percent=battery_percent)
        ),
        operator_approval_performed=approved,
        now=NOW,
        task_store_factory=lambda: store,
    )
    audit = updated["artifacts"]["gazebo_delivery_simulation_control_audit"]

    assert updated["status"] == expected_status
    assert updated["artifacts"]["existing"] == {"case_id": case_id, "kept": True}
    assert {"approval", "promotion_package", "reuse_plan", "runtime_reuse"}.isdisjoint(
        updated["artifacts"]
    )
    if blocked_reason is None:
        assert len(audit["sidecar_result_refs"]) == 5
        assert updated["artifacts"]["simulated_delivery_runner_result"][
            "final_task_status"
        ] == "completed"
    else:
        assert blocked_reason in audit["blocked_reasons"]
        if case_id == "blocked_gate":
            assert "pre_gate_not_passed" in audit["blocked_reasons"]
        assert "gazebo_delivery_sidecar_v0_sequence" not in updated["artifacts"]
        assert "simulated_delivery_runner_result" not in updated["artifacts"]
    for field in (
        "live_execution_allowed",
        "physical_execution_invoked",
        "command_payload_allowed",
        "gazebo_entity_mutation_allowed",
        "ros_dispatch_allowed",
        "mavlink_dispatch_allowed",
        "actuator_execution_allowed",
    ):
        assert audit[field] is False
