from pathlib import Path

from src.runtime.operator_minimal_delivery_simulation import (
    OPERATOR_MINIMAL_DELIVERY_SIMULATION_STATUS_SCHEMA_VERSION,
)
from src.runtime.simulated_delivery_command import (
    SimulatedCommandCategory,
    attach_simulator_command_execution_preflight,
    attach_simulator_command_execution_receipt,
    attach_simulated_command_rehearsal_result,
    attach_simulated_delivery_command_artifacts,
)
from src.runtime.simulated_delivery_episode import (
    DELIVERY_REPLAY_TRACE_SCHEMA_VERSION,
    SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION,
)
from src.runtime.task_store import TaskStore
from tests.fixtures.delivery_artifact_chain import (
    NOW,
    build_completed_delivery_artifact_chain,
)


SIMULATOR_BOUNDARY_FIELDS = (
    "physical_execution_invoked",
    "hardware_target_allowed",
    "mavlink_dispatch_allowed",
    "ros_dispatch_allowed",
    "actuator_execution_allowed",
)


def _new_completed_chain(tmp_path: Path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        kind="control_supervisor",
        title="Completed delivery execution contract",
        status="running",
        artifacts={"existing": {"kept": True}},
    )
    chain = build_completed_delivery_artifact_chain(
        store=store,
        task_id=task["task_id"],
    )
    return store, task, chain


def test_bounded_episode_and_operator_status_preserve_simulator_boundary(
    tmp_path: Path,
) -> None:
    store, task, chain = _new_completed_chain(tmp_path)
    stored = store.get(task["task_id"])
    episode = chain.episode_artifacts["simulated_delivery_episode"]
    trace = chain.episode_artifacts["delivery_replay_trace"]
    operator_status = chain.operator_artifacts[
        "operator_minimal_delivery_simulation_status"
    ]

    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"kept": True}
    assert episode["schema_version"] == SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION
    assert trace["schema_version"] == DELIVERY_REPLAY_TRACE_SCHEMA_VERSION
    assert episode["final_status"] == "completed"
    assert episode["dropoff_verified"] is True
    assert "dropoff_verified" in [step["phase"] for step in episode["steps"]]
    assert operator_status["schema_version"] == (
        OPERATOR_MINIMAL_DELIVERY_SIMULATION_STATUS_SCHEMA_VERSION
    )
    assert operator_status["status"] == "completed_without_operator_intervention"
    assert operator_status["operator_intervention_required"] is False
    assert operator_status["escalation_triggers"] == []
    assert "operator_escalation_review" not in stored["artifacts"]
    assert {"approval", "promotion_package", "reuse_plan", "runtime_reuse"}.isdisjoint(
        stored["artifacts"]
    )
    assert episode["gazebo_execution_invoked_by_episode"] is False
    assert episode["px4_mission_upload_allowed"] is False
    assert episode["unbounded_setpoint_stream_allowed"] is False
    for artifact in (episode, operator_status):
        for field in SIMULATOR_BOUNDARY_FIELDS:
            assert artifact[field] is False


def test_simulated_command_flow_records_internal_transition_without_dispatch(
    tmp_path: Path,
) -> None:
    store, task, chain = _new_completed_chain(tmp_path)
    episode = chain.episode_artifacts["simulated_delivery_episode"]
    scorecard = chain.review_artifacts["delivery_scorecard"]
    review = chain.review_artifacts["delivery_episode_review"]
    decision = chain.decision_artifacts["delivery_recovery_decision"]
    operator_status = chain.operator_artifacts[
        "operator_minimal_delivery_simulation_status"
    ]
    run = chain.run_artifacts["px4_gazebo_bounded_simulation_run"]
    hil_review = chain.run_artifacts["hil_telemetry_review"]
    gate = chain.run_artifacts["autonomy_gate_result"]

    command = attach_simulated_delivery_command_artifacts(
        task["task_id"],
        delivery_mission_contract=chain.contract,
        simulated_delivery_episode=episode,
        delivery_scorecard=scorecard,
        delivery_episode_review=review,
        delivery_recovery_decision=decision,
        operator_minimal_delivery_simulation_status=operator_status,
        hil_telemetry_review=hil_review,
        autonomy_gate_result=gate,
        command_category=SimulatedCommandCategory.START_SIMULATED_DELIVERY,
        now=NOW,
        task_store_factory=lambda: store,
    )
    rehearsal = attach_simulated_command_rehearsal_result(
        task["task_id"],
        simulated_command_proposal=command["simulated_command_proposal"],
        simulated_command_approval=command["simulated_command_approval"],
        bounded_simulation_request=chain.bounded_request,
        bounded_simulation_run=run,
        simulated_delivery_episode=episode,
        delivery_recovery_decision=decision,
        operator_minimal_delivery_simulation_status=operator_status,
        now=NOW,
        task_store_factory=lambda: store,
    )["simulated_command_rehearsal_result"]
    preflight = attach_simulator_command_execution_preflight(
        task["task_id"],
        simulated_command_proposal=command["simulated_command_proposal"],
        simulated_command_approval=command["simulated_command_approval"],
        simulated_command_receipt=command["simulated_command_receipt"],
        simulated_command_rehearsal_result=rehearsal,
        bounded_simulation_run=run,
        simulated_delivery_episode=episode,
        delivery_scorecard=scorecard,
        delivery_episode_review=review,
        delivery_recovery_decision=decision,
        operator_minimal_delivery_simulation_status=operator_status,
        hil_telemetry_review=hil_review,
        autonomy_gate_result=gate,
        now=NOW,
        task_store_factory=lambda: store,
    )["simulator_command_execution_preflight"]
    execution = attach_simulator_command_execution_receipt(
        task["task_id"],
        simulator_command_execution_preflight=preflight,
        simulated_command_proposal=command["simulated_command_proposal"],
        simulated_command_approval=command["simulated_command_approval"],
        simulated_command_rehearsal_result=rehearsal,
        bounded_simulation_run=run,
        now=NOW,
        task_store_factory=lambda: store,
    )["simulator_command_execution_receipt"]
    stored = store.get(task["task_id"])

    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"kept": True}
    assert command["simulated_command_proposal"]["approval_required"] is False
    assert command["simulated_command_approval"]["operator_approved"] is True
    assert command["simulated_command_approval"]["approval_scope"] == (
        "simulator_only_dry_run_receipt"
    )
    assert command["simulated_command_approval"]["approved_for_gazebo_execution"] is False
    assert command["simulated_command_approval"]["approved_for_hardware"] is False
    assert command["simulated_command_receipt"]["receipt_status"] == (
        "dry_run_no_dispatch_recorded"
    )
    assert rehearsal["rehearsal_status"] == "rehearsed"
    assert rehearsal["rehearsal_only"] is True
    assert rehearsal["bounded_run_reexecuted"] is False
    assert preflight["status"] == "ready_for_simulator_command"
    assert execution["receipt_status"] == "internal_state_transition_recorded"
    assert execution["internal_state_transition_only"] is True
    assert execution["internal_state_transition_recorded"] is True
    for field in (
        "external_dispatch_performed",
        "gazebo_entity_mutation_performed",
        "mavlink_dispatch_performed",
        "ros_dispatch_performed",
        "actuator_execution_performed",
        "px4_mission_upload_performed",
        "dispatch_performed",
        "command_sent",
    ):
        assert execution[field] is False
    for artifact in (
        command["simulated_command_proposal"],
        command["simulated_command_approval"],
        command["simulated_command_receipt"],
        rehearsal,
        preflight,
        execution,
    ):
        for field in SIMULATOR_BOUNDARY_FIELDS:
            assert artifact[field] is False
