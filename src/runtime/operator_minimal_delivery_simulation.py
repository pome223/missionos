"""Operator-minimal status for reviewed simulated delivery missions.

This layer decides whether an already-reviewed simulated delivery can remain
operator-minimal or must escalate to an operator. It consumes existing Mission
OS artifacts only; it does not start Gazebo, dispatch MAVLink/ROS, upload
missions, execute actuators, mutate simulators, or create approvals.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.runtime.delivery_episode_review import (
    DELIVERY_EPISODE_REVIEW_SCHEMA_VERSION,
    DELIVERY_REVIEW_BUCKET_AUTONOMY_GATE_FAILED,
    DELIVERY_REVIEW_BUCKET_BATTERY_RESERVE_VIOLATION,
    DELIVERY_REVIEW_BUCKET_DROPOFF_MISSING,
    DELIVERY_REVIEW_BUCKET_LANDING_ZONE_UNAVAILABLE,
    DELIVERY_REVIEW_BUCKET_ROUTE_CONSTRAINT_VIOLATION,
    DELIVERY_REVIEW_BUCKET_TELEMETRY_MISSING,
    DELIVERY_REVIEW_BUCKET_TELEMETRY_STALE,
    DELIVERY_REVIEW_BUCKET_VEHICLE_HEALTH_UNSAFE,
    DELIVERY_SCORECARD_SCHEMA_VERSION,
    DeliveryEpisodeReview,
    DeliveryScorecard,
)
from src.runtime.delivery_mission_contract import (
    DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION,
    DeliveryMissionContract,
)
from src.runtime.delivery_recovery_decision import (
    DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION,
    DeliveryRecoveryAction,
    DeliveryRecoveryDecision,
)
from src.runtime.hil_telemetry_review import (
    HIL_TELEMETRY_REVIEW_SCHEMA_VERSION,
    HilTelemetryReview,
)
from src.runtime.simulated_delivery_episode import (
    SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION,
    SimulatedDeliveryEpisode,
)
from src.runtime.task_store import TaskStore, get_task_store
from src.runtime.toy_grid_world import (
    TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION,
    ToyGridWorldAutonomyGateResult,
)

OPERATOR_MINIMAL_DELIVERY_SIMULATION_STATUS_SCHEMA_VERSION = (
    "operator_minimal_delivery_simulation_status.v1"
)
OPERATOR_ESCALATION_REVIEW_SCHEMA_VERSION = "operator_escalation_review.v1"

OPERATOR_ESCALATION_TRIGGER_STALE_TELEMETRY = "stale_telemetry"
OPERATOR_ESCALATION_TRIGGER_MISSING_TELEMETRY = "missing_telemetry"
OPERATOR_ESCALATION_TRIGGER_FAILED_AUTONOMY_GATE = "failed_autonomy_gate"
OPERATOR_ESCALATION_TRIGGER_FAILED_DELIVERY_SCORECARD = "failed_delivery_scorecard"
OPERATOR_ESCALATION_TRIGGER_MISSING_DROPOFF = "missing_dropoff_evidence"
OPERATOR_ESCALATION_TRIGGER_BATTERY_RESERVE_VIOLATION = "battery_reserve_violation"
OPERATOR_ESCALATION_TRIGGER_ABORT_RECOMMENDATION = "abort_recommendation"
OPERATOR_ESCALATION_TRIGGER_RETURN_TO_HOME_RECOMMENDED = "return_to_home_recommended"
OPERATOR_ESCALATION_TRIGGER_ALTERNATE_LANDING_PROPOSAL = "alternate_landing_proposal"
OPERATOR_ESCALATION_TRIGGER_REROUTE_PROPOSAL = "reroute_proposal"
OPERATOR_ESCALATION_TRIGGER_ROUTE_POLICY_VIOLATION = "route_policy_violation"
OPERATOR_ESCALATION_TRIGGER_LANDING_ZONE_POLICY_VIOLATION = (
    "landing_zone_policy_violation"
)
OPERATOR_ESCALATION_TRIGGER_UNSAFE_VEHICLE_HEALTH = "unsafe_vehicle_health"
OPERATOR_ESCALATION_TRIGGER_OPERATOR_ESCALATION_REQUIRED = (
    "operator_escalation_required"
)


class OperatorMinimalDeliverySimulationError(RuntimeError):
    """Raised when operator-minimal status cannot be built safely."""


class OperatorMinimalDeliverySimulationStatusValue(str, Enum):
    COMPLETED_WITHOUT_OPERATOR_INTERVENTION = "completed_without_operator_intervention"
    CONTINUE_WITHOUT_OPERATOR_INTERVENTION = "continue_without_operator_intervention"
    OPERATOR_ESCALATION_REQUIRED = "operator_escalation_required"


class OperatorEscalationSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


_FORBIDDEN_COMMAND_KEYS = frozenset(
    {
        "action",
        "actuator",
        "actuator_execution_allowed",
        "attitude_setpoint",
        "command",
        "command_payload_allowed",
        "dispatch",
        "execute",
        "gazebo_mutation",
        "landing_command",
        "mavlink_command",
        "mission_upload",
        "physical_execution_invoked",
        "position_setpoint",
        "return_to_home_command",
        "ros_action",
        "setpoint",
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
        raise OperatorMinimalDeliverySimulationError(
            "operator-minimal delivery simulation refused command-like keys: "
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


def _as_tuple(values: list[str] | tuple[str, ...] | set[str]) -> tuple[str, ...]:
    return tuple(sorted({str(item).strip() for item in values if str(item).strip()}))


class OperatorEscalationFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trigger: str
    severity: OperatorEscalationSeverity
    reason: str
    evidence_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _reject_command_like_metadata(self) -> "OperatorEscalationFinding":
        _raise_for_command_like_keys(self.metadata, root="finding.metadata")
        return self


class OperatorEscalationReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[OPERATOR_ESCALATION_REVIEW_SCHEMA_VERSION] = (
        OPERATOR_ESCALATION_REVIEW_SCHEMA_VERSION
    )
    review_id: str
    delivery_mission_contract_ref: str
    simulated_delivery_episode_ref: str
    delivery_scorecard_ref: str
    delivery_episode_review_ref: str
    delivery_recovery_decision_ref: str
    hil_telemetry_review_ref: str
    autonomy_gate_result_ref: str
    escalation_required: Literal[True] = True
    escalation_triggers: tuple[str, ...]
    findings: tuple[OperatorEscalationFinding, ...]
    evidence_refs: tuple[str, ...]
    created_at: datetime
    rule_based: Literal[True] = True
    llm_judge_used: Literal[False] = False
    simulation_review_only: Literal[True] = True
    command_payload_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    approval_promotion_reuse_created: Literal[False] = False


class OperatorMinimalDeliverySimulationStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        OPERATOR_MINIMAL_DELIVERY_SIMULATION_STATUS_SCHEMA_VERSION
    ] = OPERATOR_MINIMAL_DELIVERY_SIMULATION_STATUS_SCHEMA_VERSION
    status_id: str
    delivery_mission_contract_ref: str
    simulated_delivery_episode_ref: str
    delivery_scorecard_ref: str
    delivery_episode_review_ref: str
    delivery_recovery_decision_ref: str
    hil_telemetry_review_ref: str
    autonomy_gate_result_ref: str
    operator_escalation_review_ref: str = ""
    status: OperatorMinimalDeliverySimulationStatusValue
    operator_intervention_required: bool
    escalation_triggers: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    created_at: datetime
    delivery_mission_contract_schema_version: Literal[
        DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    ] = DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    simulated_delivery_episode_schema_version: Literal[
        SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION
    ] = SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION
    delivery_scorecard_schema_version: Literal[DELIVERY_SCORECARD_SCHEMA_VERSION] = (
        DELIVERY_SCORECARD_SCHEMA_VERSION
    )
    delivery_episode_review_schema_version: Literal[
        DELIVERY_EPISODE_REVIEW_SCHEMA_VERSION
    ] = DELIVERY_EPISODE_REVIEW_SCHEMA_VERSION
    delivery_recovery_decision_schema_version: Literal[
        DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION
    hil_telemetry_review_schema_version: Literal[
        HIL_TELEMETRY_REVIEW_SCHEMA_VERSION
    ] = HIL_TELEMETRY_REVIEW_SCHEMA_VERSION
    autonomy_gate_result_schema_version: Literal[
        TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION
    ] = TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION
    rule_based: Literal[True] = True
    llm_judge_used: Literal[False] = False
    simulation_review_only: Literal[True] = True
    command_payload_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    approval_promotion_reuse_created: Literal[False] = False


def _to_contract(
    value: DeliveryMissionContract | Mapping[str, Any],
) -> DeliveryMissionContract:
    if isinstance(value, DeliveryMissionContract):
        return value
    return DeliveryMissionContract.model_validate(dict(value))


def _to_episode(
    value: SimulatedDeliveryEpisode | Mapping[str, Any],
) -> SimulatedDeliveryEpisode:
    if isinstance(value, SimulatedDeliveryEpisode):
        return value
    return SimulatedDeliveryEpisode.model_validate(dict(value))


def _to_scorecard(value: DeliveryScorecard | Mapping[str, Any]) -> DeliveryScorecard:
    if isinstance(value, DeliveryScorecard):
        return value
    return DeliveryScorecard.model_validate(dict(value))


def _to_episode_review(
    value: DeliveryEpisodeReview | Mapping[str, Any],
) -> DeliveryEpisodeReview:
    if isinstance(value, DeliveryEpisodeReview):
        return value
    return DeliveryEpisodeReview.model_validate(dict(value))


def _to_recovery_decision(
    value: DeliveryRecoveryDecision | Mapping[str, Any],
) -> DeliveryRecoveryDecision:
    if isinstance(value, DeliveryRecoveryDecision):
        return value
    return DeliveryRecoveryDecision.model_validate(dict(value))


def _to_hil_review(value: HilTelemetryReview | Mapping[str, Any]) -> HilTelemetryReview:
    if isinstance(value, HilTelemetryReview):
        return value
    return HilTelemetryReview.model_validate(dict(value))


def _to_autonomy_gate(
    value: ToyGridWorldAutonomyGateResult | Mapping[str, Any],
) -> ToyGridWorldAutonomyGateResult:
    if isinstance(value, ToyGridWorldAutonomyGateResult):
        return value
    return ToyGridWorldAutonomyGateResult.model_validate(dict(value))


def _contract_ref(contract: DeliveryMissionContract) -> str:
    return f"delivery_mission_contract:{contract.contract_id}"


def _episode_ref(episode: SimulatedDeliveryEpisode) -> str:
    return f"simulated_delivery_episode:{episode.episode_id}"


def _scorecard_ref(scorecard: DeliveryScorecard) -> str:
    return f"delivery_scorecard:{scorecard.scorecard_id}"


def _episode_review_ref(review: DeliveryEpisodeReview) -> str:
    return f"delivery_episode_review:{review.review_id}"


def _recovery_decision_ref(decision: DeliveryRecoveryDecision) -> str:
    return f"delivery_recovery_decision:{decision.decision_id}"


def _hil_ref(review: HilTelemetryReview) -> str:
    return f"hil_telemetry_review:{review.review_id}"


def _autonomy_gate_ref(gate: ToyGridWorldAutonomyGateResult) -> str:
    return f"autonomy_gate_result:{gate.gate_id}"


def _validate_refs(
    *,
    contract: DeliveryMissionContract,
    episode: SimulatedDeliveryEpisode,
    scorecard: DeliveryScorecard,
    episode_review: DeliveryEpisodeReview,
    recovery_decision: DeliveryRecoveryDecision,
    hil_review: HilTelemetryReview,
    autonomy_gate: ToyGridWorldAutonomyGateResult,
) -> None:
    contract_ref = _contract_ref(contract)
    episode_ref = _episode_ref(episode)
    scorecard_ref = _scorecard_ref(scorecard)
    hil_ref = _hil_ref(hil_review)
    gate_ref = _autonomy_gate_ref(autonomy_gate)

    if episode.delivery_mission_contract_id != contract.contract_id:
        raise OperatorMinimalDeliverySimulationError("episode/contract ref mismatch")
    if scorecard.delivery_mission_contract_ref != contract_ref:
        raise OperatorMinimalDeliverySimulationError("scorecard/contract ref mismatch")
    if scorecard.simulated_delivery_episode_ref != episode_ref:
        raise OperatorMinimalDeliverySimulationError("scorecard/episode ref mismatch")
    if scorecard.hil_telemetry_review_ref != hil_ref:
        raise OperatorMinimalDeliverySimulationError("scorecard/HIL ref mismatch")
    if scorecard.autonomy_gate_result_ref != gate_ref:
        raise OperatorMinimalDeliverySimulationError("scorecard/gate ref mismatch")
    if episode_review.scorecard_ref != scorecard_ref:
        raise OperatorMinimalDeliverySimulationError("review/scorecard ref mismatch")
    if episode_review.delivery_mission_contract_ref != contract_ref:
        raise OperatorMinimalDeliverySimulationError("review/contract ref mismatch")
    if episode_review.simulated_delivery_episode_ref != episode_ref:
        raise OperatorMinimalDeliverySimulationError("review/episode ref mismatch")
    if episode_review.hil_telemetry_review_ref != hil_ref:
        raise OperatorMinimalDeliverySimulationError("review/HIL ref mismatch")
    if episode_review.autonomy_gate_result_ref != gate_ref:
        raise OperatorMinimalDeliverySimulationError("review/gate ref mismatch")
    if recovery_decision.delivery_mission_contract_id != contract.contract_id:
        raise OperatorMinimalDeliverySimulationError("decision/contract ref mismatch")
    if recovery_decision.simulated_delivery_episode_id != episode.episode_id:
        raise OperatorMinimalDeliverySimulationError("decision/episode ref mismatch")
    if recovery_decision.delivery_scorecard_id != scorecard.scorecard_id:
        raise OperatorMinimalDeliverySimulationError("decision/scorecard ref mismatch")
    if recovery_decision.delivery_episode_review_id != episode_review.review_id:
        raise OperatorMinimalDeliverySimulationError("decision/review ref mismatch")
    if recovery_decision.hil_telemetry_review_id != hil_review.review_id:
        raise OperatorMinimalDeliverySimulationError("decision/HIL ref mismatch")
    if recovery_decision.autonomy_gate_result_id != autonomy_gate.gate_id:
        raise OperatorMinimalDeliverySimulationError("decision/gate ref mismatch")


def _escalation_trigger_set(
    *,
    scorecard: DeliveryScorecard,
    episode_review: DeliveryEpisodeReview,
    recovery_decision: DeliveryRecoveryDecision,
    autonomy_gate: ToyGridWorldAutonomyGateResult,
) -> set[str]:
    triggers: set[str] = set()
    blocked = set(episode_review.blocked_buckets)
    if DELIVERY_REVIEW_BUCKET_TELEMETRY_STALE in blocked:
        triggers.add(OPERATOR_ESCALATION_TRIGGER_STALE_TELEMETRY)
    if DELIVERY_REVIEW_BUCKET_TELEMETRY_MISSING in blocked:
        triggers.add(OPERATOR_ESCALATION_TRIGGER_MISSING_TELEMETRY)
    if (
        DELIVERY_REVIEW_BUCKET_AUTONOMY_GATE_FAILED in blocked
        or not autonomy_gate.passed
    ):
        triggers.add(OPERATOR_ESCALATION_TRIGGER_FAILED_AUTONOMY_GATE)
    if not scorecard.passed:
        triggers.add(OPERATOR_ESCALATION_TRIGGER_FAILED_DELIVERY_SCORECARD)
    if DELIVERY_REVIEW_BUCKET_DROPOFF_MISSING in blocked:
        triggers.add(OPERATOR_ESCALATION_TRIGGER_MISSING_DROPOFF)
    if DELIVERY_REVIEW_BUCKET_BATTERY_RESERVE_VIOLATION in blocked:
        triggers.add(OPERATOR_ESCALATION_TRIGGER_BATTERY_RESERVE_VIOLATION)
    if recovery_decision.abort_recommended or recovery_decision.abort_proposed:
        triggers.add(OPERATOR_ESCALATION_TRIGGER_ABORT_RECOMMENDATION)
    if recovery_decision.return_to_home_recommended:
        triggers.add(OPERATOR_ESCALATION_TRIGGER_RETURN_TO_HOME_RECOMMENDED)
    if recovery_decision.alternate_landing_proposal:
        triggers.add(OPERATOR_ESCALATION_TRIGGER_ALTERNATE_LANDING_PROPOSAL)
    if recovery_decision.reroute_proposal:
        triggers.add(OPERATOR_ESCALATION_TRIGGER_REROUTE_PROPOSAL)
    if DELIVERY_REVIEW_BUCKET_ROUTE_CONSTRAINT_VIOLATION in blocked:
        triggers.add(OPERATOR_ESCALATION_TRIGGER_ROUTE_POLICY_VIOLATION)
    if DELIVERY_REVIEW_BUCKET_LANDING_ZONE_UNAVAILABLE in blocked:
        triggers.add(OPERATOR_ESCALATION_TRIGGER_LANDING_ZONE_POLICY_VIOLATION)
    if DELIVERY_REVIEW_BUCKET_VEHICLE_HEALTH_UNSAFE in blocked:
        triggers.add(OPERATOR_ESCALATION_TRIGGER_UNSAFE_VEHICLE_HEALTH)
    if recovery_decision.operator_escalation_required:
        triggers.add(OPERATOR_ESCALATION_TRIGGER_OPERATOR_ESCALATION_REQUIRED)
    return triggers


def _finding(
    trigger: str,
    *,
    evidence_refs: tuple[str, ...],
) -> OperatorEscalationFinding:
    return OperatorEscalationFinding(
        trigger=trigger,
        severity=OperatorEscalationSeverity.BLOCKING,
        reason=f"{trigger}_requires_operator_review",
        evidence_refs=evidence_refs,
        metadata={"operator_review_required": True},
    )


def build_operator_minimal_delivery_simulation_status(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    simulated_delivery_episode: SimulatedDeliveryEpisode | Mapping[str, Any],
    delivery_scorecard: DeliveryScorecard | Mapping[str, Any],
    delivery_episode_review: DeliveryEpisodeReview | Mapping[str, Any],
    delivery_recovery_decision: DeliveryRecoveryDecision | Mapping[str, Any],
    hil_telemetry_review: HilTelemetryReview | Mapping[str, Any],
    autonomy_gate_result: ToyGridWorldAutonomyGateResult | Mapping[str, Any],
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, OperatorMinimalDeliverySimulationStatus | OperatorEscalationReview]:
    """Build operator-minimal status and optional escalation review."""

    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    contract = _to_contract(delivery_mission_contract)
    episode = _to_episode(simulated_delivery_episode)
    scorecard = _to_scorecard(delivery_scorecard)
    episode_review = _to_episode_review(delivery_episode_review)
    recovery_decision = _to_recovery_decision(delivery_recovery_decision)
    hil_review = _to_hil_review(hil_telemetry_review)
    autonomy_gate = _to_autonomy_gate(autonomy_gate_result)
    created_at = _utc(now)

    _validate_refs(
        contract=contract,
        episode=episode,
        scorecard=scorecard,
        episode_review=episode_review,
        recovery_decision=recovery_decision,
        hil_review=hil_review,
        autonomy_gate=autonomy_gate,
    )
    contract_ref = _contract_ref(contract)
    episode_ref = _episode_ref(episode)
    scorecard_ref = _scorecard_ref(scorecard)
    review_ref = _episode_review_ref(episode_review)
    decision_ref = _recovery_decision_ref(recovery_decision)
    hil_ref = _hil_ref(hil_review)
    gate_ref = _autonomy_gate_ref(autonomy_gate)
    evidence_refs = _as_tuple(
        [
            contract_ref,
            episode_ref,
            scorecard_ref,
            review_ref,
            decision_ref,
            hil_ref,
            gate_ref,
            *scorecard.blocked_buckets,
            *episode_review.blocked_buckets,
        ]
    )
    triggers = _as_tuple(
        _escalation_trigger_set(
            scorecard=scorecard,
            episode_review=episode_review,
            recovery_decision=recovery_decision,
            autonomy_gate=autonomy_gate,
        )
    )

    if (
        scorecard.delivery_completed
        and scorecard.passed
        and episode_review.passed
        and recovery_decision.primary_action
        is DeliveryRecoveryAction.COMPLETED_NO_RECOVERY_NEEDED
        and not triggers
    ):
        status_value = (
            OperatorMinimalDeliverySimulationStatusValue.COMPLETED_WITHOUT_OPERATOR_INTERVENTION
        )
    elif (
        recovery_decision.primary_action is DeliveryRecoveryAction.CONTINUE
        and not recovery_decision.operator_escalation_required
        and not triggers
    ):
        status_value = (
            OperatorMinimalDeliverySimulationStatusValue.CONTINUE_WITHOUT_OPERATOR_INTERVENTION
        )
    else:
        status_value = (
            OperatorMinimalDeliverySimulationStatusValue.OPERATOR_ESCALATION_REQUIRED
        )

    escalation_review: OperatorEscalationReview | None = None
    if (
        status_value
        is OperatorMinimalDeliverySimulationStatusValue.OPERATOR_ESCALATION_REQUIRED
    ):
        if not triggers:
            triggers = (OPERATOR_ESCALATION_TRIGGER_OPERATOR_ESCALATION_REQUIRED,)
        review_payload = {
            "contract_ref": contract_ref,
            "episode_ref": episode_ref,
            "scorecard_ref": scorecard_ref,
            "episode_review_ref": review_ref,
            "decision_ref": decision_ref,
            "triggers": triggers,
        }
        escalation_review = OperatorEscalationReview(
            review_id=_stable_id("operator_escalation_review", review_payload),
            delivery_mission_contract_ref=contract_ref,
            simulated_delivery_episode_ref=episode_ref,
            delivery_scorecard_ref=scorecard_ref,
            delivery_episode_review_ref=review_ref,
            delivery_recovery_decision_ref=decision_ref,
            hil_telemetry_review_ref=hil_ref,
            autonomy_gate_result_ref=gate_ref,
            escalation_triggers=triggers,
            findings=tuple(
                _finding(trigger, evidence_refs=evidence_refs) for trigger in triggers
            ),
            evidence_refs=evidence_refs,
            created_at=created_at,
        )

    status_payload = {
        "contract_ref": contract_ref,
        "episode_ref": episode_ref,
        "scorecard_ref": scorecard_ref,
        "episode_review_ref": review_ref,
        "decision_ref": decision_ref,
        "status": status_value.value,
        "triggers": triggers,
    }
    status = OperatorMinimalDeliverySimulationStatus(
        status_id=_stable_id(
            "operator_minimal_delivery_simulation_status", status_payload
        ),
        delivery_mission_contract_ref=contract_ref,
        simulated_delivery_episode_ref=episode_ref,
        delivery_scorecard_ref=scorecard_ref,
        delivery_episode_review_ref=review_ref,
        delivery_recovery_decision_ref=decision_ref,
        hil_telemetry_review_ref=hil_ref,
        autonomy_gate_result_ref=gate_ref,
        operator_escalation_review_ref=(
            f"operator_escalation_review:{escalation_review.review_id}"
            if escalation_review
            else ""
        ),
        status=status_value,
        operator_intervention_required=(
            status_value
            is OperatorMinimalDeliverySimulationStatusValue.OPERATOR_ESCALATION_REQUIRED
        ),
        escalation_triggers=triggers,
        evidence_refs=evidence_refs,
        created_at=created_at,
    )
    result: dict[
        str, OperatorMinimalDeliverySimulationStatus | OperatorEscalationReview
    ] = {"operator_minimal_delivery_simulation_status": status}
    if escalation_review is not None:
        result["operator_escalation_review"] = escalation_review
    return result


def attach_operator_minimal_delivery_simulation_status(
    task_id: str,
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    simulated_delivery_episode: SimulatedDeliveryEpisode | Mapping[str, Any],
    delivery_scorecard: DeliveryScorecard | Mapping[str, Any],
    delivery_episode_review: DeliveryEpisodeReview | Mapping[str, Any],
    delivery_recovery_decision: DeliveryRecoveryDecision | Mapping[str, Any],
    hil_telemetry_review: HilTelemetryReview | Mapping[str, Any],
    autonomy_gate_result: ToyGridWorldAutonomyGateResult | Mapping[str, Any],
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach operator-minimal status without mutating task status."""

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise OperatorMinimalDeliverySimulationError(
            f"task {task_id} not found; cannot attach operator-minimal status"
        )
    built = build_operator_minimal_delivery_simulation_status(
        delivery_mission_contract=delivery_mission_contract,
        simulated_delivery_episode=simulated_delivery_episode,
        delivery_scorecard=delivery_scorecard,
        delivery_episode_review=delivery_episode_review,
        delivery_recovery_decision=delivery_recovery_decision,
        hil_telemetry_review=hil_telemetry_review,
        autonomy_gate_result=autonomy_gate_result,
        now=now,
        metadata=metadata,
    )
    artifacts = {key: value.model_dump(mode="json") for key, value in built.items()}
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise OperatorMinimalDeliverySimulationError(
            f"task {task_id} disappeared while attaching operator-minimal status"
        )
    return artifacts


