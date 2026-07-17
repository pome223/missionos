"""Contract tests for the delivery recovery request -> run -> outcome chain.

These replace the logic-only smokes (#428/#429/#430) whose shared fixture
lived in a module that is no longer part of this repository. The chain under
test converts an observed fault plus a rule-based recovery decision into
bounded, logic-only artifacts and must never acquire execution authority.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.runtime.delivery_episode_review import build_delivery_episode_scorecard_review
from src.runtime.delivery_fault_event import (
    DeliveryFaultCategory,
    DeliveryFaultSeverity,
    build_delivery_fault_event,
)
from src.runtime.delivery_mission_contract import build_delivery_mission_contract
from src.runtime.delivery_recovery_decision import (
    DeliveryRecoveryAction,
    build_delivery_recovery_decision_from_episode_review,
)
from src.runtime.delivery_recovery_outcome import (
    DELIVERY_RECOVERY_OUTCOME_SCHEMA_VERSION,
    DeliveryRecoveryOutcomeCategory,
    attach_delivery_recovery_outcome,
)
from src.runtime.delivery_recovery_request import (
    DELIVERY_RECOVERY_REQUEST_SCHEMA_VERSION,
    DeliveryRecoveryRequestError,
    DeliveryRecoveryRequestKind,
    DeliveryRecoveryRequestStatus,
    attach_delivery_recovery_request,
    build_delivery_recovery_request,
)
from src.runtime.delivery_recovery_run import (
    DELIVERY_RECOVERY_RUN_SCHEMA_VERSION,
    DeliveryRecoveryRunStatus,
    attach_delivery_recovery_run,
    build_delivery_recovery_run,
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
from src.runtime.px4_gazebo_sitl_mission_upload import MAV_CMD_NAV_RETURN_TO_LAUNCH
from src.runtime.px4_gazebo_telemetry import (
    build_px4_gazebo_hil_review_gate_smoke,
    sanitize_px4_gazebo_telemetry_sample,
)
from src.runtime.simulated_delivery_command import (
    SimulatedCommandCategory,
    build_simulated_command_approval,
    build_simulated_command_proposal,
    build_simulated_command_receipt,
    build_simulated_command_rehearsal_result,
    build_simulator_command_execution_preflight,
)
from src.runtime.simulated_delivery_episode import (
    build_simulated_delivery_episode_from_bounded_gazebo_run,
)
from src.runtime.task_store import TaskStore

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _contract():
    return build_delivery_mission_contract(
        mission_id="recovery-chain-contract-001",
        pickup_location={
            "location_id": "pickup-pad-a",
            "latitude": 35.681236,
            "longitude": 139.767125,
        },
        dropoff_location={
            "location_id": "dropoff-pad-b",
            "latitude": 35.689487,
            "longitude": 139.691706,
            "altitude_m": 30.0,
        },
        delivery_window={
            "earliest_pickup_at": "2026-01-01T12:00:00Z",
            "latest_dropoff_at": "2026-01-01T12:30:00Z",
        },
        package_constraints={"package_id": "pkg-recovery-chain", "max_weight_kg": 1.0},
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


def _preflight_chain() -> dict:
    """Rebuild the reviewed-episode evidence chain the recovery chain consumes."""

    contract = _contract()
    designed = run_px4_gazebo_mission_scenario_designer(
        prompt="標高30mの配送地点に1kgの荷物を届ける",
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
            "sample_id": "recovery-chain-contract",
            "source": {
                "source_kind": "gz_sim_harmonic_stdout_log",
                "source_id": "gz-sim-recovery-chain",
                "vehicle_id": "vehicle-recovery-chain",
            },
            "captured_at": "2026-01-01T12:00:00Z",
            "telemetry": {
                "position": "35.689487,139.691706,0.18",
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
    bounded_run = build_px4_gazebo_bounded_simulation_run(
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
        bounded_simulation_run=bounded_run,
        sanitized_telemetry=telemetry,
        hil_telemetry_review=hil_gate["hil_telemetry_review"],
        autonomy_gate_result=gate,
        dropoff_evidence={
            "evidence_ref": "simulated_dropoff_evidence:dropoff-pad-b",
            "dropoff_verified": True,
            "landing_error_m": 0.18,
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
    operator_status = build_operator_minimal_delivery_simulation_status(
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
        "run": bounded_run,
        "episode": episode,
        "scorecard": reviewed["delivery_scorecard"],
        "review": reviewed["delivery_episode_review"],
        "decision": decision,
        "operator_status": operator_status,
        "hil_review": hil_gate["hil_telemetry_review"],
        "gate": gate,
    }


def _preflight(chain: dict):
    proposal = build_simulated_command_proposal(
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
    )
    approval = build_simulated_command_approval(
        simulated_command_proposal=proposal,
        now=NOW,
    )
    receipt = build_simulated_command_receipt(
        simulated_command_proposal=proposal,
        simulated_command_approval=approval,
        now=NOW,
    )
    rehearsal = build_simulated_command_rehearsal_result(
        simulated_command_proposal=proposal,
        simulated_command_approval=approval,
        bounded_simulation_request=chain["request"],
        bounded_simulation_run=chain["run"],
        simulated_delivery_episode=chain["episode"],
        delivery_recovery_decision=chain["decision"],
        operator_minimal_delivery_simulation_status=chain["operator_status"],
        now=NOW,
    )
    preflight = build_simulator_command_execution_preflight(
        simulated_command_proposal=proposal,
        simulated_command_approval=approval,
        simulated_command_receipt=receipt,
        simulated_command_rehearsal_result=rehearsal,
        bounded_simulation_run=chain["run"],
        simulated_delivery_episode=chain["episode"],
        delivery_scorecard=chain["scorecard"],
        delivery_episode_review=chain["review"],
        delivery_recovery_decision=chain["decision"],
        operator_minimal_delivery_simulation_status=chain["operator_status"],
        hil_telemetry_review=chain["hil_review"],
        autonomy_gate_result=chain["gate"],
        now=NOW,
    )
    return {"proposal": proposal, "approval": approval, "preflight": preflight}


@pytest.fixture(scope="module")
def chain() -> dict:
    built = _preflight_chain()
    built.update(_preflight(built))
    return built


@pytest.fixture(scope="module")
def rth_decision(chain: dict):
    return chain["decision"].model_copy(
        update={
            "primary_action": DeliveryRecoveryAction.RETURN_TO_HOME_RECOMMENDED,
            "return_to_home_recommended": True,
            "abort_recommended": False,
            "hold_recommended": False,
            "hold_proposed": False,
            "operator_escalation_required": False,
        }
    )


@pytest.fixture(scope="module")
def battery_fault(chain: dict):
    return build_delivery_fault_event(
        fault_category=DeliveryFaultCategory.BATTERY_LOW,
        severity=DeliveryFaultSeverity.BLOCKING,
        telemetry_refs=["px4_gazebo_sanitized_telemetry:logic-only-battery"],
        episode_ref=chain["operator_status"].simulated_delivery_episode_ref,
        evidence_refs=[chain["operator_status"].delivery_episode_review_ref],
        blocked_reasons=["battery_low"],
        observed_at=NOW,
    )


@pytest.fixture(scope="module")
def recovery_request(chain: dict, rth_decision, battery_fault):
    return build_delivery_recovery_request(
        delivery_mission_contract=chain["contract"],
        delivery_recovery_decision=rth_decision,
        delivery_fault_event=battery_fault,
        operator_minimal_delivery_simulation_status=chain["operator_status"],
        now=NOW,
    )


@pytest.fixture(scope="module")
def recovery_run(chain: dict, recovery_request):
    return build_delivery_recovery_run(
        delivery_mission_contract=chain["contract"],
        delivery_recovery_request=recovery_request,
        simulator_command_execution_preflight=chain["preflight"],
        simulated_command_proposal=chain["proposal"],
        simulated_command_approval=chain["approval"],
        sitl_session_ref="sitl_session:logic-only-recovery",
        observed_facts={"bounded_recovery_plan_recorded": True},
        started_at=NOW,
        finished_at=NOW,
    )


def test_request_compiles_return_to_home_into_bounded_ready_request(
    recovery_request, battery_fault
) -> None:
    request = recovery_request
    assert request.schema_version == DELIVERY_RECOVERY_REQUEST_SCHEMA_VERSION
    assert request.request_kind == DeliveryRecoveryRequestKind.RETURN_TO_HOME_SIMULATION
    assert request.request_status == DeliveryRecoveryRequestStatus.READY
    assert (
        request.compiled_from_action
        == DeliveryRecoveryAction.RETURN_TO_HOME_RECOMMENDED
    )
    assert request.fault_category == battery_fault.fault_category
    assert request.executed_against_real_sitl is False
    assert request.recovery_chain_evidence_source == "logic_only_stub"
    assert battery_fault.executed_against_real_sitl is False
    assert battery_fault.recovery_chain_evidence_source == "logic_only_stub"


def test_request_holds_no_execution_authority(recovery_request) -> None:
    request = recovery_request.model_dump(mode="json")
    assert request["request_only"] is True
    assert request["bounded"] is True
    for key in (
        "command_payload_allowed",
        "raw_mavlink_command_allowed",
        "raw_ros_action_allowed",
        "setpoint_stream_allowed",
        "actuator_command_allowed",
        "physical_execution_invoked",
        "hardware_target_allowed",
        "real_hardware_target",
        "approval_free_stronger_execution_allowed",
    ):
        assert request[key] is False, key


def test_request_refuses_command_like_metadata(
    chain: dict, rth_decision, battery_fault
) -> None:
    with pytest.raises(DeliveryRecoveryRequestError):
        build_delivery_recovery_request(
            delivery_mission_contract=chain["contract"],
            delivery_recovery_decision=rth_decision,
            delivery_fault_event=battery_fault,
            operator_minimal_delivery_simulation_status=chain["operator_status"],
            metadata={"return_to_home_command": {"mavlink": "RTL"}},
            now=NOW,
        )


def test_run_records_logic_only_bounded_plan(recovery_run, recovery_request) -> None:
    run = recovery_run.model_dump(mode="json")
    assert run["schema_version"] == DELIVERY_RECOVERY_RUN_SCHEMA_VERSION
    assert run["status"] == DeliveryRecoveryRunStatus.LOGIC_ONLY_RECORDED.value
    assert run["execution_scope"] == "logic_only_stub_recovery_plan"
    assert run["recovery_request_kind"] == recovery_request.request_kind.value
    assert run["mission_item_count"] == 1
    assert [item["command"] for item in run["planned_mission_items"]] == [
        MAV_CMD_NAV_RETURN_TO_LAUNCH
    ]
    assert run["executed_against_real_sitl"] is False
    assert run["recovery_chain_evidence_source"] == "logic_only_stub"
    assert run["logic_only_stub"] is True
    for key in (
        "real_sitl_execution_claimed",
        "mission_upload_performed",
        "external_dispatch_performed",
        "mavlink_dispatch_performed",
        "px4_mission_upload_performed",
        "gazebo_simulator_command_performed",
        "hardware_target_allowed",
        "real_hardware_target",
        "physical_execution_invoked",
        "approval_free_stronger_execution_allowed",
    ):
        assert run[key] is False, key


def test_outcome_is_observed_facts_only_and_gated_from_epic_exit(
    recovery_request, recovery_run
) -> None:
    with _task_store() as (store, task):
        attached = attach_delivery_recovery_outcome(
            task["task_id"],
            delivery_recovery_request=recovery_request,
            delivery_recovery_run=recovery_run,
            observed_facts={
                "safe_landing_event_source": "logic_only_stub",
                "safe_landing_observed": True,
                "mission_terminated_safely": True,
                "vehicle_disarmed_or_landed": True,
            },
            now=NOW,
            task_store_factory=lambda: store,
        )
        stored = store.get(task["task_id"])

    outcome = attached["delivery_recovery_outcome"]
    assert outcome["schema_version"] == DELIVERY_RECOVERY_OUTCOME_SCHEMA_VERSION
    assert (
        outcome["outcome_category"] == DeliveryRecoveryOutcomeCategory.RECOVERED.value
    )
    assert outcome["executed_against_real_sitl"] is False
    assert outcome["recovery_chain_evidence_source"] == "logic_only_stub"
    assert outcome["logic_only_stub"] is True
    assert outcome["real_sitl_execution_claimed"] is False
    assert outcome["real_sitl_chain_required_for_epic_exit"] is True
    assert outcome["observed_facts_only"] is True
    assert outcome["synthetic_success_allowed"] is False
    for key in (
        "command_sent_by_verifier",
        "external_dispatch_performed_by_verifier",
        "mavlink_dispatch_performed_by_verifier",
        "px4_mission_upload_performed_by_verifier",
        "hardware_target_allowed",
        "real_hardware_target",
        "physical_execution_invoked",
        "approval_free_stronger_execution_allowed",
    ):
        assert outcome[key] is False, key
    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"kept": True}
    assert "delivery_recovery_outcome" in stored["artifacts"]


def test_attach_request_and_run_preserve_task_state(
    chain: dict, rth_decision, battery_fault, recovery_request
) -> None:
    with _task_store() as (store, task):
        attach_delivery_recovery_request(
            task["task_id"],
            delivery_mission_contract=chain["contract"],
            delivery_recovery_decision=rth_decision,
            delivery_fault_event=battery_fault,
            operator_minimal_delivery_simulation_status=chain["operator_status"],
            now=NOW,
            task_store_factory=lambda: store,
        )
        attach_delivery_recovery_run(
            task["task_id"],
            delivery_mission_contract=chain["contract"],
            delivery_recovery_request=recovery_request,
            simulator_command_execution_preflight=chain["preflight"],
            simulated_command_proposal=chain["proposal"],
            simulated_command_approval=chain["approval"],
            sitl_session_ref="sitl_session:logic-only-recovery",
            observed_facts={"bounded_recovery_plan_recorded": True},
            started_at=NOW,
            finished_at=NOW,
            task_store_factory=lambda: store,
        )
        stored = store.get(task["task_id"])

    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"kept": True}
    assert "delivery_recovery_request" in stored["artifacts"]
    assert "delivery_recovery_run" in stored["artifacts"]


class _task_store:
    """Context manager yielding a temp-backed TaskStore and a running task."""

    def __enter__(self):
        from tempfile import TemporaryDirectory

        self._tmp = TemporaryDirectory()
        store = TaskStore(f"{self._tmp.name}/tasks.db")
        task = store.create(
            kind="control_supervisor",
            title="delivery recovery chain contract",
            status="running",
            artifacts={"existing": {"kept": True}},
        )
        return store, task

    def __exit__(self, *exc_info):
        self._tmp.cleanup()
        return False
