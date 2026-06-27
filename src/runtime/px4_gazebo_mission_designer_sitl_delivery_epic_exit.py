"""Mission Designer SITL delivery epic-exit artifact.

This layer binds the Mission Designer prompt/approval/Gateway execution chain to
observed PX4/Gazebo SITL upload, flight, payload-release, and dropoff facts. It
does not update the upload/flight execution result into a synthetic delivery
success. Delivery completion is claimed only by this observed-fact exit artifact.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from src.runtime.delivery_mission_contract import DeliveryMissionContract
from src.runtime.px4_gazebo_mission_designer_sitl_runner import (
    PX4GazeboMissionDesignerSITLDropoffVerification,
    PX4GazeboMissionDesignerSITLExecutionResult,
    PX4GazeboMissionDesignerSITLFlightEvidence,
    PX4GazeboMissionDesignerSITLPayloadReleaseObservation,
)
from src.runtime.px4_gazebo_mission_scenario_designer import (
    PX4GazeboBoundedSimulationRequest,
    PX4GazeboMissionDesignerSITLExecutionRequest,
    PX4GazeboMissionScenarioApproval,
    PX4GazeboMissionScenarioProposal,
    PX4GazeboMissionScenarioValidationResult,
)
from src.runtime.px4_gazebo_sitl_dropoff_verification import (
    PX4GazeboSITLDropoffFlightFact,
    PX4GazeboSITLDropoffVerification,
    PX4GazeboSITLDropoffVerificationStatus,
    PX4GazeboSITLPayloadReleaseEvent,
)
from src.runtime.px4_gazebo_sitl_mission_upload import (
    MAV_MISSION_ACCEPTED,
    PX4GazeboSITLMissionUploadReceipt,
    PX4GazeboSITLMissionUploadStatus,
)
from src.runtime.simulated_delivery_command import (
    SimulatorCommandExecutionPreflight,
)
from src.runtime.task_store import TaskStore, get_task_store

PX4_GAZEBO_MISSION_DESIGNER_SITL_DELIVERY_EPIC_EXIT_SCHEMA_VERSION = (
    "px4_gazebo_mission_designer_sitl_delivery_epic_exit.v1"
)


class PX4GazeboMissionDesignerSITLDeliveryEpicExitError(RuntimeError):
    """Raised when the Mission Designer SITL delivery exit is not proven."""


class PX4GazeboMissionDesignerSITLDeliveryEpicExitResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        PX4_GAZEBO_MISSION_DESIGNER_SITL_DELIVERY_EPIC_EXIT_SCHEMA_VERSION
    ] = PX4_GAZEBO_MISSION_DESIGNER_SITL_DELIVERY_EPIC_EXIT_SCHEMA_VERSION
    epic_exit_id: str
    task_ref: str
    prompt: str
    result_status: Literal["mission_designer_sitl_delivery_verified"]
    mission_designer_chain_state: Literal["dropoff-verified"]
    mission_designer_sitl_delivery_epic_exit_complete: Literal[True]
    delivery_success_claimed_from_observed_facts_only: Literal[True]
    prompt_to_scenario_proposal_observed: Literal[True]
    scenario_approval_observed: Literal[True]
    prepared_sitl_execution_request_observed: Literal[True]
    explicit_sitl_execution_approval_observed: Literal[True]
    actual_px4_gazebo_sitl_upload_observed: Literal[True]
    actual_sitl_flight_evidence_observed: Literal[True]
    actual_takeoff_observed: Literal[True]
    actual_dropoff_region_reached: Literal[True]
    actual_land_observed: Literal[True]
    payload_release_observed: Literal[True]
    payload_release_verified: Literal[True]
    dropoff_verified: Literal[True]
    upload_only_delivery_success_rejected: Literal[True]
    missing_flight_delivery_success_rejected: Literal[True]
    missing_payload_release_delivery_success_rejected: Literal[True]
    missing_dropoff_delivery_success_rejected: Literal[True]
    scenario_proposal_ref: str
    scenario_validation_ref: str
    scenario_approval_ref: str
    bounded_simulation_request_ref: str
    execution_request_ref: str
    delivery_mission_contract_ref: str
    simulator_command_execution_preflight_ref: str
    mission_upload_receipt_ref: str
    execution_result_ref: str
    flight_evidence_ref: str
    payload_release_observation_ref: str
    payload_release_event_ref: str
    dropoff_flight_fact_ref: str
    sitl_dropoff_verification_ref: str
    mission_designer_dropoff_verification_ref: str
    delivery_scorecard_ref: str
    delivery_episode_review_ref: str
    autonomy_gate_result_ref: str
    scorecard_review_gate_evidence_source: Literal[
        "simulator_command_execution_preflight_projection"
    ] = "simulator_command_execution_preflight_projection"
    scorecard_review_gate_projected_refs_observed: Literal[True]
    scorecard_passed: Literal[True]
    episode_review_passed: Literal[True]
    autonomy_gate_passed: Literal[True]
    payload_release_event_source: Literal["gazebo_detachable_joint_detach_event"]
    mission_ack_observed: Literal[True]
    mission_ack_type: Literal[MAV_MISSION_ACCEPTED]
    mission_request_sequences: tuple[int, ...]
    dropoff_predicate_mode: Literal[
        "position_in_zone_and_altitude_and_mission_item_and_payload_release"
    ]
    observed_distance_to_dropoff_m: float
    release_distance_to_dropoff_m: float
    release_time_delta_seconds: float
    external_dispatch_performed: Literal[True]
    mavlink_dispatch_performed: Literal[True]
    px4_mission_upload_performed: Literal[True]
    gazebo_simulator_command_performed: Literal[True]
    command_sent_by_verifier: Literal[False] = False
    external_dispatch_performed_by_verifier: Literal[False] = False
    mavlink_dispatch_performed_by_verifier: Literal[False] = False
    px4_mission_upload_performed_by_verifier: Literal[False] = False
    gazebo_entity_mutation_performed_by_verifier: Literal[False] = False
    gazebo_entity_mutation_performed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    ros_dispatch_performed: Literal[False] = False
    actuator_execution_performed: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    synthetic_success_allowed: Literal[False] = False
    observed_at: datetime
    metadata: dict[str, Any]

    @field_validator("mission_request_sequences", mode="before")
    @classmethod
    def _coerce_int_tuple(cls, value: Any) -> tuple[int, ...]:
        return tuple(int(item) for item in (value or ()))

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_exit(self) -> "PX4GazeboMissionDesignerSITLDeliveryEpicExitResult":
        expected_prefixes = {
            "task_ref": "task:",
            "scenario_proposal_ref": "px4_gazebo_mission_scenario_proposal:",
            "scenario_validation_ref": (
                "px4_gazebo_mission_scenario_validation_result:"
            ),
            "scenario_approval_ref": "px4_gazebo_mission_scenario_approval:",
            "bounded_simulation_request_ref": "px4_gazebo_bounded_simulation_request:",
            "execution_request_ref": (
                "px4_gazebo_mission_designer_sitl_execution_request:"
            ),
            "delivery_mission_contract_ref": "delivery_mission_contract:",
            "simulator_command_execution_preflight_ref": (
                "simulator_command_execution_preflight:"
            ),
            "mission_upload_receipt_ref": "px4_gazebo_sitl_mission_upload_receipt:",
            "execution_result_ref": (
                "px4_gazebo_mission_designer_sitl_execution_result:"
            ),
            "flight_evidence_ref": (
                "px4_gazebo_mission_designer_sitl_flight_evidence:"
            ),
            "payload_release_observation_ref": (
                "px4_gazebo_mission_designer_sitl_payload_release_observation:"
            ),
            "payload_release_event_ref": "px4_gazebo_sitl_payload_release_event:",
            "dropoff_flight_fact_ref": "px4_gazebo_sitl_dropoff_flight_fact:",
            "sitl_dropoff_verification_ref": "px4_gazebo_sitl_dropoff_verification:",
            "mission_designer_dropoff_verification_ref": (
                "px4_gazebo_mission_designer_sitl_dropoff_verification:"
            ),
            "delivery_scorecard_ref": "delivery_scorecard:",
            "delivery_episode_review_ref": "delivery_episode_review:",
            "autonomy_gate_result_ref": "autonomy_gate_result:",
        }
        for field_name, prefix in expected_prefixes.items():
            if not str(getattr(self, field_name)).startswith(prefix):
                raise PX4GazeboMissionDesignerSITLDeliveryEpicExitError(
                    f"Mission Designer SITL epic exit requires {field_name}"
                )
        if self.mission_request_sequences != (0, 1, 2, 3):
            raise PX4GazeboMissionDesignerSITLDeliveryEpicExitError(
                "Mission Designer SITL epic exit requires mission request sequence 0..3"
            )
        return self


def _utc(value: datetime | None = None) -> datetime:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        return resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc)


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _artifact_ref(prefix: str, value: str) -> str:
    return f"{prefix}:{value}"


def _artifact(artifacts: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = artifacts.get(key)
    if not isinstance(value, Mapping):
        raise PX4GazeboMissionDesignerSITLDeliveryEpicExitError(
            f"Mission Designer SITL epic exit requires {key}"
        )
    return value


def _require_ref(actual: str, expected: str, label: str) -> None:
    if actual != expected:
        raise PX4GazeboMissionDesignerSITLDeliveryEpicExitError(
            f"Mission Designer SITL epic exit {label} mismatch"
        )


def build_px4_gazebo_mission_designer_sitl_delivery_epic_exit_result(
    *,
    task: Mapping[str, Any],
    prompt: str,
    upload_only_delivery_success_rejected: bool,
    missing_flight_delivery_success_rejected: bool,
    missing_payload_release_delivery_success_rejected: bool,
    missing_dropoff_delivery_success_rejected: bool,
    observed_at: datetime | None = None,
) -> PX4GazeboMissionDesignerSITLDeliveryEpicExitResult:
    """Build the #501 Mission Designer SITL delivery epic-exit result."""

    artifacts_value = task.get("artifacts")
    if not isinstance(artifacts_value, Mapping):
        raise PX4GazeboMissionDesignerSITLDeliveryEpicExitError(
            "Mission Designer SITL epic exit requires task artifacts"
        )
    artifacts = artifacts_value
    if task.get("status") != "completed":
        raise PX4GazeboMissionDesignerSITLDeliveryEpicExitError(
            "Mission Designer SITL epic exit requires completed task"
        )

    proposal = PX4GazeboMissionScenarioProposal.model_validate(
        _artifact(artifacts, "px4_gazebo_mission_scenario_proposal")
    )
    validation = PX4GazeboMissionScenarioValidationResult.model_validate(
        _artifact(artifacts, "px4_gazebo_mission_scenario_validation_result")
    )
    approval = PX4GazeboMissionScenarioApproval.model_validate(
        _artifact(artifacts, "px4_gazebo_mission_scenario_approval")
    )
    bounded_request = PX4GazeboBoundedSimulationRequest.model_validate(
        _artifact(artifacts, "px4_gazebo_bounded_simulation_request")
    )
    execution_request = PX4GazeboMissionDesignerSITLExecutionRequest.model_validate(
        _artifact(artifacts, "px4_gazebo_mission_designer_sitl_execution_request")
    )
    contract = DeliveryMissionContract.model_validate(
        _artifact(artifacts, "delivery_mission_contract")
    )
    preflight = SimulatorCommandExecutionPreflight.model_validate(
        _artifact(artifacts, "simulator_command_execution_preflight")
    )
    receipt = PX4GazeboSITLMissionUploadReceipt.model_validate(
        _artifact(artifacts, "px4_gazebo_sitl_mission_upload_receipt")
    )
    execution_result = PX4GazeboMissionDesignerSITLExecutionResult.model_validate(
        _artifact(artifacts, "px4_gazebo_mission_designer_sitl_execution_result")
    )
    flight_evidence = PX4GazeboMissionDesignerSITLFlightEvidence.model_validate(
        _artifact(artifacts, "px4_gazebo_mission_designer_sitl_flight_evidence")
    )
    payload_observation = (
        PX4GazeboMissionDesignerSITLPayloadReleaseObservation.model_validate(
            _artifact(
                artifacts,
                "px4_gazebo_mission_designer_sitl_payload_release_observation",
            )
        )
    )
    release_event = PX4GazeboSITLPayloadReleaseEvent.model_validate(
        _artifact(artifacts, "px4_gazebo_sitl_payload_release_event")
    )
    dropoff_fact = PX4GazeboSITLDropoffFlightFact.model_validate(
        _artifact(artifacts, "px4_gazebo_sitl_dropoff_flight_fact")
    )
    sitl_dropoff = PX4GazeboSITLDropoffVerification.model_validate(
        _artifact(artifacts, "px4_gazebo_sitl_dropoff_verification")
    )
    mission_designer_dropoff = (
        PX4GazeboMissionDesignerSITLDropoffVerification.model_validate(
            _artifact(
                artifacts,
                "px4_gazebo_mission_designer_sitl_dropoff_verification",
            )
        )
    )

    scenario_proposal_ref = _artifact_ref(
        "px4_gazebo_mission_scenario_proposal", proposal.proposal_id
    )
    scenario_validation_ref = _artifact_ref(
        "px4_gazebo_mission_scenario_validation_result", validation.validation_id
    )
    scenario_approval_ref = _artifact_ref(
        "px4_gazebo_mission_scenario_approval", approval.approval_id
    )
    bounded_request_ref = _artifact_ref(
        "px4_gazebo_bounded_simulation_request", bounded_request.request_id
    )
    execution_request_ref = _artifact_ref(
        "px4_gazebo_mission_designer_sitl_execution_request",
        execution_request.execution_request_id,
    )
    contract_ref = _artifact_ref("delivery_mission_contract", contract.contract_id)
    preflight_ref = _artifact_ref(
        "simulator_command_execution_preflight", preflight.preflight_id
    )
    receipt_ref = _artifact_ref(
        "px4_gazebo_sitl_mission_upload_receipt", receipt.receipt_id
    )
    execution_result_ref = _artifact_ref(
        "px4_gazebo_mission_designer_sitl_execution_result",
        execution_result.result_id,
    )
    flight_ref = _artifact_ref(
        "px4_gazebo_mission_designer_sitl_flight_evidence",
        flight_evidence.flight_evidence_id,
    )
    payload_observation_ref = _artifact_ref(
        "px4_gazebo_mission_designer_sitl_payload_release_observation",
        payload_observation.observation_id,
    )
    release_event_ref = _artifact_ref(
        "px4_gazebo_sitl_payload_release_event", release_event.event_id
    )
    dropoff_fact_ref = _artifact_ref(
        "px4_gazebo_sitl_dropoff_flight_fact", dropoff_fact.fact_id
    )
    sitl_dropoff_ref = _artifact_ref(
        "px4_gazebo_sitl_dropoff_verification", sitl_dropoff.verification_id
    )
    mission_designer_dropoff_ref = _artifact_ref(
        "px4_gazebo_mission_designer_sitl_dropoff_verification",
        mission_designer_dropoff.verification_id,
    )

    for actual, expected, label in (
        (execution_request.scenario_proposal_ref, scenario_proposal_ref, "proposal"),
        (execution_request.validation_ref, scenario_validation_ref, "validation"),
        (execution_request.approval_ref, scenario_approval_ref, "approval"),
        (
            execution_request.bounded_simulation_request_ref,
            bounded_request_ref,
            "bounded request",
        ),
        (execution_result.execution_request_ref, execution_request_ref, "request"),
        (
            execution_result.delivery_mission_contract_ref,
            contract_ref,
            "contract",
        ),
        (
            execution_result.simulator_command_execution_preflight_ref,
            preflight_ref,
            "preflight",
        ),
        (
            execution_result.px4_gazebo_sitl_mission_upload_receipt_ref,
            receipt_ref,
            "receipt",
        ),
        (
            flight_evidence.execution_request_ref,
            execution_request_ref,
            "flight request",
        ),
        (
            flight_evidence.delivery_mission_contract_ref,
            contract_ref,
            "flight contract",
        ),
        (
            flight_evidence.px4_gazebo_sitl_mission_upload_receipt_ref,
            receipt_ref,
            "flight receipt",
        ),
        (
            payload_observation.execution_result_ref,
            execution_result_ref,
            "payload result",
        ),
        (payload_observation.flight_evidence_ref, flight_ref, "payload flight"),
        (
            payload_observation.payload_release_event_ref,
            release_event_ref,
            "payload release event",
        ),
        (
            mission_designer_dropoff.execution_result_ref,
            execution_result_ref,
            "dropoff result",
        ),
        (
            mission_designer_dropoff.flight_evidence_ref,
            flight_ref,
            "dropoff flight",
        ),
        (
            mission_designer_dropoff.payload_release_observation_ref,
            payload_observation_ref,
            "dropoff payload observation",
        ),
        (
            mission_designer_dropoff.payload_release_event_ref,
            release_event_ref,
            "dropoff release event",
        ),
        (
            mission_designer_dropoff.dropoff_flight_fact_ref,
            dropoff_fact_ref,
            "dropoff flight fact",
        ),
        (
            mission_designer_dropoff.sitl_dropoff_verification_ref,
            sitl_dropoff_ref,
            "SITL dropoff verification",
        ),
        (dropoff_fact.payload_release_event_ref, release_event_ref, "fact release"),
        (
            dropoff_fact.sitl_mission_upload_receipt_ref,
            receipt_ref,
            "fact receipt",
        ),
        (sitl_dropoff.dropoff_flight_fact_ref, dropoff_fact_ref, "SITL fact"),
        (sitl_dropoff.payload_release_event_ref, release_event_ref, "SITL release"),
        (sitl_dropoff.sitl_mission_upload_receipt_ref, receipt_ref, "SITL receipt"),
    ):
        _require_ref(actual, expected, label)

    if receipt.upload_status is not PX4GazeboSITLMissionUploadStatus.UPLOADED:
        raise PX4GazeboMissionDesignerSITLDeliveryEpicExitError(
            "Mission Designer SITL epic exit requires uploaded receipt"
        )
    if receipt.mission_ack_observed is not True or receipt.mission_ack_type != 0:
        raise PX4GazeboMissionDesignerSITLDeliveryEpicExitError(
            "Mission Designer SITL epic exit requires observed accepted ACK"
        )
    if tuple(receipt.mission_request_sequences) != (0, 1, 2, 3):
        raise PX4GazeboMissionDesignerSITLDeliveryEpicExitError(
            "Mission Designer SITL epic exit requires observed request sequences"
        )
    if (
        execution_result.result_status
        != "flight_evidence_observed_payload_dropoff_pending"
    ):
        raise PX4GazeboMissionDesignerSITLDeliveryEpicExitError(
            "Mission Designer SITL epic exit requires flight-evidence result status"
        )
    if execution_result.payload_release_observed or execution_result.dropoff_verified:
        raise PX4GazeboMissionDesignerSITLDeliveryEpicExitError(
            "Mission Designer SITL execution result must not synthesize delivery success"
        )
    if flight_evidence.actual_sitl_flight_evidence_observed is not True:
        raise PX4GazeboMissionDesignerSITLDeliveryEpicExitError(
            "Mission Designer SITL epic exit requires observed flight evidence"
        )
    if payload_observation.payload_release_observed is not True:
        raise PX4GazeboMissionDesignerSITLDeliveryEpicExitError(
            "Mission Designer SITL epic exit requires observed payload release"
        )
    if payload_observation.event_source != "gazebo_detachable_joint_detach_event":
        raise PX4GazeboMissionDesignerSITLDeliveryEpicExitError(
            "Mission Designer SITL epic exit requires Gazebo detachable-joint source"
        )
    if release_event.event_source != payload_observation.event_source:
        raise PX4GazeboMissionDesignerSITLDeliveryEpicExitError(
            "Mission Designer SITL epic exit release source mismatch"
        )
    if sitl_dropoff.status is not PX4GazeboSITLDropoffVerificationStatus.VERIFIED:
        raise PX4GazeboMissionDesignerSITLDeliveryEpicExitError(
            "Mission Designer SITL epic exit requires verified SITL dropoff"
        )
    if not (
        sitl_dropoff.dropoff_verified and mission_designer_dropoff.dropoff_verified
    ):
        raise PX4GazeboMissionDesignerSITLDeliveryEpicExitError(
            "Mission Designer SITL epic exit requires verified dropoff chain"
        )
    if not (
        preflight.scorecard_passed
        and preflight.episode_review_passed
        and preflight.autonomy_gate_passed
    ):
        raise PX4GazeboMissionDesignerSITLDeliveryEpicExitError(
            "Mission Designer SITL epic exit requires scorecard/review/gate refs"
        )
    if not (
        upload_only_delivery_success_rejected
        and missing_flight_delivery_success_rejected
        and missing_payload_release_delivery_success_rejected
        and missing_dropoff_delivery_success_rejected
    ):
        raise PX4GazeboMissionDesignerSITLDeliveryEpicExitError(
            "Mission Designer SITL epic exit requires negative proof cases"
        )

    payload = {
        "task_id": task.get("task_id"),
        "execution_result_ref": execution_result_ref,
        "flight_ref": flight_ref,
        "payload_ref": payload_observation_ref,
        "dropoff_ref": mission_designer_dropoff_ref,
        "sitl_dropoff_ref": sitl_dropoff_ref,
    }
    return PX4GazeboMissionDesignerSITLDeliveryEpicExitResult(
        epic_exit_id=_stable_id(
            "px4_gazebo_mission_designer_sitl_delivery_epic_exit",
            payload,
        ),
        task_ref=f"task:{task.get('task_id')}",
        prompt=prompt,
        result_status="mission_designer_sitl_delivery_verified",
        mission_designer_chain_state="dropoff-verified",
        mission_designer_sitl_delivery_epic_exit_complete=True,
        delivery_success_claimed_from_observed_facts_only=True,
        prompt_to_scenario_proposal_observed=True,
        scenario_approval_observed=True,
        prepared_sitl_execution_request_observed=True,
        explicit_sitl_execution_approval_observed=True,
        actual_px4_gazebo_sitl_upload_observed=True,
        actual_sitl_flight_evidence_observed=True,
        actual_takeoff_observed=True,
        actual_dropoff_region_reached=True,
        actual_land_observed=True,
        payload_release_observed=True,
        payload_release_verified=True,
        dropoff_verified=True,
        upload_only_delivery_success_rejected=True,
        missing_flight_delivery_success_rejected=True,
        missing_payload_release_delivery_success_rejected=True,
        missing_dropoff_delivery_success_rejected=True,
        scenario_proposal_ref=scenario_proposal_ref,
        scenario_validation_ref=scenario_validation_ref,
        scenario_approval_ref=scenario_approval_ref,
        bounded_simulation_request_ref=bounded_request_ref,
        execution_request_ref=execution_request_ref,
        delivery_mission_contract_ref=contract_ref,
        simulator_command_execution_preflight_ref=preflight_ref,
        mission_upload_receipt_ref=receipt_ref,
        execution_result_ref=execution_result_ref,
        flight_evidence_ref=flight_ref,
        payload_release_observation_ref=payload_observation_ref,
        payload_release_event_ref=release_event_ref,
        dropoff_flight_fact_ref=dropoff_fact_ref,
        sitl_dropoff_verification_ref=sitl_dropoff_ref,
        mission_designer_dropoff_verification_ref=mission_designer_dropoff_ref,
        delivery_scorecard_ref=preflight.delivery_scorecard_ref,
        delivery_episode_review_ref=preflight.delivery_episode_review_ref,
        autonomy_gate_result_ref=preflight.autonomy_gate_result_ref,
        scorecard_review_gate_projected_refs_observed=True,
        scorecard_passed=True,
        episode_review_passed=True,
        autonomy_gate_passed=True,
        payload_release_event_source="gazebo_detachable_joint_detach_event",
        mission_ack_observed=True,
        mission_ack_type=MAV_MISSION_ACCEPTED,
        mission_request_sequences=tuple(receipt.mission_request_sequences),
        dropoff_predicate_mode=mission_designer_dropoff.predicate_mode,
        observed_distance_to_dropoff_m=(
            mission_designer_dropoff.observed_distance_to_dropoff_m
        ),
        release_distance_to_dropoff_m=(
            mission_designer_dropoff.release_distance_to_dropoff_m
        ),
        release_time_delta_seconds=mission_designer_dropoff.release_time_delta_seconds,
        external_dispatch_performed=True,
        mavlink_dispatch_performed=True,
        px4_mission_upload_performed=True,
        gazebo_simulator_command_performed=receipt.gazebo_simulator_command_performed,
        observed_at=_utc(observed_at),
        metadata={
            "source": "px4_gazebo_mission_designer_sitl_delivery_epic_exit",
            "issue": "#501",
            "execution_result_still_payload_dropoff_pending": True,
            "ui_chain_state_supported": "dropoff-verified",
            "scorecard_review_gate_refs_projected_by_preflight": True,
        },
    )


