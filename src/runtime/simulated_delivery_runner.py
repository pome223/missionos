"""Simulated delivery runner v0.

This runner closes the first production-style simulated delivery loop: it
builds the delivery artifact chain, attaches the artifacts to a Mission OS
task, and transitions the task to a terminal simulated status. It is still
simulation/evidence only. It does not start Gazebo, mutate Gazebo entities,
upload PX4 missions, dispatch ROS/MAVLink, send setpoints, or execute
actuators.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import re
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.runtime.delivery_mission_contract import (
    DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION,
    DeliveryMissionContract,
)
from src.runtime.delivery_mission_gate import build_delivery_mission_gate_artifacts
from src.runtime.delivery_mission_policy_review import build_delivery_mission_policy_review
from src.runtime.delivery_progress_review import (
    DeliveryProgressStatus,
    build_delivery_progress_review,
)
from src.runtime.delivery_recovery_decision import (
    DeliveryRecoveryAction,
    build_delivery_recovery_decision,
)
from src.runtime.gazebo_delivery_scenario import (
    GAZEBO_DELIVERY_SCENARIO_SCHEMA_VERSION,
    GazeboDeliveryScenario,
    build_gazebo_delivery_scenario,
)
from src.runtime.gazebo_delivery_sidecar_contract import (
    build_gazebo_delivery_sidecar_contract,
    validate_gazebo_delivery_sidecar_contract,
)
from src.runtime.gazebo_delivery_telemetry_window import (
    build_gazebo_delivery_telemetry_window_hil_artifacts,
)
from src.runtime.px4_gazebo_telemetry import (
    PX4_GAZEBO_SANITIZED_TELEMETRY_SCHEMA_VERSION,
    Px4GazeboSanitizedTelemetry,
)
from src.runtime.simulated_delivery_episode import build_simulated_delivery_episode
from src.runtime.task_store import TaskStore, get_task_store


SIMULATED_DELIVERY_RUNNER_RESULT_SCHEMA_VERSION = (
    "simulated_delivery_runner_result.v1"
)


class SimulatedDeliveryRunnerStatus(str, Enum):
    COMPLETED = "completed"
    BLOCKED = "blocked"


class SimulatedDeliveryRunnerError(RuntimeError):
    """Raised when the simulated delivery runner cannot safely finish."""


_FORBIDDEN_COMMAND_KEYS = frozenset(
    {
        "action",
        "actions",
        "actuator",
        "actuator_execution_allowed",
        "actuators",
        "attitude_setpoint",
        "command",
        "command_payload_allowed",
        "commands",
        "dispatch",
        "dispatch_implementation_present",
        "entity_mutation",
        "execute",
        "execute_now",
        "gazebo_command",
        "gazebo_entity_mutation",
        "gazebo_mutation",
        "joint",
        "landing_command",
        "live_execution_allowed",
        "mavlink_command",
        "mavlink_dispatch_allowed",
        "mission_upload",
        "motor",
        "physical_execution_invoked",
        "position_setpoint",
        "return_to_home_command",
        "ros_action",
        "ros_dispatch_allowed",
        "ros_topic",
        "ros2_topic",
        "setpoint",
        "thrust",
        "torque",
        "velocity_command",
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
        raise SimulatedDeliveryRunnerError(
            "simulated delivery runner refused command-like keys: "
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


def _as_tuple(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({str(value).strip() for value in values if str(value).strip()}))


def _to_contract(value: DeliveryMissionContract | Mapping[str, Any]) -> DeliveryMissionContract:
    if isinstance(value, DeliveryMissionContract):
        return value
    return DeliveryMissionContract.model_validate(dict(value))


def _to_scenario(
    value: GazeboDeliveryScenario | Mapping[str, Any] | None,
    *,
    contract: DeliveryMissionContract,
    now: datetime,
) -> GazeboDeliveryScenario:
    if value is None:
        return build_gazebo_delivery_scenario(
            delivery_mission_contract=contract,
            now=now,
        )
    if isinstance(value, GazeboDeliveryScenario):
        return value
    return GazeboDeliveryScenario.model_validate(dict(value))


def _to_telemetry(
    value: Px4GazeboSanitizedTelemetry | Mapping[str, Any],
) -> Px4GazeboSanitizedTelemetry:
    if isinstance(value, Px4GazeboSanitizedTelemetry):
        return value
    return Px4GazeboSanitizedTelemetry.model_validate(dict(value))


class SimulatedDeliveryRunnerResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[SIMULATED_DELIVERY_RUNNER_RESULT_SCHEMA_VERSION] = (
        SIMULATED_DELIVERY_RUNNER_RESULT_SCHEMA_VERSION
    )
    runner_result_id: str
    task_id: str
    final_task_status: SimulatedDeliveryRunnerStatus
    delivery_mission_contract_id: str
    delivery_mission_id: str
    gazebo_delivery_scenario_id: str
    sanitized_telemetry_id: str
    delivery_gate_status: str
    delivery_gate_passed: bool
    simulated_delivery_episode_id: str
    simulated_delivery_episode_final_status: str
    delivery_progress_review_id: str
    delivery_progress_status: str
    delivery_recovery_decision_id: str
    recovery_primary_action: str
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    completed_at: datetime
    delivery_mission_contract_schema_version: Literal[
        DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    ] = DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    gazebo_delivery_scenario_schema_version: Literal[
        GAZEBO_DELIVERY_SCENARIO_SCHEMA_VERSION
    ] = GAZEBO_DELIVERY_SCENARIO_SCHEMA_VERSION
    px4_gazebo_sanitized_telemetry_schema_version: Literal[
        PX4_GAZEBO_SANITIZED_TELEMETRY_SCHEMA_VERSION
    ] = PX4_GAZEBO_SANITIZED_TELEMETRY_SCHEMA_VERSION
    simulation_only: Literal[True] = True
    telemetry_only: Literal[True] = True
    read_only: Literal[True] = True
    task_status_mutation_only: Literal[True] = True
    recommendations_only: Literal[True] = True
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    stronger_execution_allowed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    rule_based: Literal[True] = True
    llm_judge_used: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _reject_command_like_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        findings = _command_like_key_paths(value, root="metadata")
        if findings:
            raise ValueError(
                "simulated delivery runner result refused command-like metadata keys: "
                + ", ".join(sorted(findings))
            )
        return value


def build_simulated_delivery_runner_artifacts(
    *,
    task_id: str,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    sanitized_telemetry: Px4GazeboSanitizedTelemetry | Mapping[str, Any],
    gazebo_delivery_scenario: GazeboDeliveryScenario | Mapping[str, Any] | None = None,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the full simulated delivery runner v0 artifact set."""

    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    completed_at = _utc(now)
    contract = _to_contract(delivery_mission_contract)
    telemetry = _to_telemetry(sanitized_telemetry)
    scenario = _to_scenario(
        gazebo_delivery_scenario,
        contract=contract,
        now=completed_at,
    )
    sidecar_contract = build_gazebo_delivery_sidecar_contract(
        delivery_mission_contract=contract,
        gazebo_delivery_scenario=scenario,
        now=completed_at,
    )
    validated_sidecar = validate_gazebo_delivery_sidecar_contract(sidecar_contract)
    telemetry_artifacts = build_gazebo_delivery_telemetry_window_hil_artifacts(
        delivery_mission_contract=contract,
        gazebo_delivery_scenario=scenario,
        sanitized_telemetry=telemetry,
        now=completed_at,
    )
    hil_review = telemetry_artifacts["hil_telemetry_review"]
    policy_review = build_delivery_mission_policy_review(
        delivery_mission_contract=contract,
        sanitized_telemetry=telemetry,
        hil_telemetry_review=hil_review,
        now=completed_at,
    )
    delivery_gate = build_delivery_mission_gate_artifacts(
        delivery_mission_contract=contract,
        delivery_mission_policy_review=policy_review,
        now=completed_at,
    )
    telemetry_window_ref = (
        "gazebo_delivery_telemetry_window:"
        f"{telemetry_artifacts['gazebo_delivery_telemetry_window']['window_id']}"
    )
    episode = build_simulated_delivery_episode(
        delivery_mission_contract=contract,
        delivery_mission_policy_review=policy_review,
        delivery_mission_scorecard=delivery_gate["delivery_mission_scorecard"],
        delivery_mission_gate_result=delivery_gate["delivery_mission_gate_result"],
        telemetry_refs=(telemetry_window_ref,),
        now=completed_at,
    )
    progress = build_delivery_progress_review(
        delivery_mission_contract=contract,
        gazebo_delivery_scenario=scenario,
        simulated_delivery_episode=episode,
        sanitized_telemetry=telemetry,
        hil_telemetry_review=hil_review,
        now=completed_at,
    )
    recovery = build_delivery_recovery_decision(
        delivery_mission_contract=contract,
        simulated_delivery_episode=episode,
        delivery_progress_review=progress,
        now=completed_at,
    )
    final_status = (
        SimulatedDeliveryRunnerStatus.COMPLETED
        if delivery_gate["delivery_mission_gate_result"]["passed"]
        and progress.status is DeliveryProgressStatus.COMPLETED
        and recovery.primary_action is DeliveryRecoveryAction.CONTINUE
        else SimulatedDeliveryRunnerStatus.BLOCKED
    )
    terminal_blocked_reasons: list[str] = []
    if final_status is SimulatedDeliveryRunnerStatus.BLOCKED:
        if not delivery_gate["delivery_mission_gate_result"]["passed"]:
            terminal_blocked_reasons.append("delivery_gate_not_passed")
        if progress.status is not DeliveryProgressStatus.COMPLETED:
            terminal_blocked_reasons.append("delivery_progress_not_completed")
        if recovery.primary_action is not DeliveryRecoveryAction.CONTINUE:
            terminal_blocked_reasons.append(
                f"recovery_action_{recovery.primary_action.value}"
            )
    blocked_reasons = _as_tuple(
        [
            *terminal_blocked_reasons,
            *delivery_gate["delivery_mission_gate_result"]["blocked_reasons"],
            *episode.blocked_reasons,
            *progress.blocked_reasons,
        ]
    )
    if final_status is SimulatedDeliveryRunnerStatus.BLOCKED and not blocked_reasons:
        blocked_reasons = ("simulated_delivery_terminal_condition_not_met",)
    warning_reasons = _as_tuple(
        [
            *delivery_gate["delivery_mission_gate_result"]["warning_reasons"],
            *episode.warning_reasons,
            *progress.warning_reasons,
        ]
    )
    artifact_refs = _as_tuple(
        [
            f"delivery_mission_contract:{contract.contract_id}",
            f"gazebo_delivery_scenario:{scenario.scenario_id}",
            f"gazebo_delivery_sidecar_contract:{validated_sidecar.sidecar_contract_id}",
            f"px4_gazebo_sanitized_telemetry:{telemetry.telemetry_id}",
            telemetry_window_ref,
            f"hil_telemetry_review:{hil_review['review_id']}",
            f"delivery_mission_policy_review:{policy_review.review_id}",
            f"delivery_mission_scorecard:{delivery_gate['delivery_mission_scorecard']['scorecard_id']}",
            f"delivery_mission_gate_result:{delivery_gate['delivery_mission_gate_result']['gate_id']}",
            f"simulated_delivery_episode:{episode.episode_id}",
            f"delivery_progress_review:{progress.progress_review_id}",
            f"delivery_recovery_decision:{recovery.decision_id}",
        ]
    )
    result_payload = {
        "task_id": task_id,
        "delivery_mission_contract_id": contract.contract_id,
        "gazebo_delivery_scenario_id": scenario.scenario_id,
        "sanitized_telemetry_id": telemetry.telemetry_id,
        "delivery_gate_id": delivery_gate["delivery_mission_gate_result"]["gate_id"],
        "episode_id": episode.episode_id,
        "progress_review_id": progress.progress_review_id,
        "recovery_decision_id": recovery.decision_id,
        "final_task_status": final_status.value,
        "blocked_reasons": blocked_reasons,
        "warning_reasons": warning_reasons,
    }
    result = SimulatedDeliveryRunnerResult(
        runner_result_id=_stable_id("simulated_delivery_runner_result", result_payload),
        task_id=task_id,
        final_task_status=final_status,
        delivery_mission_contract_id=contract.contract_id,
        delivery_mission_id=contract.mission_id,
        gazebo_delivery_scenario_id=scenario.scenario_id,
        sanitized_telemetry_id=telemetry.telemetry_id,
        delivery_gate_status=delivery_gate["delivery_mission_gate_result"]["status"],
        delivery_gate_passed=delivery_gate["delivery_mission_gate_result"]["passed"],
        simulated_delivery_episode_id=episode.episode_id,
        simulated_delivery_episode_final_status=episode.final_status.value,
        delivery_progress_review_id=progress.progress_review_id,
        delivery_progress_status=progress.status.value,
        delivery_recovery_decision_id=recovery.decision_id,
        recovery_primary_action=recovery.primary_action.value,
        blocked_reasons=blocked_reasons,
        warning_reasons=warning_reasons,
        artifact_refs=artifact_refs,
        completed_at=completed_at,
        metadata={
            **metadata_payload,
            "artifact_only": True,
            "runner_v0": True,
            "task_status_mutation_only": True,
            "no_dispatch_surface": True,
            "no_entity_mutation": True,
        },
    )
    return {
        "delivery_mission_contract": contract.model_dump(mode="json"),
        "gazebo_delivery_scenario": scenario.model_dump(mode="json"),
        "gazebo_delivery_sidecar_contract": validated_sidecar.model_dump(mode="json"),
        "px4_gazebo_sanitized_telemetry": telemetry.model_dump(mode="json"),
        **telemetry_artifacts,
        "delivery_mission_policy_review": policy_review.model_dump(mode="json"),
        **delivery_gate,
        "simulated_delivery_episode": episode.model_dump(mode="json"),
        "delivery_progress_review": progress.model_dump(mode="json"),
        "delivery_recovery_decision": recovery.model_dump(mode="json"),
        "simulated_delivery_runner_result": result.model_dump(mode="json"),
    }


