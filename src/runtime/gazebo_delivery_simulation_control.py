"""Operator-supervised simulation-only control for Gazebo delivery.

This module wraps the deterministic Gazebo delivery sidecar v0 with an
operator-approval artifact and a read-only audit artifact. It may request only
bounded simulation operations (`start_delivery_simulation` and
`advance_delivery_step`) and only after a delivery gate has passed and an
explicit simulation-only approval is present.

It does not expose raw Gazebo mutation, ROS/MAVLink/PX4 commands, actuator
execution, live execution, or physical execution.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import re
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.runtime.delivery_mission_contract import DeliveryMissionContract
from src.runtime.delivery_mission_gate import DeliveryMissionGateResult
from src.runtime.gazebo_delivery_scenario import GazeboDeliveryScenario
from src.runtime.gazebo_delivery_sidecar_contract import (
    GazeboDeliverySidecarContract,
    GazeboDeliverySidecarRequestKind,
    build_gazebo_delivery_sidecar_contract,
    validate_gazebo_delivery_sidecar_contract,
)
from src.runtime.gazebo_delivery_sidecar_v0 import (
    build_gazebo_delivery_sidecar_v0_sequence,
)
from src.runtime.simulated_delivery_runner import run_simulated_delivery_task_v0
from src.runtime.task_store import TaskStore, get_task_store

GAZEBO_DELIVERY_SIMULATION_APPROVAL_SCHEMA_VERSION = (
    "gazebo_delivery_simulation_approval.v1"
)
GAZEBO_DELIVERY_SIMULATION_CONTROL_AUDIT_SCHEMA_VERSION = (
    "gazebo_delivery_simulation_control_audit.v1"
)


class GazeboDeliverySimulationControlStatus(str, Enum):
    COMPLETED = "completed"
    BLOCKED = "blocked"


class GazeboDeliverySimulationControlError(RuntimeError):
    """Raised when simulation-only Gazebo delivery control cannot proceed."""


_FORBIDDEN_COMMAND_KEYS = frozenset(
    {
        "action",
        "actuator",
        "actuator_execution_allowed",
        "command",
        "command_payload_allowed",
        "dispatch",
        "entity_mutation",
        "gazebo_command",
        "gazebo_entity_mutation",
        "gazebo_mutation",
        "live_execution_allowed",
        "mavlink_command",
        "mavlink_dispatch_allowed",
        "mission_upload",
        "physical_execution_invoked",
        "position_setpoint",
        "ros_action",
        "ros_dispatch_allowed",
        "ros_topic",
        "setpoint",
        "thrust",
        "torque",
    }
)


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


_FORBIDDEN_COMMAND_KEYS_NORMALIZED = frozenset(
    _normalize_key(key) for key in _FORBIDDEN_COMMAND_KEYS
)


def _command_like_key_paths(value: Any, *, root: str = "") -> list[str]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key, sub in value.items():
            key_text = str(key)
            path = f"{root}.{key_text}" if root else key_text
            if _normalize_key(key_text) in _FORBIDDEN_COMMAND_KEYS_NORMALIZED:
                findings.append(path)
            findings.extend(_command_like_key_paths(sub, root=path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            path = f"{root}.{index}" if root else str(index)
            findings.extend(_command_like_key_paths(item, root=path))
    return findings


def _raise_for_command_like_keys(value: Any, *, root: str) -> None:
    findings = _command_like_key_paths(value, root=root)
    if findings:
        raise GazeboDeliverySimulationControlError(
            "gazebo delivery simulation control refused command-like keys: "
            + ", ".join(sorted(findings))
        )


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


def _as_tuple(values: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(
        sorted({str(item).strip() for item in values or () if str(item).strip()})
    )


def _ordered_tuple(values: Sequence[str] | None) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in values or ():
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            ordered.append(text)
    return tuple(ordered)


def _to_contract(
    value: DeliveryMissionContract | Mapping[str, Any],
) -> DeliveryMissionContract:
    if isinstance(value, DeliveryMissionContract):
        return value
    return DeliveryMissionContract.model_validate(dict(value))


def _to_scenario(
    value: GazeboDeliveryScenario | Mapping[str, Any],
) -> GazeboDeliveryScenario:
    if isinstance(value, GazeboDeliveryScenario):
        return value
    return GazeboDeliveryScenario.model_validate(dict(value))


def _to_sidecar_contract(
    value: GazeboDeliverySidecarContract | Mapping[str, Any],
) -> GazeboDeliverySidecarContract:
    return validate_gazebo_delivery_sidecar_contract(value)


def _to_request_kind(
    value: GazeboDeliverySidecarRequestKind | str,
) -> GazeboDeliverySidecarRequestKind:
    if isinstance(value, GazeboDeliverySidecarRequestKind):
        return value
    return GazeboDeliverySidecarRequestKind(str(value))


class GazeboDeliverySimulationApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[GAZEBO_DELIVERY_SIMULATION_APPROVAL_SCHEMA_VERSION] = (
        GAZEBO_DELIVERY_SIMULATION_APPROVAL_SCHEMA_VERSION
    )
    approval_id: str
    approval_kind: Literal["gazebo_delivery_simulation_control"] = (
        "gazebo_delivery_simulation_control"
    )
    request_kind: GazeboDeliverySidecarRequestKind
    sidecar_contract_id: str
    delivery_mission_contract_id: str
    delivery_mission_id: str
    gazebo_delivery_scenario_id: str
    gate_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    operator_approval_performed: bool
    approved_by: str = "operator"
    approved_at: datetime
    simulation_only: Literal[True] = True
    operator_approval_required: Literal[True] = True
    real_world_approval: Literal[False] = False
    stronger_execution_allowed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    raw_gazebo_entity_mutation_exposed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _reject_command_like_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        findings = _command_like_key_paths(value, root="metadata")
        if findings:
            raise ValueError(
                "gazebo delivery simulation approval refused command-like metadata keys: "
                + ", ".join(sorted(findings))
            )
        return value


class GazeboDeliverySimulationControlAudit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[GAZEBO_DELIVERY_SIMULATION_CONTROL_AUDIT_SCHEMA_VERSION] = (
        GAZEBO_DELIVERY_SIMULATION_CONTROL_AUDIT_SCHEMA_VERSION
    )
    audit_id: str
    status: GazeboDeliverySimulationControlStatus
    requested_simulation_actions: tuple[str, ...]
    approval_ref: str | None = None
    sidecar_contract_ref: str
    pre_gate_refs: tuple[str, ...] = ()
    post_gate_refs: tuple[str, ...] = ()
    returned_artifact_refs: tuple[str, ...] = ()
    sidecar_result_refs: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    final_task_status: str | None = None
    audited_at: datetime
    simulation_only: Literal[True] = True
    read_only_audit: Literal[True] = True
    operator_visible: Literal[True] = True
    operator_approval_required: Literal[True] = True
    operator_approval_performed: bool
    sidecar_returns_artifacts_only: Literal[True] = True
    mission_os_validates_returned_artifacts: Literal[True] = True
    stronger_execution_allowed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    raw_gazebo_entity_mutation_exposed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _reject_command_like_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        findings = _command_like_key_paths(value, root="metadata")
        if findings:
            raise ValueError(
                "gazebo delivery simulation audit refused command-like metadata keys: "
                + ", ".join(sorted(findings))
            )
        return value


def build_gazebo_delivery_simulation_approval(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    gazebo_delivery_scenario: GazeboDeliveryScenario | Mapping[str, Any],
    sidecar_contract: GazeboDeliverySidecarContract | Mapping[str, Any],
    request_kind: GazeboDeliverySidecarRequestKind | str,
    operator_approval_performed: bool,
    gate_refs: Sequence[str] | None = None,
    evidence_refs: Sequence[str] | None = None,
    approved_by: str = "operator",
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> GazeboDeliverySimulationApproval:
    """Build simulation-only operator approval without real-world authority."""

    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    approved_at = _utc(now)
    contract = _to_contract(delivery_mission_contract)
    scenario = _to_scenario(gazebo_delivery_scenario)
    sidecar = _to_sidecar_contract(sidecar_contract)
    kind = _to_request_kind(request_kind)
    if sidecar.delivery_mission_contract_id != contract.contract_id:
        raise GazeboDeliverySimulationControlError("sidecar contract_id mismatch")
    if sidecar.gazebo_delivery_scenario_id != scenario.scenario_id:
        raise GazeboDeliverySimulationControlError("sidecar scenario_id mismatch")
    payload = {
        "request_kind": kind.value,
        "sidecar_contract_id": sidecar.sidecar_contract_id,
        "gate_refs": _as_tuple(gate_refs),
        "evidence_refs": _as_tuple(evidence_refs),
        "operator_approval_performed": operator_approval_performed,
    }
    return GazeboDeliverySimulationApproval(
        approval_id=_stable_id("gazebo_delivery_simulation_approval", payload),
        request_kind=kind,
        sidecar_contract_id=sidecar.sidecar_contract_id,
        delivery_mission_contract_id=contract.contract_id,
        delivery_mission_id=contract.mission_id,
        gazebo_delivery_scenario_id=scenario.scenario_id,
        gate_refs=_as_tuple(gate_refs),
        evidence_refs=_as_tuple(evidence_refs),
        operator_approval_performed=operator_approval_performed,
        approved_by=str(approved_by or "operator"),
        approved_at=approved_at,
        metadata={
            **metadata_payload,
            "approval_scope": "simulation_only_gazebo_delivery",
            "real_world_authority_granted": False,
            "no_raw_gazebo_mutation_surface": True,
            "no_ros_mavlink_px4_command_surface": True,
            "no_actuator_live_physical_execution": True,
        },
    )


def _gate_ref(gate: Mapping[str, Any]) -> str:
    return f"delivery_mission_gate_result:{gate.get('gate_id', '-')}"


def _pre_gate_blocked_reasons(
    gate_payload: Mapping[str, Any],
    *,
    contract: DeliveryMissionContract,
) -> list[str]:
    try:
        gate = DeliveryMissionGateResult.model_validate(dict(gate_payload))
    except Exception:
        return ["pre_gate_invalid"]

    blocked: list[str] = []
    if gate.delivery_mission_contract_id != contract.contract_id:
        blocked.append("pre_gate_contract_mismatch")
    if gate.delivery_mission_id != contract.mission_id:
        blocked.append("pre_gate_mission_mismatch")
    if not gate.passed:
        blocked.append("pre_gate_not_passed")
        blocked.extend(gate.blocked_reasons)
    return blocked


def _blocked_audit(
    *,
    approval: GazeboDeliverySimulationApproval,
    sidecar_contract: GazeboDeliverySidecarContract,
    requested_actions: Sequence[str],
    pre_gate_refs: Sequence[str],
    blocked_reasons: Sequence[str],
    now: datetime,
) -> GazeboDeliverySimulationControlAudit:
    payload = {
        "status": "blocked",
        "approval_id": approval.approval_id,
        "sidecar_contract_id": sidecar_contract.sidecar_contract_id,
        "requested_actions": list(requested_actions),
        "blocked_reasons": _as_tuple(blocked_reasons),
    }
    return GazeboDeliverySimulationControlAudit(
        audit_id=_stable_id("gazebo_delivery_simulation_control_audit", payload),
        status=GazeboDeliverySimulationControlStatus.BLOCKED,
        requested_simulation_actions=_ordered_tuple(requested_actions),
        approval_ref=f"gazebo_delivery_simulation_approval:{approval.approval_id}",
        sidecar_contract_ref=(
            f"gazebo_delivery_sidecar_contract:{sidecar_contract.sidecar_contract_id}"
        ),
        pre_gate_refs=_ordered_tuple(pre_gate_refs),
        blocked_reasons=_as_tuple(blocked_reasons),
        final_task_status="blocked",
        audited_at=now,
        operator_approval_performed=approval.operator_approval_performed,
        metadata={
            "audit_only": True,
            "simulation_control_blocked_before_sidecar": True,
        },
    )


def _completed_audit(
    *,
    approval: GazeboDeliverySimulationApproval,
    sidecar_contract: GazeboDeliverySidecarContract,
    sequence: Sequence[Mapping[str, Any]],
    updated_task: Mapping[str, Any],
    requested_actions: Sequence[str],
    pre_gate_refs: Sequence[str],
    now: datetime,
) -> GazeboDeliverySimulationControlAudit:
    sidecar_results = [item["gazebo_delivery_sidecar_result"] for item in sequence]
    returned_refs: list[str] = []
    sidecar_refs: list[str] = []
    for item in sidecar_results:
        sidecar_refs.append(
            f"gazebo_delivery_sidecar_result:{item['sidecar_result_id']}"
        )
        returned_refs.extend(item.get("returned_artifact_refs", ()))
    final_gate = updated_task["artifacts"].get("delivery_mission_gate_result", {})
    blocked_reasons = list(final_gate.get("blocked_reasons", ()))
    warning_reasons = list(final_gate.get("warning_reasons", ()))
    payload = {
        "status": updated_task["status"],
        "approval_id": approval.approval_id,
        "sidecar_contract_id": sidecar_contract.sidecar_contract_id,
        "sidecar_result_refs": sidecar_refs,
        "blocked_reasons": blocked_reasons,
        "warning_reasons": warning_reasons,
    }
    return GazeboDeliverySimulationControlAudit(
        audit_id=_stable_id("gazebo_delivery_simulation_control_audit", payload),
        status=(
            GazeboDeliverySimulationControlStatus.COMPLETED
            if updated_task["status"] == "completed"
            else GazeboDeliverySimulationControlStatus.BLOCKED
        ),
        requested_simulation_actions=_ordered_tuple(requested_actions),
        approval_ref=f"gazebo_delivery_simulation_approval:{approval.approval_id}",
        sidecar_contract_ref=(
            f"gazebo_delivery_sidecar_contract:{sidecar_contract.sidecar_contract_id}"
        ),
        pre_gate_refs=_ordered_tuple(pre_gate_refs),
        post_gate_refs=_ordered_tuple([_gate_ref(final_gate)] if final_gate else ()),
        returned_artifact_refs=_ordered_tuple(returned_refs),
        sidecar_result_refs=_ordered_tuple(sidecar_refs),
        blocked_reasons=_as_tuple(blocked_reasons),
        warning_reasons=_as_tuple(warning_reasons),
        final_task_status=str(updated_task["status"]),
        audited_at=now,
        operator_approval_performed=approval.operator_approval_performed,
        metadata={
            "audit_only": True,
            "simulation_control_completed_sidecar_runner_path": updated_task["status"]
            == "completed",
        },
    )


def run_gazebo_delivery_simulation_control_v0_task(
    task_id: str,
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    gazebo_delivery_scenario: GazeboDeliveryScenario | Mapping[str, Any],
    delivery_mission_gate_result: Mapping[str, Any],
    operator_approval_performed: bool,
    sidecar_contract: GazeboDeliverySidecarContract | Mapping[str, Any] | None = None,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Run bounded simulation-only start/advance after gate and approval."""

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise GazeboDeliverySimulationControlError(
            f"task {task_id} not found; cannot run simulation control"
        )
    base_time = _utc(now)
    contract = _to_contract(delivery_mission_contract)
    scenario = _to_scenario(gazebo_delivery_scenario)
    sidecar = _to_sidecar_contract(
        sidecar_contract
        or build_gazebo_delivery_sidecar_contract(
            delivery_mission_contract=contract,
            gazebo_delivery_scenario=scenario,
            now=base_time,
        )
    )
    pre_gate_refs = (_gate_ref(delivery_mission_gate_result),)
    requested_actions = (
        GazeboDeliverySidecarRequestKind.START_DELIVERY_SIMULATION.value,
        GazeboDeliverySidecarRequestKind.ADVANCE_DELIVERY_STEP.value,
    )
    approval = build_gazebo_delivery_simulation_approval(
        delivery_mission_contract=contract,
        gazebo_delivery_scenario=scenario,
        sidecar_contract=sidecar,
        request_kind=GazeboDeliverySidecarRequestKind.START_DELIVERY_SIMULATION,
        operator_approval_performed=operator_approval_performed,
        gate_refs=pre_gate_refs,
        evidence_refs=delivery_mission_gate_result.get("evidence_refs", ()),
        now=base_time,
    )
    blocked: list[str] = []
    blocked.extend(
        _pre_gate_blocked_reasons(
            delivery_mission_gate_result,
            contract=contract,
        )
    )
    if not approval.operator_approval_performed:
        blocked.append("simulation_operator_approval_missing")
    if blocked:
        audit = _blocked_audit(
            approval=approval,
            sidecar_contract=sidecar,
            requested_actions=requested_actions,
            pre_gate_refs=pre_gate_refs,
            blocked_reasons=blocked,
            now=base_time,
        )
        updated = store.update(
            task_id,
            status="blocked",
            artifacts={
                "gazebo_delivery_simulation_approval": approval.model_dump(mode="json"),
                "gazebo_delivery_simulation_control_audit": audit.model_dump(
                    mode="json"
                ),
            },
            error="gazebo_delivery_simulation_control_blocked: "
            + ", ".join(audit.blocked_reasons),
            ended_at=time.time(),
        )
        if updated is None:
            raise GazeboDeliverySimulationControlError(
                f"task {task_id} disappeared while blocking simulation control"
            )
        return updated

    sequence = build_gazebo_delivery_sidecar_v0_sequence(
        delivery_mission_contract=contract,
        gazebo_delivery_scenario=scenario,
        sidecar_contract=sidecar,
        now=base_time,
    )
    store.update(
        task_id,
        artifacts={
            "gazebo_delivery_simulation_approval": approval.model_dump(mode="json"),
            "gazebo_delivery_sidecar_v0_sequence": [
                item["gazebo_delivery_sidecar_result"] for item in sequence
            ],
            "gazebo_delivery_sidecar_v0_steps": [
                item["simulated_delivery_step"] for item in sequence
            ],
        },
    )
    final_artifacts = sequence[-1]
    updated = run_simulated_delivery_task_v0(
        task_id,
        delivery_mission_contract=contract,
        gazebo_delivery_scenario=scenario,
        sanitized_telemetry=final_artifacts["px4_gazebo_sanitized_telemetry"],
        now=base_time,
        task_store_factory=lambda: store,
    )
    audit = _completed_audit(
        approval=approval,
        sidecar_contract=sidecar,
        sequence=sequence,
        updated_task=updated,
        requested_actions=requested_actions,
        pre_gate_refs=pre_gate_refs,
        now=base_time,
    )
    final = store.update(
        task_id,
        artifacts={
            "gazebo_delivery_simulation_control_audit": audit.model_dump(mode="json")
        },
    )
    if final is None:
        raise GazeboDeliverySimulationControlError(
            f"task {task_id} disappeared while attaching simulation control audit"
        )
    return final


__all__ = [
    "GAZEBO_DELIVERY_SIMULATION_APPROVAL_SCHEMA_VERSION",
    "GAZEBO_DELIVERY_SIMULATION_CONTROL_AUDIT_SCHEMA_VERSION",
    "GazeboDeliverySimulationApproval",
    "GazeboDeliverySimulationControlAudit",
    "GazeboDeliverySimulationControlError",
    "GazeboDeliverySimulationControlStatus",
    "build_gazebo_delivery_simulation_approval",
    "run_gazebo_delivery_simulation_control_v0_task",
]