def attach_px4_gazebo_mission_designer_sitl_delivery_epic_exit_result(
    task_id: str,
    *,
    prompt: str,
    upload_only_delivery_success_rejected: bool,
    missing_flight_delivery_success_rejected: bool,
    missing_payload_release_delivery_success_rejected: bool,
    missing_dropoff_delivery_success_rejected: bool,
    task_store_factory: Callable[[], TaskStore] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    store = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise PX4GazeboMissionDesignerSITLDeliveryEpicExitError(
            f"task {task_id} not found; cannot attach Mission Designer SITL exit"
        )
    result = build_px4_gazebo_mission_designer_sitl_delivery_epic_exit_result(
        task=current,
        prompt=prompt,
        upload_only_delivery_success_rejected=upload_only_delivery_success_rejected,
        missing_flight_delivery_success_rejected=(
            missing_flight_delivery_success_rejected
        ),
        missing_payload_release_delivery_success_rejected=(
            missing_payload_release_delivery_success_rejected
        ),
        missing_dropoff_delivery_success_rejected=(
            missing_dropoff_delivery_success_rejected
        ),
        observed_at=now,
    )
    updated = store.update(
        task_id,
        artifacts={
            "px4_gazebo_mission_designer_sitl_delivery_epic_exit": (
                result.model_dump(mode="json")
            )
        },
        metadata={
            "mission_designer_sitl_delivery_epic_exit_complete": True,
            "mission_designer_chain_state": result.mission_designer_chain_state,
            "delivery_success_claimed_from_observed_facts_only": True,
            "synthetic_success_allowed": False,
        },
    )
    if updated is None:
        raise PX4GazeboMissionDesignerSITLDeliveryEpicExitError(
            f"task {task_id} disappeared while attaching Mission Designer SITL exit"
        )
    return {
        "task": updated,
        "px4_gazebo_mission_designer_sitl_delivery_epic_exit": result.model_dump(
            mode="json"
        ),
        "summary": {
            "task_id": updated["task_id"],
            "task_status": updated["status"],
            "mission_designer_sitl_delivery_epic_exit_complete": (
                result.mission_designer_sitl_delivery_epic_exit_complete
            ),
            "mission_designer_chain_state": result.mission_designer_chain_state,
            "payload_release_observed": result.payload_release_observed,
            "payload_release_verified": result.payload_release_verified,
            "dropoff_verified": result.dropoff_verified,
            "delivery_scorecard_ref": result.delivery_scorecard_ref,
            "delivery_episode_review_ref": result.delivery_episode_review_ref,
            "autonomy_gate_result_ref": result.autonomy_gate_result_ref,
            "hardware_target_allowed": result.hardware_target_allowed,
            "physical_execution_invoked": result.physical_execution_invoked,
            "ros_dispatch_performed": result.ros_dispatch_performed,
            "actuator_execution_performed": result.actuator_execution_performed,
            "synthetic_success_allowed": result.synthetic_success_allowed,
        },
    }


__all__ = [
    "PX4_GAZEBO_MISSION_DESIGNER_SITL_DELIVERY_EPIC_EXIT_SCHEMA_VERSION",
    "PX4GazeboMissionDesignerSITLDeliveryEpicExitError",
    "PX4GazeboMissionDesignerSITLDeliveryEpicExitResult",
    "attach_px4_gazebo_mission_designer_sitl_delivery_epic_exit_result",
    "build_px4_gazebo_mission_designer_sitl_delivery_epic_exit_result",
]