def run_simulated_delivery_task_v0(
    task_id: str,
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    sanitized_telemetry: Px4GazeboSanitizedTelemetry | Mapping[str, Any],
    gazebo_delivery_scenario: GazeboDeliveryScenario | Mapping[str, Any] | None = None,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Run the simulated delivery chain and transition the task terminally."""

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise SimulatedDeliveryRunnerError(
            f"task {task_id} not found; cannot run simulated delivery"
        )
    artifacts = build_simulated_delivery_runner_artifacts(
        task_id=task_id,
        delivery_mission_contract=delivery_mission_contract,
        sanitized_telemetry=sanitized_telemetry,
        gazebo_delivery_scenario=gazebo_delivery_scenario,
        now=now,
    )
    result = artifacts["simulated_delivery_runner_result"]
    final_status = result["final_task_status"]
    blocked_reasons = result["blocked_reasons"]
    error = (
        None
        if final_status == SimulatedDeliveryRunnerStatus.COMPLETED.value
        else "simulated_delivery_blocked: " + ", ".join(blocked_reasons or ["blocked"])
    )
    updated = store.update(
        task_id,
        status=final_status,
        artifacts=artifacts,
        error=error,
        ended_at=time.time(),
    )
    if updated is None:
        raise SimulatedDeliveryRunnerError(
            f"task {task_id} disappeared while running simulated delivery"
        )
    return updated


def create_and_run_simulated_delivery_task_v0(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    sanitized_telemetry: Px4GazeboSanitizedTelemetry | Mapping[str, Any],
    gazebo_delivery_scenario: GazeboDeliveryScenario | Mapping[str, Any] | None = None,
    title: str = "Simulated delivery runner v0",
    owner_session_id: str | None = None,
    owner_user_id: str | None = None,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Create a Mission OS task, run the simulated delivery chain, and finish it."""

    store: TaskStore = (task_store_factory or get_task_store)()
    task = store.create(
        kind="simulated_delivery_runner",
        title=title,
        status="running",
        owner_session_id=owner_session_id,
        owner_user_id=owner_user_id,
        artifacts={"runner": {"schema_version": "simulated_delivery_runner.v0"}},
    )
    return run_simulated_delivery_task_v0(
        task["task_id"],
        delivery_mission_contract=delivery_mission_contract,
        sanitized_telemetry=sanitized_telemetry,
        gazebo_delivery_scenario=gazebo_delivery_scenario,
        now=now,
        task_store_factory=lambda: store,
    )


__all__ = [
    "SIMULATED_DELIVERY_RUNNER_RESULT_SCHEMA_VERSION",
    "SimulatedDeliveryRunnerError",
    "SimulatedDeliveryRunnerResult",
    "SimulatedDeliveryRunnerStatus",
    "build_simulated_delivery_runner_artifacts",
    "create_and_run_simulated_delivery_task_v0",
    "run_simulated_delivery_task_v0",
]
