"""Deterministic delivery-mission contracts replacing two opt-in wrappers."""

from datetime import datetime, timezone
from tempfile import TemporaryDirectory

import pytest

from src.runtime.px4_gazebo_delivery_mission_control import (
    DEFAULT_MISSION_PHASE_SEQUENCE,
    PX4GazeboDeliveryMissionFailureType,
    PX4GazeboDeliveryMissionPhase,
    attach_px4_gazebo_delivery_mission_v1_task,
    build_px4_gazebo_delivery_mission_contract,
    build_px4_gazebo_delivery_mission_golden_corpus,
    prepare_px4_gazebo_delivery_mission_v1,
    run_px4_gazebo_delivery_mission_v1,
)
from src.runtime.task_store import TaskStore


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
ROUTE_DISPATCH_REFS = (
    "px4_gazebo_route_command_dispatch_result:leg_pickup_to_waypoint",
    "px4_gazebo_route_command_dispatch_result:leg_waypoint_alpha_to_bravo",
    "px4_gazebo_route_command_dispatch_result:leg_waypoint_to_dropoff",
)
ROUTE_COMPLETION_REFS = (
    "px4_gazebo_route_delivery_completion_gate:leg_pickup_to_waypoint",
    "px4_gazebo_route_delivery_completion_gate:leg_waypoint_alpha_to_bravo",
    "px4_gazebo_route_delivery_completion_gate:leg_waypoint_to_dropoff",
)


@pytest.fixture(scope="module")
def contract():
    return build_px4_gazebo_delivery_mission_contract(
        route_plan_refs=(
            "px4_gazebo_pickup_dropoff_route_plan:pickup_to_waypoint",
            "px4_gazebo_pickup_dropoff_route_plan:waypoint_alpha_to_bravo",
            "px4_gazebo_pickup_dropoff_route_plan:waypoint_to_dropoff",
        ),
        waypoint_refs=(
            "gazebo_waypoint:alpha",
            "gazebo_waypoint:bravo",
            "gazebo_waypoint:charlie",
        ),
        now=NOW,
    )


@pytest.fixture(scope="module")
def happy(contract):
    return run_px4_gazebo_delivery_mission_v1(
        mission_contract=contract,
        route_dispatch_refs=ROUTE_DISPATCH_REFS,
        route_completion_gate_refs=ROUTE_COMPLETION_REFS,
        now=NOW,
    )


@pytest.fixture(scope="module")
def pose_failure(contract):
    return run_px4_gazebo_delivery_mission_v1(
        mission_contract=contract,
        failure_phase=PX4GazeboDeliveryMissionPhase.DELIVERY_ROUTE,
        failure_type=PX4GazeboDeliveryMissionFailureType.POSE_DEVIATION,
        now=NOW,
    )


def test_happy_path_is_complete_but_never_gains_execution_authority(
    contract, happy
) -> None:
    prepared = prepare_px4_gazebo_delivery_mission_v1(
        mission_contract=contract,
        now=NOW,
    )
    runner = happy["runner_result"]

    assert contract.contract_refs_complete is True
    assert prepared["prepared_run"].schema_version
    assert runner.final_status.value == "completed"
    assert len(runner.observed_phases) == len(DEFAULT_MISSION_PHASE_SEQUENCE) == 10
    assert runner.waypoint_count >= 3
    assert runner.route_segment_count >= 3
    assert runner.dropoff_landing_error_m <= 0.5
    assert len(happy["health_snapshots"]) == 20
    assert {item.verdict.value for item in happy["phase_gate_evaluations"]} == {
        "pass"
    }
    assert len(happy["recovery_policy_matrix"].entries) == (
        len(DEFAULT_MISSION_PHASE_SEQUENCE)
        * len(PX4GazeboDeliveryMissionFailureType)
    )
    for field in (
        "hardware_target_allowed",
        "physical_execution_invoked",
        "px4_mission_upload_allowed",
        "unbounded_setpoint_stream_allowed",
        "memory_direct_command_authority_allowed",
    ):
        assert getattr(runner, field) is False, field


@pytest.mark.parametrize(
    ("failure_type", "phase"),
    [
        (
            PX4GazeboDeliveryMissionFailureType.POSE_DEVIATION,
            PX4GazeboDeliveryMissionPhase.DELIVERY_ROUTE,
        ),
        (
            PX4GazeboDeliveryMissionFailureType.BATTERY_LOW,
            PX4GazeboDeliveryMissionPhase.DELIVERY_ROUTE,
        ),
        (
            PX4GazeboDeliveryMissionFailureType.LINK_LOSS,
            PX4GazeboDeliveryMissionPhase.WAYPOINT_ROUTE,
        ),
        (
            PX4GazeboDeliveryMissionFailureType.GATE_BLOCKED,
            PX4GazeboDeliveryMissionPhase.DROPOFF_APPROACH,
        ),
        (
            PX4GazeboDeliveryMissionFailureType.ACK_TIMEOUT,
            PX4GazeboDeliveryMissionPhase.TAKEOFF,
        ),
    ],
)
def test_failure_matrix_stays_blocked(contract, failure_type, phase) -> None:
    result = run_px4_gazebo_delivery_mission_v1(
        mission_contract=contract,
        failure_phase=phase,
        failure_type=failure_type,
        now=NOW,
    )

    assert result["runner_result"].final_status.value == "blocked"
    assert result["phase_gate_evaluations"][-1].verdict.value in {
        "abort",
        "block",
        "blocked",
    }
    assert result["runner_result"].physical_execution_invoked is False


def test_task_attachment_preserves_artifacts_and_golden_corpus(
    happy, pose_failure
) -> None:
    corpus = build_px4_gazebo_delivery_mission_golden_corpus(
        happy_runner_result=happy["runner_result"],
        failure_runner_result=pose_failure["runner_result"],
        now=NOW,
    )
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="px4_gazebo_delivery_mission_runner_v1",
            title="delivery mission contract",
            status="running",
            artifacts={"existing": {"kept": True}},
        )
        updated = attach_px4_gazebo_delivery_mission_v1_task(
            task["task_id"],
            mission_artifacts=happy,
            task_store_factory=lambda: store,
        )

    assert updated["status"] == "completed"
    assert updated["artifacts"]["existing"] == {"kept": True}
    assert len(corpus.case_ids) == 2
    assert "multi_waypoint_happy_path" in corpus.required_coverage_labels
    assert "failure_branching" in corpus.required_coverage_labels
