"""Bounded recovery-decision to SITL mission upload loop."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.delivery_mission_contract import DeliveryMissionContract
from src.runtime.delivery_recovery_decision import DeliveryRecoveryDecision
from src.runtime.px4_gazebo_sitl_mission_upload import (
    MAV_CMD_NAV_LAND,
    MAV_CMD_NAV_RETURN_TO_LAUNCH,
    MAV_CMD_NAV_WAYPOINT,
    SITL_MISSION_UPLOAD_ABSOLUTE_GEOFENCE_RADIUS_M,
    SITL_MISSION_UPLOAD_DEFAULT_GEOFENCE_RADIUS_M,
    PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT,
    PX4GazeboSITLMissionItem,
    PX4GazeboSITLMissionUploadReceipt,
    PX4GazeboSITLMissionUploader,
    build_px4_gazebo_sitl_mission_upload_receipt,
)
from src.runtime.simulated_delivery_command import (
    SimulatedCommandApproval,
    SimulatedCommandProposal,
    SimulatorCommandExecutionPreflight,
)
from src.runtime.task_store import TaskStore, get_task_store

PX4_GAZEBO_SITL_RECOVERY_UPLOAD_ITERATION_SCHEMA_VERSION = (
    "px4_gazebo_sitl_recovery_upload_iteration.v1"
)
PX4_GAZEBO_SITL_RECOVERY_UPLOAD_LOOP_SCHEMA_VERSION = (
    "px4_gazebo_sitl_recovery_upload_loop.v1"
)

SITL_RECOVERY_UPLOAD_DEFAULT_MAX_ITERATIONS = 1
SITL_RECOVERY_UPLOAD_ABSOLUTE_MAX_ITERATIONS = 3
SITL_RECOVERY_UPLOAD_DEFAULT_GEOFENCE_RADIUS_M = (
    SITL_MISSION_UPLOAD_DEFAULT_GEOFENCE_RADIUS_M
)
SITL_RECOVERY_UPLOAD_ABSOLUTE_GEOFENCE_RADIUS_M = (
    SITL_MISSION_UPLOAD_ABSOLUTE_GEOFENCE_RADIUS_M
)


class PX4GazeboSITLRecoveryUploadLoopError(RuntimeError):
    """Raised when a recovery upload loop cannot proceed safely."""


class PX4GazeboSITLRecoveryUploadAction(str, Enum):
    RETURN_TO_HOME_MISSION = "return_to_home_mission"
    HOLD_MISSION = "hold_mission"
    REROUTE_MISSION = "reroute_mission"
    ALTERNATE_LANDING_MISSION = "alternate_landing_mission"
    OPERATOR_ESCALATION_REQUIRED = "operator_escalation_required"


class PX4GazeboSITLRecoveryUploadLoopStatus(str, Enum):
    UPLOADED = "uploaded"
    BLOCKED = "blocked"
    OPERATOR_ESCALATION_REQUIRED = "operator_escalation_required"


class PX4GazeboSITLRecoveryUploadIteration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        PX4_GAZEBO_SITL_RECOVERY_UPLOAD_ITERATION_SCHEMA_VERSION
    ] = PX4_GAZEBO_SITL_RECOVERY_UPLOAD_ITERATION_SCHEMA_VERSION
    iteration_id: str
    iteration_index: int = Field(ge=0)
    recovery_action: PX4GazeboSITLRecoveryUploadAction
    delivery_recovery_decision_ref: str
    previous_receipt_ref: str = ""
    mission_upload_receipt_ref: str = ""
    mission_item_count: int = Field(ge=0)
    blocked_reasons: tuple[str, ...] = ()
    external_dispatch_performed: bool
    mavlink_dispatch_performed: bool
    px4_mission_upload_performed: bool
    operator_escalation_required: bool = False
    physical_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    gazebo_entity_mutation_performed: Literal[False] = False
    ros_dispatch_performed: Literal[False] = False
    actuator_execution_performed: Literal[False] = False
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("blocked_reasons", mode="before")
    @classmethod
    def _coerce_blocked_reasons(cls, value: Any) -> tuple[str, ...]:
        return tuple(str(item) for item in value or ())

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_created_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


class PX4GazeboSITLRecoveryUploadLoop(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PX4_GAZEBO_SITL_RECOVERY_UPLOAD_LOOP_SCHEMA_VERSION] = (
        PX4_GAZEBO_SITL_RECOVERY_UPLOAD_LOOP_SCHEMA_VERSION
    )
    loop_id: str
    delivery_mission_contract_ref: str
    delivery_recovery_decision_ref: str
    simulator_command_execution_preflight_ref: str
    simulated_command_proposal_ref: str
    simulated_command_approval_ref: str
    status: PX4GazeboSITLRecoveryUploadLoopStatus
    selected_action: PX4GazeboSITLRecoveryUploadAction
    iterations: tuple[PX4GazeboSITLRecoveryUploadIteration, ...]
    receipt_refs: tuple[str, ...]
    bounded_iteration_count: int = Field(ge=0)
    max_iterations: int = Field(ge=1)
    operator_escalation_required: bool
    blocked_reasons: tuple[str, ...] = ()
    external_dispatch_performed: bool
    mavlink_dispatch_performed: bool
    px4_mission_upload_performed: bool
    rule_based: Literal[True] = True
    llm_judge_used: Literal[False] = False
    sitl_only: Literal[True] = True
    target_endpoint: Literal[PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT]
    physical_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    gazebo_entity_mutation_performed: Literal[False] = False
    ros_dispatch_performed: Literal[False] = False
    actuator_execution_performed: Literal[False] = False
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("blocked_reasons", "receipt_refs", "iterations", mode="before")
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[Any, ...]:
        return tuple(value or ())

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_created_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_loop(self) -> "PX4GazeboSITLRecoveryUploadLoop":
        if self.bounded_iteration_count != len(self.iterations):
            raise PX4GazeboSITLRecoveryUploadLoopError("iteration count mismatch")
        if self.status is PX4GazeboSITLRecoveryUploadLoopStatus.UPLOADED:
            if not self.external_dispatch_performed:
                raise PX4GazeboSITLRecoveryUploadLoopError(
                    "uploaded loop requires external dispatch"
                )
            if self.operator_escalation_required:
                raise PX4GazeboSITLRecoveryUploadLoopError(
                    "uploaded loop cannot require escalation"
                )
        else:
            if self.external_dispatch_performed:
                raise PX4GazeboSITLRecoveryUploadLoopError(
                    "blocked/escalated loop cannot dispatch"
                )
            if not self.blocked_reasons:
                raise PX4GazeboSITLRecoveryUploadLoopError(
                    "blocked/escalated loop requires reasons"
                )
        return self


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _contract_ref(contract: DeliveryMissionContract) -> str:
    return f"delivery_mission_contract:{contract.contract_id}"


def _decision_ref(decision: DeliveryRecoveryDecision) -> str:
    return f"delivery_recovery_decision:{decision.decision_id}"


def _preflight_ref(preflight: SimulatorCommandExecutionPreflight) -> str:
    return f"simulator_command_execution_preflight:{preflight.preflight_id}"


def _proposal_ref(proposal: SimulatedCommandProposal) -> str:
    return f"simulated_command_proposal:{proposal.proposal_id}"


def _approval_ref(approval: SimulatedCommandApproval) -> str:
    return f"simulated_command_approval:{approval.approval_id}"


def _receipt_ref(receipt: PX4GazeboSITLMissionUploadReceipt) -> str:
    return f"px4_gazebo_sitl_mission_upload_receipt:{receipt.receipt_id}"


def _receipt_ref_from_artifact(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    receipt_id = str(value.get("receipt_id") or "")
    schema_version = str(value.get("schema_version") or "")
    if not receipt_id or schema_version != "px4_gazebo_sitl_mission_upload_receipt.v1":
        return ""
    return f"px4_gazebo_sitl_mission_upload_receipt:{receipt_id}"


def _task_known_receipt_refs(task: Mapping[str, Any]) -> tuple[str, ...]:
    artifacts = task.get("artifacts") if isinstance(task, Mapping) else None
    if not isinstance(artifacts, Mapping):
        return ()
    refs: list[str] = []
    receipt_ref = _receipt_ref_from_artifact(
        artifacts.get("px4_gazebo_sitl_mission_upload_receipt")
    )
    if receipt_ref:
        refs.append(receipt_ref)
    loop = artifacts.get("px4_gazebo_sitl_recovery_upload_loop")
    if isinstance(loop, Mapping):
        refs.extend(str(ref) for ref in loop.get("receipt_refs") or ())
        for iteration in loop.get("iterations") or ():
            if isinstance(iteration, Mapping):
                refs.extend(
                    str(ref)
                    for ref in (
                        iteration.get("previous_receipt_ref"),
                        iteration.get("mission_upload_receipt_ref"),
                    )
                    if ref
                )
    return tuple(dict.fromkeys(refs))


def validate_previous_receipt_refs_for_task(
    task: Mapping[str, Any],
    previous_receipt_refs: Sequence[str] | None,
) -> tuple[str, ...]:
    requested_refs = tuple(str(ref) for ref in previous_receipt_refs or ())
    if not requested_refs:
        return ()
    known_refs = set(_task_known_receipt_refs(task))
    unknown_refs = tuple(ref for ref in requested_refs if ref not in known_refs)
    if unknown_refs:
        raise PX4GazeboSITLRecoveryUploadLoopError(
            "previous_receipt_ref_not_found_in_task"
        )
    return requested_refs


def _to_contract(value: DeliveryMissionContract | Mapping[str, Any]):
    return (
        value
        if isinstance(value, DeliveryMissionContract)
        else DeliveryMissionContract.model_validate(dict(value))
    )


def _to_decision(value: DeliveryRecoveryDecision | Mapping[str, Any]):
    return (
        value
        if isinstance(value, DeliveryRecoveryDecision)
        else DeliveryRecoveryDecision.model_validate(dict(value))
    )


def _to_preflight(value: SimulatorCommandExecutionPreflight | Mapping[str, Any]):
    return (
        value
        if isinstance(value, SimulatorCommandExecutionPreflight)
        else SimulatorCommandExecutionPreflight.model_validate(dict(value))
    )


def _to_proposal(value: SimulatedCommandProposal | Mapping[str, Any]):
    return (
        value
        if isinstance(value, SimulatedCommandProposal)
        else SimulatedCommandProposal.model_validate(dict(value))
    )


def _to_approval(value: SimulatedCommandApproval | Mapping[str, Any]):
    return (
        value
        if isinstance(value, SimulatedCommandApproval)
        else SimulatedCommandApproval.model_validate(dict(value))
    )


def _bounded_iteration_count(requested: int) -> int:
    return max(1, min(int(requested), SITL_RECOVERY_UPLOAD_ABSOLUTE_MAX_ITERATIONS))


def _bounded_recovery_geofence_radius(requested: float) -> float:
    return max(
        1.0,
        min(float(requested), SITL_RECOVERY_UPLOAD_ABSOLUTE_GEOFENCE_RADIUS_M),
    )


def _recovery_geofence_radius(contract: DeliveryMissionContract) -> float:
    return _bounded_recovery_geofence_radius(
        contract.metadata.get(
            "recovery_geofence_radius_m",
            SITL_RECOVERY_UPLOAD_DEFAULT_GEOFENCE_RADIUS_M,
        )
    )


def _selected_action(
    decision: DeliveryRecoveryDecision,
) -> PX4GazeboSITLRecoveryUploadAction:
    if decision.return_to_home_recommended:
        return PX4GazeboSITLRecoveryUploadAction.RETURN_TO_HOME_MISSION
    if decision.alternate_landing_proposal:
        return PX4GazeboSITLRecoveryUploadAction.ALTERNATE_LANDING_MISSION
    if decision.reroute_proposal:
        return PX4GazeboSITLRecoveryUploadAction.REROUTE_MISSION
    if decision.hold_recommended or decision.hold_proposed:
        return PX4GazeboSITLRecoveryUploadAction.HOLD_MISSION
    return PX4GazeboSITLRecoveryUploadAction.OPERATOR_ESCALATION_REQUIRED


def _home(contract: DeliveryMissionContract) -> tuple[float, float]:
    return (contract.pickup_location.latitude, contract.pickup_location.longitude)


def _dropoff(contract: DeliveryMissionContract) -> tuple[float, float]:
    return (contract.dropoff_location.latitude, contract.dropoff_location.longitude)


def _recovery_items(
    *,
    contract: DeliveryMissionContract,
    action: PX4GazeboSITLRecoveryUploadAction,
) -> tuple[PX4GazeboSITLMissionItem, ...]:
    home_lat, home_lon = _home(contract)
    drop_lat, drop_lon = _dropoff(contract)
    midpoint_lat = (home_lat + drop_lat) / 2.0
    midpoint_lon = (home_lon + drop_lon) / 2.0
    if action is PX4GazeboSITLRecoveryUploadAction.RETURN_TO_HOME_MISSION:
        return (
            PX4GazeboSITLMissionItem(
                seq=0,
                command=MAV_CMD_NAV_RETURN_TO_LAUNCH,
                latitude_deg=home_lat,
                longitude_deg=home_lon,
                altitude_m=0.0,
                current=1,
            ),
        )
    if action is PX4GazeboSITLRecoveryUploadAction.HOLD_MISSION:
        return (
            PX4GazeboSITLMissionItem(
                seq=0,
                command=MAV_CMD_NAV_WAYPOINT,
                latitude_deg=home_lat,
                longitude_deg=home_lon,
                altitude_m=20.0,
                current=1,
            ),
        )
    if action is PX4GazeboSITLRecoveryUploadAction.ALTERNATE_LANDING_MISSION:
        return (
            PX4GazeboSITLMissionItem(
                seq=0,
                command=MAV_CMD_NAV_LAND,
                latitude_deg=home_lat,
                longitude_deg=home_lon,
                altitude_m=0.0,
                current=1,
            ),
        )
    if action is PX4GazeboSITLRecoveryUploadAction.REROUTE_MISSION:
        return (
            PX4GazeboSITLMissionItem(
                seq=0,
                command=MAV_CMD_NAV_WAYPOINT,
                latitude_deg=midpoint_lat,
                longitude_deg=midpoint_lon,
                altitude_m=30.0,
                current=1,
            ),
            PX4GazeboSITLMissionItem(
                seq=1,
                command=MAV_CMD_NAV_WAYPOINT,
                latitude_deg=drop_lat,
                longitude_deg=drop_lon,
                altitude_m=30.0,
            ),
            PX4GazeboSITLMissionItem(
                seq=2,
                command=MAV_CMD_NAV_LAND,
                latitude_deg=drop_lat,
                longitude_deg=drop_lon,
                altitude_m=0.0,
            ),
        )
    return ()


def build_px4_gazebo_sitl_recovery_upload_loop(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    delivery_recovery_decision: DeliveryRecoveryDecision | Mapping[str, Any],
    simulator_command_execution_preflight: (
        SimulatorCommandExecutionPreflight | Mapping[str, Any]
    ),
    simulated_command_proposal: SimulatedCommandProposal | Mapping[str, Any],
    simulated_command_approval: SimulatedCommandApproval | Mapping[str, Any],
    target_endpoint: str = PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT,
    allow_sitl_mission_upload: bool = True,
    uploader: PX4GazeboSITLMissionUploader | None = None,
    requested_max_iterations: int = SITL_RECOVERY_UPLOAD_DEFAULT_MAX_ITERATIONS,
    previous_receipt_refs: Sequence[str] | None = None,
    now: datetime | None = None,
) -> tuple[PX4GazeboSITLRecoveryUploadLoop, PX4GazeboSITLMissionUploadReceipt | None]:
    contract = _to_contract(delivery_mission_contract)
    decision = _to_decision(delivery_recovery_decision)
    preflight = _to_preflight(simulator_command_execution_preflight)
    proposal = _to_proposal(simulated_command_proposal)
    approval = _to_approval(simulated_command_approval)
    created_at = _utc(now)
    max_iterations = _bounded_iteration_count(requested_max_iterations)
    recovery_geofence_radius_m = _recovery_geofence_radius(contract)
    previous_refs = tuple(str(ref) for ref in previous_receipt_refs or ())
    blocked: list[str] = []
    if len(previous_refs) >= max_iterations:
        blocked.append("recovery_upload_iteration_limit_exhausted")
    if decision.delivery_mission_contract_id != contract.contract_id:
        blocked.append("decision_contract_ref_mismatch")
    if proposal.delivery_mission_contract_ref != _contract_ref(contract):
        blocked.append("proposal_contract_ref_mismatch")
    selected_action = _selected_action(decision)
    if (
        selected_action
        is PX4GazeboSITLRecoveryUploadAction.OPERATOR_ESCALATION_REQUIRED
    ):
        blocked.append("recovery_decision_has_no_uploadable_action")
    receipt: PX4GazeboSITLMissionUploadReceipt | None = None
    mission_items = _recovery_items(contract=contract, action=selected_action)
    if not blocked:
        receipt = build_px4_gazebo_sitl_mission_upload_receipt(
            delivery_mission_contract=contract,
            simulator_command_execution_preflight=preflight,
            simulated_command_proposal=proposal,
            simulated_command_approval=approval,
            target_endpoint=target_endpoint,
            allow_sitl_mission_upload=allow_sitl_mission_upload,
            uploader=uploader,
            mission_items_override=mission_items,
            geofence_radius_m=recovery_geofence_radius_m,
            now=created_at,
        )
        if receipt.upload_status.value != "uploaded":
            blocked.extend(receipt.blocked_reasons or ("mission_upload_not_uploaded",))
    uploaded = (
        receipt is not None
        and receipt.upload_status.value == "uploaded"
        and not blocked
    )
    iteration = PX4GazeboSITLRecoveryUploadIteration(
        iteration_id=_stable_id(
            "px4_gazebo_sitl_recovery_upload_iteration",
            {
                "decision": decision.decision_id,
                "previous": previous_refs,
                "action": selected_action.value,
                "receipt": receipt.receipt_id if receipt else "",
            },
        ),
        iteration_index=len(previous_refs),
        recovery_action=selected_action,
        delivery_recovery_decision_ref=_decision_ref(decision),
        previous_receipt_ref=previous_refs[-1] if previous_refs else "",
        mission_upload_receipt_ref=_receipt_ref(receipt) if receipt else "",
        mission_item_count=len(mission_items),
        blocked_reasons=tuple(dict.fromkeys(blocked)),
        external_dispatch_performed=uploaded,
        mavlink_dispatch_performed=uploaded,
        px4_mission_upload_performed=uploaded,
        operator_escalation_required=bool(blocked),
        created_at=created_at,
        metadata={
            "new_iteration_ref_chain": True,
            "previous_receipt_refs": list(previous_refs),
        },
    )
    status = (
        PX4GazeboSITLRecoveryUploadLoopStatus.UPLOADED
        if uploaded
        else PX4GazeboSITLRecoveryUploadLoopStatus.OPERATOR_ESCALATION_REQUIRED
    )
    receipt_refs = tuple(
        [*previous_refs, *([_receipt_ref(receipt)] if receipt else [])]
    )
    payload = {
        "contract": contract.contract_id,
        "decision": decision.decision_id,
        "preflight": preflight.preflight_id,
        "action": selected_action.value,
        "previous": previous_refs,
        "receipt": receipt.receipt_id if receipt else "",
        "blocked": iteration.blocked_reasons,
    }
    loop = PX4GazeboSITLRecoveryUploadLoop(
        loop_id=_stable_id("px4_gazebo_sitl_recovery_upload_loop", payload),
        delivery_mission_contract_ref=_contract_ref(contract),
        delivery_recovery_decision_ref=_decision_ref(decision),
        simulator_command_execution_preflight_ref=_preflight_ref(preflight),
        simulated_command_proposal_ref=_proposal_ref(proposal),
        simulated_command_approval_ref=_approval_ref(approval),
        status=status,
        selected_action=selected_action,
        iterations=(iteration,),
        receipt_refs=receipt_refs,
        bounded_iteration_count=1,
        max_iterations=max_iterations,
        operator_escalation_required=bool(blocked),
        blocked_reasons=iteration.blocked_reasons,
        external_dispatch_performed=uploaded,
        mavlink_dispatch_performed=uploaded,
        px4_mission_upload_performed=uploaded,
        target_endpoint=PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT,
        created_at=created_at,
        metadata={
            "bounded_iterations": True,
            "default_max_iterations": SITL_RECOVERY_UPLOAD_DEFAULT_MAX_ITERATIONS,
            "absolute_max_iterations": SITL_RECOVERY_UPLOAD_ABSOLUTE_MAX_ITERATIONS,
            "decision_to_upload_mapping": selected_action.value,
            "recovery_geofence_radius_m": recovery_geofence_radius_m,
            "default_recovery_geofence_radius_m": (
                SITL_RECOVERY_UPLOAD_DEFAULT_GEOFENCE_RADIUS_M
            ),
            "absolute_recovery_geofence_radius_m": (
                SITL_RECOVERY_UPLOAD_ABSOLUTE_GEOFENCE_RADIUS_M
            ),
        },
    )
    return loop, receipt


def attach_px4_gazebo_sitl_recovery_upload_loop(
    task_id: str,
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    delivery_recovery_decision: DeliveryRecoveryDecision | Mapping[str, Any],
    simulator_command_execution_preflight: (
        SimulatorCommandExecutionPreflight | Mapping[str, Any]
    ),
    simulated_command_proposal: SimulatedCommandProposal | Mapping[str, Any],
    simulated_command_approval: SimulatedCommandApproval | Mapping[str, Any],
    uploader: PX4GazeboSITLMissionUploader | None = None,
    requested_max_iterations: int = SITL_RECOVERY_UPLOAD_DEFAULT_MAX_ITERATIONS,
    previous_receipt_refs: Sequence[str] | None = None,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    store = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise PX4GazeboSITLRecoveryUploadLoopError(
            f"task {task_id} not found; cannot attach recovery upload loop"
        )
    validated_previous_refs = validate_previous_receipt_refs_for_task(
        current,
        previous_receipt_refs,
    )
    loop, receipt = build_px4_gazebo_sitl_recovery_upload_loop(
        delivery_mission_contract=delivery_mission_contract,
        delivery_recovery_decision=delivery_recovery_decision,
        simulator_command_execution_preflight=simulator_command_execution_preflight,
        simulated_command_proposal=simulated_command_proposal,
        simulated_command_approval=simulated_command_approval,
        uploader=uploader,
        requested_max_iterations=requested_max_iterations,
        previous_receipt_refs=validated_previous_refs,
        now=now,
    )
    artifacts = {"px4_gazebo_sitl_recovery_upload_loop": loop.model_dump(mode="json")}
    if receipt is not None:
        artifacts["px4_gazebo_sitl_mission_upload_receipt"] = receipt.model_dump(
            mode="json"
        )
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise PX4GazeboSITLRecoveryUploadLoopError(
            f"task {task_id} disappeared while attaching recovery upload loop"
        )
    return {**artifacts, "task": updated}


__all__ = [
    "PX4_GAZEBO_SITL_RECOVERY_UPLOAD_ITERATION_SCHEMA_VERSION",
    "PX4_GAZEBO_SITL_RECOVERY_UPLOAD_LOOP_SCHEMA_VERSION",
    "PX4GazeboSITLRecoveryUploadAction",
    "PX4GazeboSITLRecoveryUploadIteration",
    "PX4GazeboSITLRecoveryUploadLoop",
    "PX4GazeboSITLRecoveryUploadLoopError",
    "PX4GazeboSITLRecoveryUploadLoopStatus",
    "SITL_RECOVERY_UPLOAD_ABSOLUTE_MAX_ITERATIONS",
    "SITL_RECOVERY_UPLOAD_ABSOLUTE_GEOFENCE_RADIUS_M",
    "SITL_RECOVERY_UPLOAD_DEFAULT_MAX_ITERATIONS",
    "SITL_RECOVERY_UPLOAD_DEFAULT_GEOFENCE_RADIUS_M",
    "attach_px4_gazebo_sitl_recovery_upload_loop",
    "build_px4_gazebo_sitl_recovery_upload_loop",
    "validate_previous_receipt_refs_for_task",
]
