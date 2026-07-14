from pathlib import Path

import pytest
from pydantic import ValidationError

from src.runtime.delivery_mission_contract import build_delivery_mission_contract
from src.runtime.delivery_mission_gate import (
    DELIVERY_MISSION_GATE_RESULT_SCHEMA_VERSION,
    attach_delivery_mission_gate_artifacts,
)
from src.runtime.delivery_mission_policy_review import (
    DELIVERY_POLICY_BUCKET_BATTERY_RETURN_HOME_RECOMMENDED,
    attach_delivery_mission_policy_review,
)
from src.runtime.delivery_progress_review import attach_delivery_progress_review
from src.runtime.delivery_recovery_decision import attach_delivery_recovery_decision
from src.runtime.simulated_delivery_episode import (
    SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION,
    attach_simulated_delivery_episode,
)
from src.runtime.task_store import TaskStore
from tests.fixtures.delivery_artifact_chain import (
    NOW,
    build_delivery_artifact_chain,
    build_delivery_contract,
)


FORBIDDEN_AUTHORITY_ARTIFACTS = {
    "approval",
    "promotion_package",
    "reuse_plan",
    "runtime_reuse",
}


def test_delivery_contract_is_deterministic_and_rejects_authority_escalation() -> None:
    contract = build_delivery_contract()
    assert contract.contract_id == build_delivery_contract().contract_id
    assert "hil_telemetry_evidence" in contract.required_evidence
    assert contract.operator_approval_required is True
    assert contract.operator_approval_performed is False
    assert contract.live_execution_allowed is False
    assert contract.physical_execution_invoked is False
    assert contract.command_payload_allowed is False
    assert contract.ros_dispatch_allowed is False
    assert contract.mavlink_dispatch_allowed is False
    assert contract.actuator_execution_allowed is False

    with pytest.raises((ValueError, ValidationError)):
        build_delivery_mission_contract(
            mission_id="delivery-contract-fixture-001",
            pickup_location=contract.pickup_location.model_dump(mode="json"),
            dropoff_location=contract.dropoff_location.model_dump(mode="json"),
            delivery_window=contract.delivery_window.model_dump(mode="json"),
            package_constraints=contract.package_constraints.model_dump(mode="json"),
            geofence_constraints=contract.geofence_constraints.model_dump(mode="json"),
            weather_constraints=contract.weather_constraints.model_dump(mode="json"),
            battery_policy=contract.battery_policy.model_dump(mode="json"),
            landing_zone_policy=contract.landing_zone_policy.model_dump(mode="json"),
            telemetry_requirements=contract.telemetry_requirements.model_dump(
                mode="json"
            ),
            metadata={"nested": [{"RosTopic": "/cmd_vel"}]},
            now=NOW,
        )

    payload = contract.model_dump(mode="json")
    payload["live_execution_allowed"] = True
    with pytest.raises(ValidationError):
        type(contract).model_validate(payload)


def test_delivery_chain_reuses_one_fixture_and_preserves_safety_contracts() -> None:
    chain = build_delivery_artifact_chain()
    gate = chain.gate_artifacts["delivery_mission_gate_result"]

    assert chain.policy_review.passed is True
    assert (
        DELIVERY_POLICY_BUCKET_BATTERY_RETURN_HOME_RECOMMENDED
        in chain.policy_review.warning_reasons
    )
    assert chain.policy_review.return_to_home_recommended is True
    assert gate["schema_version"] == DELIVERY_MISSION_GATE_RESULT_SCHEMA_VERSION
    assert gate["status"] == "warning"
    assert gate["passed"] is True
    assert chain.episode.schema_version == SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION
    assert chain.episode.phase == "preflight_review"
    assert chain.episode.final_status == "ready_with_warnings"
    assert chain.progress_review.status == "in_progress"
    assert chain.progress_review.pickup_reached is True
    assert chain.progress_review.route_progress_percent == 42.5

    for artifact in (
        chain.policy_review,
        chain.episode,
        chain.progress_review,
    ):
        assert artifact.live_execution_allowed is False
        assert artifact.physical_execution_invoked is False
        assert artifact.command_payload_allowed is False
    assert gate["live_execution_allowed"] is False
    assert gate["physical_execution_invoked"] is False
    assert gate["command_payload_allowed"] is False


def test_delivery_chain_attach_paths_preserve_task_and_create_no_authority(
    tmp_path: Path,
) -> None:
    chain = build_delivery_artifact_chain()
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        kind="control_supervisor",
        title="Delivery artifact chain contract",
        status="running",
        artifacts={"existing": {"kept": True}},
    )
    def factory() -> TaskStore:
        return store

    attach_delivery_mission_policy_review(
        task["task_id"],
        delivery_mission_contract=chain.contract,
        sanitized_telemetry=chain.telemetry,
        hil_telemetry_review=chain.hil_review,
        now=NOW,
        task_store_factory=factory,
    )
    attach_delivery_mission_gate_artifacts(
        task["task_id"],
        delivery_mission_contract=chain.contract,
        delivery_mission_policy_review=chain.policy_review,
        now=NOW,
        task_store_factory=factory,
    )
    attach_simulated_delivery_episode(
        task["task_id"],
        delivery_mission_contract=chain.contract,
        delivery_mission_policy_review=chain.policy_review,
        delivery_mission_scorecard=chain.gate_artifacts["delivery_mission_scorecard"],
        delivery_mission_gate_result=chain.gate_artifacts[
            "delivery_mission_gate_result"
        ],
        now=NOW,
        task_store_factory=factory,
    )
    attach_delivery_progress_review(
        task["task_id"],
        delivery_mission_contract=chain.contract,
        gazebo_delivery_scenario=chain.scenario,
        simulated_delivery_episode=chain.episode,
        sanitized_telemetry=chain.telemetry,
        hil_telemetry_review=chain.hil_review,
        now=NOW,
        task_store_factory=factory,
    )
    recovery_artifacts = attach_delivery_recovery_decision(
        task["task_id"],
        delivery_mission_contract=chain.contract,
        simulated_delivery_episode=chain.episode,
        delivery_progress_review=chain.progress_review,
        now=NOW,
        task_store_factory=factory,
    )

    stored = store.get(task["task_id"])
    decision = recovery_artifacts["delivery_recovery_decision"]
    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"kept": True}
    assert FORBIDDEN_AUTHORITY_ARTIFACTS.isdisjoint(stored["artifacts"])
    assert decision["primary_action"] == "return_to_home_recommended"
    assert decision["return_to_home_recommended"] is True
    assert decision["recommendations_only"] is True
    assert decision["live_execution_allowed"] is False
    assert decision["physical_execution_invoked"] is False
    assert decision["command_payload_allowed"] is False
    assert decision["gazebo_entity_mutation_allowed"] is False
