"""Record logic-only bounded SITL recovery run artifacts."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.delivery_mission_contract import (
    DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION,
    DeliveryMissionContract,
)
from src.runtime.delivery_recovery_request import (
    DELIVERY_RECOVERY_REQUEST_SCHEMA_VERSION,
    DeliveryRecoveryRequest,
    DeliveryRecoveryRequestKind,
    DeliveryRecoveryRequestStatus,
)
from src.runtime.delivery_recovery_safety import raise_for_command_like_payload
from src.runtime.px4_gazebo_sitl_mission_upload import (
    MAV_CMD_NAV_LAND,
    MAV_CMD_NAV_RETURN_TO_LAUNCH,
    MAV_CMD_NAV_WAYPOINT,
    PX4GazeboSITLMissionItem,
)
from src.runtime.simulated_delivery_command import (
    SIMULATED_COMMAND_APPROVAL_SCHEMA_VERSION,
    SIMULATED_COMMAND_PROPOSAL_SCHEMA_VERSION,
    SIMULATOR_COMMAND_EXECUTION_PREFLIGHT_SCHEMA_VERSION,
    SimulatedCommandApproval,
    SimulatedCommandProposal,
    SimulatorCommandExecutionPreflight,
    SimulatorCommandExecutionPreflightStatus,
)
from src.runtime.task_store import TaskStore, get_task_store

DELIVERY_RECOVERY_RUN_SCHEMA_VERSION = "delivery_recovery_run.v1"


class DeliveryRecoveryRunError(RuntimeError):
    """Raised when a bounded recovery run cannot be represented safely."""


class DeliveryRecoveryRunStatus(str, Enum):
    LOGIC_ONLY_RECORDED = "logic_only_recorded"
    BLOCKED = "blocked"
    OPERATOR_ESCALATION_REQUIRED = "operator_escalation_required"


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


def _as_tuple(values: Sequence[Any] | None) -> tuple[Any, ...]:
    return tuple(values or ())


def _as_str_tuple(values: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(
        sorted({str(item).strip() for item in (values or ()) if str(item).strip()})
    )


def _contract_ref(contract: DeliveryMissionContract) -> str:
    return f"delivery_mission_contract:{contract.contract_id}"


def _request_ref(request: DeliveryRecoveryRequest) -> str:
    return f"delivery_recovery_request:{request.request_id}"


def _preflight_ref(preflight: SimulatorCommandExecutionPreflight) -> str:
    return f"simulator_command_execution_preflight:{preflight.preflight_id}"


def _proposal_ref(proposal: SimulatedCommandProposal) -> str:
    return f"simulated_command_proposal:{proposal.proposal_id}"


def _approval_ref(approval: SimulatedCommandApproval) -> str:
    return f"simulated_command_approval:{approval.approval_id}"


def _to_contract(
    value: DeliveryMissionContract | Mapping[str, Any],
) -> DeliveryMissionContract:
    if isinstance(value, DeliveryMissionContract):
        return value
    return DeliveryMissionContract.model_validate(dict(value))


def _to_request(
    value: DeliveryRecoveryRequest | Mapping[str, Any],
) -> DeliveryRecoveryRequest:
    if isinstance(value, DeliveryRecoveryRequest):
        return value
    return DeliveryRecoveryRequest.model_validate(dict(value))


def _to_preflight(
    value: SimulatorCommandExecutionPreflight | Mapping[str, Any],
) -> SimulatorCommandExecutionPreflight:
    if isinstance(value, SimulatorCommandExecutionPreflight):
        return value
    return SimulatorCommandExecutionPreflight.model_validate(dict(value))


def _to_proposal(
    value: SimulatedCommandProposal | Mapping[str, Any],
) -> SimulatedCommandProposal:
    if isinstance(value, SimulatedCommandProposal):
        return value
    return SimulatedCommandProposal.model_validate(dict(value))


def _to_approval(
    value: SimulatedCommandApproval | Mapping[str, Any],
) -> SimulatedCommandApproval:
    if isinstance(value, SimulatedCommandApproval):
        return value
    return SimulatedCommandApproval.model_validate(dict(value))


def _home(contract: DeliveryMissionContract) -> tuple[float, float]:
    return (contract.pickup_location.latitude, contract.pickup_location.longitude)


def _dropoff(contract: DeliveryMissionContract) -> tuple[float, float]:
    return (contract.dropoff_location.latitude, contract.dropoff_location.longitude)


def _mission_items_for_request(
    *,
    contract: DeliveryMissionContract,
    request_kind: DeliveryRecoveryRequestKind,
) -> tuple[PX4GazeboSITLMissionItem, ...]:
    home_lat, home_lon = _home(contract)
    drop_lat, drop_lon = _dropoff(contract)
    if request_kind is DeliveryRecoveryRequestKind.RETURN_TO_HOME_SIMULATION:
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
    if request_kind is DeliveryRecoveryRequestKind.ABORT_AND_LAND_SIMULATION:
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
    if request_kind is DeliveryRecoveryRequestKind.HOLD_POSITION_SIMULATION:
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
    if request_kind is DeliveryRecoveryRequestKind.RETRY_DROPOFF_SIMULATION:
        return (
            PX4GazeboSITLMissionItem(
                seq=0,
                command=MAV_CMD_NAV_WAYPOINT,
                latitude_deg=drop_lat,
                longitude_deg=drop_lon,
                altitude_m=20.0,
                current=1,
            ),
            PX4GazeboSITLMissionItem(
                seq=1,
                command=MAV_CMD_NAV_LAND,
                latitude_deg=drop_lat,
                longitude_deg=drop_lon,
                altitude_m=0.0,
            ),
        )
    return ()


def _validate_logic_only_request(request: DeliveryRecoveryRequest) -> None:
    if request.executed_against_real_sitl is not False:
        raise DeliveryRecoveryRunError(
            "delivery recovery run requires logic-only request"
        )
    if request.recovery_chain_evidence_source != "logic_only_stub":
        raise DeliveryRecoveryRunError(
            "delivery recovery run requires logic-only evidence source"
        )


class DeliveryRecoveryRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_RECOVERY_RUN_SCHEMA_VERSION] = (
        DELIVERY_RECOVERY_RUN_SCHEMA_VERSION
    )
    recovery_run_id: str
    recovery_request_ref: str
    mission_contract_ref: str
    simulator_command_execution_preflight_ref: str
    simulated_command_proposal_ref: str
    simulated_command_approval_ref: str
    sitl_session_ref: str
    execution_scope: Literal[
        "logic_only_stub_recovery_plan",
        "operator_escalation_only",
        "blocked_no_execution",
    ]
    planned_mission_items: tuple[PX4GazeboSITLMissionItem, ...] = ()
    mission_item_count: int = Field(ge=0)
    recovery_request_kind: DeliveryRecoveryRequestKind
    status: DeliveryRecoveryRunStatus
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    observed_facts: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime
    finished_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    delivery_mission_contract_schema_version: Literal[
        DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    ] = DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    delivery_recovery_request_schema_version: Literal[
        DELIVERY_RECOVERY_REQUEST_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_REQUEST_SCHEMA_VERSION
    simulator_command_execution_preflight_schema_version: Literal[
        SIMULATOR_COMMAND_EXECUTION_PREFLIGHT_SCHEMA_VERSION
    ] = SIMULATOR_COMMAND_EXECUTION_PREFLIGHT_SCHEMA_VERSION
    simulated_command_proposal_schema_version: Literal[
        SIMULATED_COMMAND_PROPOSAL_SCHEMA_VERSION
    ] = SIMULATED_COMMAND_PROPOSAL_SCHEMA_VERSION
    simulated_command_approval_schema_version: Literal[
        SIMULATED_COMMAND_APPROVAL_SCHEMA_VERSION
    ] = SIMULATED_COMMAND_APPROVAL_SCHEMA_VERSION
    executed_against_real_sitl: Literal[False] = False
    recovery_chain_evidence_source: Literal["logic_only_stub"] = "logic_only_stub"
    logic_only_stub: Literal[True] = True
    real_sitl_execution_claimed: Literal[False] = False
    mission_upload_performed: Literal[False] = False
    external_dispatch_performed: Literal[False] = False
    mavlink_dispatch_performed: Literal[False] = False
    px4_mission_upload_performed: Literal[False] = False
    gazebo_simulator_command_performed: Literal[False] = False
    sitl_only: Literal[True] = True
    physical_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_hardware_target: Literal[False] = False
    gazebo_entity_mutation_performed: Literal[False] = False
    ros_dispatch_performed: Literal[False] = False
    actuator_execution_performed: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False

    @field_validator(
        "planned_mission_items",
        "blocked_reasons",
        "warning_reasons",
        mode="before",
    )
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[Any, ...]:
        if isinstance(value, tuple):
            return value
        if isinstance(value, list):
            return tuple(value)
        return _as_tuple(value)

    @field_validator("started_at", "finished_at", mode="before")
    @classmethod
    def _coerce_timestamp(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_run(self) -> "DeliveryRecoveryRun":
        raise_for_command_like_payload(
            self.metadata,
            root="run.metadata",
            error_type=DeliveryRecoveryRunError,
            prefix="delivery recovery run refused command-like metadata",
        )
        raise_for_command_like_payload(
            self.observed_facts,
            root="run.observed_facts",
            error_type=DeliveryRecoveryRunError,
            prefix="delivery recovery run refused command-like observed facts",
        )
        if self.finished_at < self.started_at:
            raise DeliveryRecoveryRunError("recovery run finished before it started")
        if self.mission_item_count != len(self.planned_mission_items):
            raise DeliveryRecoveryRunError("mission item count mismatch")
        if self.status is DeliveryRecoveryRunStatus.LOGIC_ONLY_RECORDED:
            if self.blocked_reasons:
                raise DeliveryRecoveryRunError("logic-only run cannot be blocked")
            if self.execution_scope != "logic_only_stub_recovery_plan":
                raise DeliveryRecoveryRunError("logic-only run scope mismatch")
            if not self.planned_mission_items:
                raise DeliveryRecoveryRunError(
                    "logic-only run requires planned mission items"
                )
        else:
            if not self.blocked_reasons:
                raise DeliveryRecoveryRunError("blocked run requires reasons")
            if self.planned_mission_items:
                raise DeliveryRecoveryRunError("blocked run cannot plan mission items")
        return self


def build_delivery_recovery_run(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    delivery_recovery_request: DeliveryRecoveryRequest | Mapping[str, Any],
    simulator_command_execution_preflight: (
        SimulatorCommandExecutionPreflight | Mapping[str, Any]
    ),
    simulated_command_proposal: SimulatedCommandProposal | Mapping[str, Any],
    simulated_command_approval: SimulatedCommandApproval | Mapping[str, Any],
    sitl_session_ref: str,
    observed_facts: Mapping[str, Any] | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> DeliveryRecoveryRun:
    metadata_payload = dict(metadata or {})
    facts = dict(observed_facts or {})
    raise_for_command_like_payload(
        metadata_payload,
        root="metadata",
        error_type=DeliveryRecoveryRunError,
        prefix="delivery recovery run refused command-like metadata",
    )
    raise_for_command_like_payload(
        facts,
        root="observed_facts",
        error_type=DeliveryRecoveryRunError,
        prefix="delivery recovery run refused command-like observed facts",
    )
    contract = _to_contract(delivery_mission_contract)
    request = _to_request(delivery_recovery_request)
    preflight = _to_preflight(simulator_command_execution_preflight)
    proposal = _to_proposal(simulated_command_proposal)
    approval = _to_approval(simulated_command_approval)
    _validate_logic_only_request(request)
    started = _utc(started_at)
    finished = _utc(finished_at or started)
    contract_ref = _contract_ref(contract)
    proposal_ref = _proposal_ref(proposal)
    approval_ref = _approval_ref(approval)
    preflight_ref = _preflight_ref(preflight)
    if request.mission_contract_ref != contract_ref:
        raise DeliveryRecoveryRunError("request contract ref mismatch")
    blocked: list[str] = []
    if preflight.simulated_command_proposal_ref != proposal_ref:
        blocked.append("preflight_proposal_ref_mismatch")
    if preflight.simulated_command_approval_ref != approval_ref:
        blocked.append("preflight_approval_ref_mismatch")
    if approval.simulated_command_proposal_ref != proposal_ref:
        blocked.append("approval_proposal_ref_mismatch")
    if not approval.operator_approved:
        blocked.append("simulated_command_not_operator_approved")
    if (
        preflight.status
        is not SimulatorCommandExecutionPreflightStatus.READY_FOR_SIMULATOR_COMMAND
    ):
        blocked.extend(preflight.blocked_reasons or ("preflight_not_ready",))
    if request.request_status is not DeliveryRecoveryRequestStatus.READY:
        blocked.extend(request.blocked_reasons or ("recovery_request_not_ready",))

    mission_items: tuple[PX4GazeboSITLMissionItem, ...] = ()
    if not blocked and (
        request.request_kind is DeliveryRecoveryRequestKind.OPERATOR_ESCALATION_ONLY
    ):
        blocked.append("operator_escalation_only_request_not_executable")
    if not blocked:
        mission_items = _mission_items_for_request(
            contract=contract,
            request_kind=request.request_kind,
        )
        if not mission_items:
            blocked.append("recovery_request_has_no_bounded_mission_items")

    blocked_reasons = _as_str_tuple(blocked)
    status = (
        DeliveryRecoveryRunStatus.LOGIC_ONLY_RECORDED
        if not blocked_reasons
        else (
            DeliveryRecoveryRunStatus.OPERATOR_ESCALATION_REQUIRED
            if request.request_kind
            is DeliveryRecoveryRequestKind.OPERATOR_ESCALATION_ONLY
            else DeliveryRecoveryRunStatus.BLOCKED
        )
    )
    execution_scope = (
        "logic_only_stub_recovery_plan"
        if status is DeliveryRecoveryRunStatus.LOGIC_ONLY_RECORDED
        else (
            "operator_escalation_only"
            if status is DeliveryRecoveryRunStatus.OPERATOR_ESCALATION_REQUIRED
            else "blocked_no_execution"
        )
    )
    if blocked_reasons:
        mission_items = ()
    payload = {
        "request": request.request_id,
        "contract": contract.contract_id,
        "session": sitl_session_ref,
        "kind": request.request_kind.value,
        "status": status.value,
        "blocked": blocked_reasons,
        "mission_items": [item.model_dump(mode="json") for item in mission_items],
        "executed_against_real_sitl": False,
        "recovery_chain_evidence_source": "logic_only_stub",
    }
    return DeliveryRecoveryRun(
        recovery_run_id=_stable_id("delivery_recovery_run", payload),
        recovery_request_ref=_request_ref(request),
        mission_contract_ref=contract_ref,
        simulator_command_execution_preflight_ref=preflight_ref,
        simulated_command_proposal_ref=proposal_ref,
        simulated_command_approval_ref=approval_ref,
        sitl_session_ref=str(sitl_session_ref or ""),
        execution_scope=execution_scope,
        planned_mission_items=mission_items,
        mission_item_count=len(mission_items),
        recovery_request_kind=request.request_kind,
        status=status,
        blocked_reasons=blocked_reasons,
        warning_reasons=request.warning_reasons,
        observed_facts=facts,
        started_at=started,
        finished_at=finished,
        metadata={
            **metadata_payload,
            "logic_only_stub": True,
            "no_real_sitl_container_started": True,
            "mission_upload_performed": False,
            "executed_against_real_sitl": False,
            "recovery_chain_evidence_source": "logic_only_stub",
        },
    )


def attach_delivery_recovery_run(
    task_id: str,
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    delivery_recovery_request: DeliveryRecoveryRequest | Mapping[str, Any],
    simulator_command_execution_preflight: (
        SimulatorCommandExecutionPreflight | Mapping[str, Any]
    ),
    simulated_command_proposal: SimulatedCommandProposal | Mapping[str, Any],
    simulated_command_approval: SimulatedCommandApproval | Mapping[str, Any],
    sitl_session_ref: str,
    observed_facts: Mapping[str, Any] | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    store = (task_store_factory or get_task_store)()
    if store.get(task_id) is None:
        raise DeliveryRecoveryRunError(
            f"task {task_id} not found; cannot attach recovery run"
        )
    run = build_delivery_recovery_run(
        delivery_mission_contract=delivery_mission_contract,
        delivery_recovery_request=delivery_recovery_request,
        simulator_command_execution_preflight=simulator_command_execution_preflight,
        simulated_command_proposal=simulated_command_proposal,
        simulated_command_approval=simulated_command_approval,
        sitl_session_ref=sitl_session_ref,
        observed_facts=observed_facts,
        started_at=started_at,
        finished_at=finished_at,
        metadata=metadata,
    )
    artifacts = {"delivery_recovery_run": run.model_dump(mode="json")}
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise DeliveryRecoveryRunError(
            f"task {task_id} disappeared while attaching recovery run"
        )
    return {**artifacts, "task": updated}


__all__ = [
    "DELIVERY_RECOVERY_RUN_SCHEMA_VERSION",
    "DeliveryRecoveryRun",
    "DeliveryRecoveryRunError",
    "DeliveryRecoveryRunStatus",
    "attach_delivery_recovery_run",
    "build_delivery_recovery_run",
]