__all__ = [
    "OPERATOR_ESCALATION_REVIEW_SCHEMA_VERSION",
    "OPERATOR_MINIMAL_DELIVERY_SIMULATION_STATUS_SCHEMA_VERSION",
    "OPERATOR_ESCALATION_TRIGGER_ABORT_RECOMMENDATION",
    "OPERATOR_ESCALATION_TRIGGER_ALTERNATE_LANDING_PROPOSAL",
    "OPERATOR_ESCALATION_TRIGGER_BATTERY_RESERVE_VIOLATION",
    "OPERATOR_ESCALATION_TRIGGER_FAILED_AUTONOMY_GATE",
    "OPERATOR_ESCALATION_TRIGGER_FAILED_DELIVERY_SCORECARD",
    "OPERATOR_ESCALATION_TRIGGER_LANDING_ZONE_POLICY_VIOLATION",
    "OPERATOR_ESCALATION_TRIGGER_MISSING_DROPOFF",
    "OPERATOR_ESCALATION_TRIGGER_MISSING_TELEMETRY",
    "OPERATOR_ESCALATION_TRIGGER_OPERATOR_ESCALATION_REQUIRED",
    "OPERATOR_ESCALATION_TRIGGER_REROUTE_PROPOSAL",
    "OPERATOR_ESCALATION_TRIGGER_RETURN_TO_HOME_RECOMMENDED",
    "OPERATOR_ESCALATION_TRIGGER_ROUTE_POLICY_VIOLATION",
    "OPERATOR_ESCALATION_TRIGGER_STALE_TELEMETRY",
    "OPERATOR_ESCALATION_TRIGGER_UNSAFE_VEHICLE_HEALTH",
    "OperatorEscalationFinding",
    "OperatorEscalationReview",
    "OperatorEscalationSeverity",
    "OperatorMinimalDeliverySimulationError",
    "OperatorMinimalDeliverySimulationStatus",
    "OperatorMinimalDeliverySimulationStatusValue",
    "attach_operator_minimal_delivery_simulation_status",
    "build_operator_minimal_delivery_simulation_status",
]
