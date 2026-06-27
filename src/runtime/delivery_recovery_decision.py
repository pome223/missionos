"""Rule-based recovery decisions for simulated delivery missions.

``delivery_recovery_decision.v1`` converts delivery progress review state into
operator-visible recovery recommendations. The artifact is not an execution
surface: it does not send return-to-home, hold, abort, alternate-dropoff,
Gazebo, ROS, MAVLink, setpoint, or actuator commands.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
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
    DELIVERY_SCORECARD_SCHEMA_VERSION,
    DeliveryEpisodeReview,
    DeliveryScorecard,
)
from src.runtime.delivery_mission_contract import (
    DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION,
    DeliveryMissionContract,
)
from src.runtime.delivery_mission_policy_review import (
    DELIVERY_POLICY_BUCKET_BATTERY_ABORT_RECOMMENDED,
    DELIVERY_POLICY_BUCKET_BATTERY_RETURN_HOME_RECOMMENDED,
)
from src.runtime.delivery_progress_review import (
    DELIVERY_PROGRESS_BUCKET_HIL_REVIEW_BLOCKED,
    DELIVERY_PROGRESS_BUCKET_HIL_TELEMETRY_STALE,
    DELIVERY_PROGRESS_BUCKET_TELEMETRY_MISSING,
    DELIVERY_PROGRESS_REVIEW_SCHEMA_VERSION,
    DeliveryProgressReview,
    DeliveryProgressStatus,
)
from src.runtime.simulated_delivery_episode import (
    SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION,
    SimulatedDeliveryEpisode,
)
from src.runtime.hil_telemetry_review import (
    HIL_TELEMETRY_REVIEW_SCHEMA_VERSION,
    HilTelemetryReview,
)
from src.runtime.task_store import TaskStore, get_task_store
from src.runtime.toy_grid_world import (
    TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION,
    ToyGridWorldAutonomyGateResult,
)

DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION = "delivery_recovery_decision.v1"


class DeliveryRecoveryAction(str, Enum):
    CONTINUE = "continue"
    COMPLETED_NO_RECOVERY_NEEDED = "completed_no_recovery_needed"
    HOLD = "hold"
    HOLD_RECOMMENDED = "hold_recommended"
    RETURN_TO_HOME_RECOMMENDED = "return_to_home_recommended"
    ABORT = "abort"
    ABORT_RECOMMENDED = "abort_recommended"
    OPERATOR_ESCALATION_REQUIRED = "operator_escalation_required"
    ALTERNATE_LANDING_PROPOSAL = "alternate_landing_proposal"
    ALTERNATE_DROPOFF_PROPOSAL = "alternate_dropoff_proposal"
    REROUTE_PROPOSAL = "reroute_proposal"


class DeliveryRecoverySeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


class DeliveryRecoveryDecisionError(RuntimeError):
    """Raised when a recovery decision cannot be built safely."""


_FORBIDDEN_COMMAND_KEYS = frozenset(
    {
        "action",
        "actuator",
        "actuator_execution_allowed",
        "attitude_setpoint",
        "command",
        "command_payload_allowed",
        "dispatch",
        "dispatch_implementation_present",
        "entity_mutation",
        "execute",
        "gazebo_mutation",
        "joint",
        "landing_command",
        "live_execution_allowed",
        "mavlink_command",
        "mavlink_dispatch_allowed",
        "mission_upload",
        "physical_execution_invoked",
        "position_setpoint",
        "return_to_home_command",
        "ros_action",
        "ros_dispatch_allowed",
        "ros_topic",
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
        raise DeliveryRecoveryDecisionError(
            "delivery recovery decision refused command-like keys: "
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
        sorted({str(item).strip() for item in (values or ()) if str(item).strip()})
    )


class DeliveryRecoveryRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: DeliveryRecoveryAction
    severity: DeliveryRecoverySeverity
    reason: str
    evidence_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _reject_command_like_metadata(self) -> "DeliveryRecoveryRecommendation":
        _raise_for_command_like_keys(
            self.metadata,
            root="recommendation.metadata",
        )
        return self


class DeliveryRecoveryDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION] = (
        DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION
    )
    decision_id: str
    delivery_mission_contract_id: str
    delivery_mission_id: str
    simulated_delivery_episode_id: str
    delivery_progress_review_id: str = ""
    delivery_scorecard_id: str = ""
    delivery_episode_review_id: str = ""
    hil_telemetry_review_id: str = ""
    autonomy_gate_result_id: str = ""
    decision_source: Literal["delivery_progress_review", "delivery_episode_review"] = (
        "delivery_progress_review"
    )
    primary_action: DeliveryRecoveryAction
    recommendations: tuple[DeliveryRecoveryRecommendation, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    completed_no_recovery_needed: bool = False
    continue_recommended: bool = False
    hold_proposed: bool = False
    hold_recommended: bool = False
    return_to_home_recommended: bool = False
    abort_proposed: bool = False
    abort_recommended: bool = False
    operator_escalation_required: bool = False
    alternate_landing_proposal: bool = False
    alternate_dropoff_proposal: bool = False
    reroute_proposal: bool = False
    alternate_dropoff_refs: tuple[str, ...] = ()
    alternate_landing_refs: tuple[str, ...] = ()
    reroute_refs: tuple[str, ...] = ()
    created_at: datetime
    delivery_mission_contract_schema_version: Literal[
        DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    ] = DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    simulated_delivery_episode_schema_version: Literal[
        SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION
    ] = SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION
    delivery_progress_review_schema_version: Literal[
        DELIVERY_PROGRESS_REVIEW_SCHEMA_VERSION
    ] = DELIVERY_PROGRESS_REVIEW_SCHEMA_VERSION
    delivery_scorecard_schema_version: Literal[DELIVERY_SCORECARD_SCHEMA_VERSION] = (
        DELIVERY_SCORECARD_SCHEMA_VERSION
    )
    delivery_episode_review_schema_version: Literal[
        DELIVERY_EPISODE_REVIEW_SCHEMA_VERSION
    ] = DELIVERY_EPISODE_REVIEW_SCHEMA_VERSION
    hil_telemetry_review_schema_version: Literal[
        HIL_TELEMETRY_REVIEW_SCHEMA_VERSION
    ] = HIL_TELEMETRY_REVIEW_SCHEMA_VERSION
    autonomy_gate_result_schema_version: Literal[
        TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION
    ] = TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION
    rule_based: Literal[True] = True
    llm_judge_used: Literal[False] = False
    simulation_only: Literal[True] = True
    telemetry_only: Literal[True] = True
    read_only: Literal[True] = True
    recommendations_only: Literal[True] = True
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    stronger_execution_allowed: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)


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


def _to_progress_review(
    value: DeliveryProgressReview | Mapping[str, Any],
) -> DeliveryProgressReview:
    if isinstance(value, DeliveryProgressReview):
        return value
    return DeliveryProgressReview.model_validate(dict(value))


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


def _source_refs(
    *,
    contract: DeliveryMissionContract,
    episode: SimulatedDeliveryEpisode,
    progress_review: DeliveryProgressReview,
    extra_source_refs: Sequence[str] | None,
) -> tuple[str, ...]:
    return _as_tuple(
        [
            f"delivery_mission_contract:{contract.contract_id}",
            f"simulated_delivery_episode:{episode.episode_id}",
            f"delivery_progress_review:{progress_review.progress_review_id}",
            *progress_review.telemetry_refs,
            *progress_review.gate_refs,
            *(extra_source_refs or ()),
        ]
    )


def _episode_reasons(episode: SimulatedDeliveryEpisode) -> set[str]:
    return set(episode.blocked_reasons) | set(episode.warning_reasons)


def _scorecard_ref(scorecard: DeliveryScorecard) -> str:
    return f"delivery_scorecard:{scorecard.scorecard_id}"


def _episode_review_ref(review: DeliveryEpisodeReview) -> str:
    return f"delivery_episode_review:{review.review_id}"


def _hil_ref(review: HilTelemetryReview) -> str:
    return f"hil_telemetry_review:{review.review_id}"


def _autonomy_gate_ref(gate: ToyGridWorldAutonomyGateResult) -> str:
    return f"autonomy_gate_result:{gate.gate_id}"


def build_delivery_recovery_decision(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    simulated_delivery_episode: SimulatedDeliveryEpisode | Mapping[str, Any],
    delivery_progress_review: DeliveryProgressReview | Mapping[str, Any],
    alternate_dropoff_refs: Sequence[str] | None = None,
    evidence_refs: Sequence[str] | None = None,
    source_refs: Sequence[str] | None = None,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> DeliveryRecoveryDecision:
    """Build rule-based recovery recommendations from progress review state."""

    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    contract = _to_contract(delivery_mission_contract)
    episode = _to_episode(simulated_delivery_episode)
    progress_review = _to_progress_review(delivery_progress_review)
    created_at = _utc(now)
    if episode.delivery_mission_contract_id != contract.contract_id:
        raise DeliveryRecoveryDecisionError("simulated episode contract_id mismatch")
    if episode.mission_id != contract.mission_id:
        raise DeliveryRecoveryDecisionError("simulated episode mission_id mismatch")
    if progress_review.delivery_mission_contract_id != contract.contract_id:
        raise DeliveryRecoveryDecisionError("progress review contract_id mismatch")
    if progress_review.delivery_mission_id != contract.mission_id:
        raise DeliveryRecoveryDecisionError("progress review mission_id mismatch")
    if progress_review.simulated_delivery_episode_id != episode.episode_id:
        raise DeliveryRecoveryDecisionError("progress review episode_id mismatch")

    refs = _as_tuple(
        [
            *progress_review.telemetry_refs,
            *progress_review.episode_refs,
            *progress_review.scenario_refs,
            *progress_review.gate_refs,
            *(evidence_refs or ()),
        ]
    )
    recommendations: list[DeliveryRecoveryRecommendation] = []

    def add(
        action: DeliveryRecoveryAction,
        severity: DeliveryRecoverySeverity,
        reason: str,
        *,
        extra_refs: Sequence[str] | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        recommendations.append(
            DeliveryRecoveryRecommendation(
                action=action,
                severity=severity,
                reason=reason,
                evidence_refs=_as_tuple([*refs, *(extra_refs or ())]),
                metadata={
                    "recommendation_only": True,
                    "no_command_surface": True,
                    **dict(detail or {}),
                },
            )
        )

    episode_reasons = _episode_reasons(episode)
    progress_reasons = set(progress_review.blocked_reasons) | set(
        progress_review.warning_reasons
    )
    hold_recommended = False
    return_to_home_recommended = episode.return_to_home_recommended
    abort_recommended = episode.abort_recommended
    operator_escalation_required = episode.operator_escalation_required
    alternate_refs = _as_tuple(alternate_dropoff_refs)
    alternate_dropoff_proposal = False

    if progress_review.status is DeliveryProgressStatus.BLOCKED:
        operator_escalation_required = True
        if progress_reasons & {
            DELIVERY_PROGRESS_BUCKET_TELEMETRY_MISSING,
            DELIVERY_PROGRESS_BUCKET_HIL_REVIEW_BLOCKED,
            DELIVERY_PROGRESS_BUCKET_HIL_TELEMETRY_STALE,
        }:
            hold_recommended = True
        if DELIVERY_POLICY_BUCKET_BATTERY_ABORT_RECOMMENDED in episode_reasons:
            abort_recommended = True
        if DELIVERY_POLICY_BUCKET_BATTERY_RETURN_HOME_RECOMMENDED in episode_reasons:
            return_to_home_recommended = True
        if (
            progress_review.pickup_reached
            and not progress_review.dropoff_reached
            and not abort_recommended
            and alternate_refs
        ):
            alternate_dropoff_proposal = True
    elif episode.return_to_home_recommended:
        return_to_home_recommended = True

    if abort_recommended:
        add(
            DeliveryRecoveryAction.ABORT_RECOMMENDED,
            DeliveryRecoverySeverity.BLOCKING,
            "delivery_policy_recommends_abort",
            detail={"source": "simulated_delivery_episode"},
        )
    if hold_recommended:
        add(
            DeliveryRecoveryAction.HOLD_RECOMMENDED,
            DeliveryRecoverySeverity.WARNING,
            "delivery_progress_should_hold_for_telemetry_or_gate_recovery",
            detail={"progress_blocked_reasons": list(progress_review.blocked_reasons)},
        )
    if return_to_home_recommended:
        add(
            DeliveryRecoveryAction.RETURN_TO_HOME_RECOMMENDED,
            DeliveryRecoverySeverity.WARNING,
            "delivery_policy_recommends_return_to_home",
            detail={"source": "simulated_delivery_episode"},
        )
    if alternate_dropoff_proposal:
        add(
            DeliveryRecoveryAction.ALTERNATE_DROPOFF_PROPOSAL,
            DeliveryRecoverySeverity.WARNING,
            "delivery_progress_supports_alternate_dropoff_review",
            extra_refs=alternate_refs,
            detail={"alternate_dropoff_refs": list(alternate_refs)},
        )
    if operator_escalation_required:
        add(
            DeliveryRecoveryAction.OPERATOR_ESCALATION_REQUIRED,
            DeliveryRecoverySeverity.WARNING,
            "delivery_recovery_requires_operator_review",
        )
    if not recommendations:
        add(
            DeliveryRecoveryAction.CONTINUE,
            DeliveryRecoverySeverity.INFO,
            "delivery_progress_allows_continue_recommendation",
        )

    primary_action = recommendations[0].action
    payload = {
        "delivery_mission_contract_id": contract.contract_id,
        "simulated_delivery_episode_id": episode.episode_id,
        "delivery_progress_review_id": progress_review.progress_review_id,
        "recommendations": [
            {"action": item.action.value, "reason": item.reason}
            for item in recommendations
        ],
        "alternate_dropoff_refs": alternate_refs,
    }
    return DeliveryRecoveryDecision(
        decision_id=_stable_id("delivery_recovery_decision", payload),
        delivery_mission_contract_id=contract.contract_id,
        delivery_mission_id=contract.mission_id,
        simulated_delivery_episode_id=episode.episode_id,
        delivery_progress_review_id=progress_review.progress_review_id,
        primary_action=primary_action,
        recommendations=tuple(recommendations),
        evidence_refs=refs,
        source_refs=_source_refs(
            contract=contract,
            episode=episode,
            progress_review=progress_review,
            extra_source_refs=source_refs,
        ),
        continue_recommended=primary_action is DeliveryRecoveryAction.CONTINUE,
        hold_recommended=hold_recommended,
        return_to_home_recommended=return_to_home_recommended,
        abort_recommended=abort_recommended,
        operator_escalation_required=operator_escalation_required,
        alternate_dropoff_proposal=alternate_dropoff_proposal,
        alternate_dropoff_refs=alternate_refs,
        created_at=created_at,
        metadata={
            **metadata_payload,
            "artifact_only": True,
            "recovery_decision_only": True,
            "recommendations_only": True,
            "return_to_home_is_recommendation_only": True,
            "abort_is_recommendation_only": True,
            "hold_is_recommendation_only": True,
            "alternate_dropoff_is_proposal_only": True,
            "no_dispatch_surface": True,
            "no_entity_mutation": True,
        },
    )


def build_delivery_recovery_decision_from_episode_review(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    simulated_delivery_episode: SimulatedDeliveryEpisode | Mapping[str, Any],
    delivery_scorecard: DeliveryScorecard | Mapping[str, Any],
    delivery_episode_review: DeliveryEpisodeReview | Mapping[str, Any],
    hil_telemetry_review: HilTelemetryReview | Mapping[str, Any],
    autonomy_gate_result: ToyGridWorldAutonomyGateResult | Mapping[str, Any],
    alternate_landing_refs: Sequence[str] | None = None,
    reroute_refs: Sequence[str] | None = None,
    evidence_refs: Sequence[str] | None = None,
    source_refs: Sequence[str] | None = None,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> DeliveryRecoveryDecision:
    """Build recovery recommendations from delivery episode scorecard/review.

    The returned artifact is recommendation-only. Return-to-home, hold, abort,
    alternate landing, and reroute outcomes are proposals, not command paths.
    """

    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    contract = _to_contract(delivery_mission_contract)
    episode = _to_episode(simulated_delivery_episode)
    scorecard = _to_scorecard(delivery_scorecard)
    review = _to_episode_review(delivery_episode_review)
    hil_review = _to_hil_review(hil_telemetry_review)
    autonomy_gate = _to_autonomy_gate(autonomy_gate_result)
    created_at = _utc(now)

    contract_ref = f"delivery_mission_contract:{contract.contract_id}"
    episode_ref = f"simulated_delivery_episode:{episode.episode_id}"
    scorecard_ref = _scorecard_ref(scorecard)
    review_ref = _episode_review_ref(review)
    hil_ref = _hil_ref(hil_review)
    gate_ref = _autonomy_gate_ref(autonomy_gate)

    if episode.delivery_mission_contract_id != contract.contract_id:
        raise DeliveryRecoveryDecisionError("simulated episode contract_id mismatch")
    if episode.mission_id != contract.mission_id:
        raise DeliveryRecoveryDecisionError("simulated episode mission_id mismatch")
    if scorecard.delivery_mission_contract_ref != contract_ref:
        raise DeliveryRecoveryDecisionError("scorecard contract ref mismatch")
    if scorecard.simulated_delivery_episode_ref != episode_ref:
        raise DeliveryRecoveryDecisionError("scorecard episode ref mismatch")
    if scorecard.hil_telemetry_review_ref != hil_ref:
        raise DeliveryRecoveryDecisionError("scorecard HIL review ref mismatch")
    if scorecard.autonomy_gate_result_ref != gate_ref:
        raise DeliveryRecoveryDecisionError("scorecard autonomy gate ref mismatch")
    if scorecard.gate_passed != bool(autonomy_gate.passed):
        raise DeliveryRecoveryDecisionError("scorecard/autonomy gate status mismatch")
    if review.scorecard_ref != scorecard_ref:
        raise DeliveryRecoveryDecisionError("episode review/scorecard ref mismatch")
    if review.delivery_mission_contract_ref != contract_ref:
        raise DeliveryRecoveryDecisionError("episode review contract ref mismatch")
    if review.simulated_delivery_episode_ref != episode_ref:
        raise DeliveryRecoveryDecisionError("episode review episode ref mismatch")
    if review.hil_telemetry_review_ref != hil_ref:
        raise DeliveryRecoveryDecisionError("episode review HIL review ref mismatch")
    if review.autonomy_gate_result_ref != gate_ref:
        raise DeliveryRecoveryDecisionError("episode review autonomy gate ref mismatch")

    alternate_landing = _as_tuple(alternate_landing_refs)
    reroute = _as_tuple(reroute_refs)
    refs = _as_tuple(
        [
            contract_ref,
            episode_ref,
            scorecard_ref,
            review_ref,
            hil_ref,
            gate_ref,
            *episode.telemetry_refs,
            *episode.gate_refs,
            *(evidence_refs or ()),
        ]
    )
    recommendations: list[DeliveryRecoveryRecommendation] = []

    def add(
        action: DeliveryRecoveryAction,
        severity: DeliveryRecoverySeverity,
        reason: str,
        *,
        extra_refs: Sequence[str] | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        recommendations.append(
            DeliveryRecoveryRecommendation(
                action=action,
                severity=severity,
                reason=reason,
                evidence_refs=_as_tuple([*refs, *(extra_refs or ())]),
                metadata={
                    "recommendation_only": True,
                    "proposal_only": True,
                    "no_command_surface": True,
                    **dict(detail or {}),
                },
            )
        )

    blocked = set(review.blocked_buckets)
    warnings = set(review.warning_buckets)
    completed_no_recovery_needed = (
        scorecard.delivery_completed and scorecard.passed and review.passed
    )
    hold_proposed = bool(
        blocked
        & {
            DELIVERY_REVIEW_BUCKET_TELEMETRY_MISSING,
            DELIVERY_REVIEW_BUCKET_TELEMETRY_STALE,
            DELIVERY_REVIEW_BUCKET_AUTONOMY_GATE_FAILED,
        }
    )
    abort_proposed = bool(scorecard.abort_recommended or episode.abort_recommended)
    return_to_home_recommended = bool(scorecard.return_to_home_recommended)
    if (
        DELIVERY_REVIEW_BUCKET_BATTERY_RESERVE_VIOLATION in blocked
        and not abort_proposed
    ):
        return_to_home_recommended = True
    operator_escalation_required = bool(
        episode.operator_escalation_required
        or DELIVERY_REVIEW_BUCKET_DROPOFF_MISSING in blocked
        or DELIVERY_REVIEW_BUCKET_ROUTE_CONSTRAINT_VIOLATION in blocked
        or hold_proposed
    )
    alternate_landing_proposal = (
        DELIVERY_REVIEW_BUCKET_LANDING_ZONE_UNAVAILABLE in blocked
    )
    reroute_proposal = DELIVERY_REVIEW_BUCKET_ROUTE_CONSTRAINT_VIOLATION in blocked

    if completed_no_recovery_needed:
        add(
            DeliveryRecoveryAction.COMPLETED_NO_RECOVERY_NEEDED,
            DeliveryRecoverySeverity.INFO,
            "delivery_completed_no_recovery_needed",
            detail={"warning_buckets": sorted(warnings)},
        )
    else:
        if abort_proposed:
            add(
                DeliveryRecoveryAction.ABORT,
                DeliveryRecoverySeverity.BLOCKING,
                "delivery_review_requires_abort_proposal",
                detail={"blocked_buckets": sorted(blocked)},
            )
        if return_to_home_recommended:
            add(
                DeliveryRecoveryAction.RETURN_TO_HOME_RECOMMENDED,
                DeliveryRecoverySeverity.BLOCKING,
                "battery_reserve_violation_recommends_return_to_home",
                detail={"return_to_home_is_recommendation_only": True},
            )
        if hold_proposed:
            add(
                DeliveryRecoveryAction.HOLD,
                DeliveryRecoverySeverity.BLOCKING,
                "delivery_review_requires_hold_proposal",
                detail={"blocked_buckets": sorted(blocked)},
            )
        if alternate_landing_proposal:
            add(
                DeliveryRecoveryAction.ALTERNATE_LANDING_PROPOSAL,
                DeliveryRecoverySeverity.WARNING,
                "landing_zone_unavailable_requires_alternate_landing_proposal",
                extra_refs=alternate_landing,
                detail={"alternate_landing_refs": list(alternate_landing)},
            )
        if reroute_proposal:
            add(
                DeliveryRecoveryAction.REROUTE_PROPOSAL,
                DeliveryRecoverySeverity.WARNING,
                "route_constraint_violation_requires_reroute_review",
                extra_refs=reroute,
                detail={"reroute_refs": list(reroute)},
            )
        if operator_escalation_required:
            add(
                DeliveryRecoveryAction.OPERATOR_ESCALATION_REQUIRED,
                DeliveryRecoverySeverity.BLOCKING,
                "delivery_review_requires_operator_escalation",
                detail={"blocked_buckets": sorted(blocked)},
            )
    if not recommendations:
        add(
            DeliveryRecoveryAction.CONTINUE,
            DeliveryRecoverySeverity.INFO,
            "delivery_review_allows_continue_recommendation",
            detail={"warning_buckets": sorted(warnings)},
        )

    primary_action = recommendations[0].action
    payload = {
        "delivery_mission_contract_id": contract.contract_id,
        "simulated_delivery_episode_id": episode.episode_id,
        "delivery_scorecard_id": scorecard.scorecard_id,
        "delivery_episode_review_id": review.review_id,
        "recommendations": [
            {"action": item.action.value, "reason": item.reason}
            for item in recommendations
        ],
        "alternate_landing_refs": alternate_landing,
        "reroute_refs": reroute,
    }
    return DeliveryRecoveryDecision(
        decision_id=_stable_id("delivery_recovery_decision", payload),
        delivery_mission_contract_id=contract.contract_id,
        delivery_mission_id=contract.mission_id,
        simulated_delivery_episode_id=episode.episode_id,
        delivery_scorecard_id=scorecard.scorecard_id,
        delivery_episode_review_id=review.review_id,
        hil_telemetry_review_id=hil_review.review_id,
        autonomy_gate_result_id=autonomy_gate.gate_id,
        decision_source="delivery_episode_review",
        primary_action=primary_action,
        recommendations=tuple(recommendations),
        evidence_refs=refs,
        source_refs=_as_tuple([*refs, *(source_refs or ())]),
        completed_no_recovery_needed=completed_no_recovery_needed,
        continue_recommended=primary_action
        in {
            DeliveryRecoveryAction.CONTINUE,
            DeliveryRecoveryAction.COMPLETED_NO_RECOVERY_NEEDED,
        },
        hold_proposed=hold_proposed,
        hold_recommended=hold_proposed,
        return_to_home_recommended=return_to_home_recommended,
        abort_proposed=abort_proposed,
        abort_recommended=abort_proposed,
        operator_escalation_required=operator_escalation_required,
        alternate_landing_proposal=alternate_landing_proposal,
        reroute_proposal=reroute_proposal,
        alternate_landing_refs=alternate_landing,
        reroute_refs=reroute,
        created_at=created_at,
        metadata={
            **metadata_payload,
            "artifact_only": True,
            "recovery_decision_only": True,
            "recommendations_only": True,
            "decision_source": "delivery_episode_review",
            "return_to_home_is_recommendation_only": True,
            "abort_is_recommendation_only": True,
            "hold_is_recommendation_only": True,
            "alternate_landing_is_proposal_only": True,
            "reroute_is_proposal_only": True,
            "no_dispatch_surface": True,
            "no_entity_mutation": True,
        },
    )


def attach_delivery_recovery_decision(
    task_id: str,
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    simulated_delivery_episode: SimulatedDeliveryEpisode | Mapping[str, Any],
    delivery_progress_review: DeliveryProgressReview | Mapping[str, Any],
    alternate_dropoff_refs: Sequence[str] | None = None,
    evidence_refs: Sequence[str] | None = None,
    source_refs: Sequence[str] | None = None,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach a recovery decision without mutating task status."""

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise DeliveryRecoveryDecisionError(
            f"task {task_id} not found; cannot attach delivery recovery decision"
        )
    decision = build_delivery_recovery_decision(
        delivery_mission_contract=delivery_mission_contract,
        simulated_delivery_episode=simulated_delivery_episode,
        delivery_progress_review=delivery_progress_review,
        alternate_dropoff_refs=alternate_dropoff_refs,
        evidence_refs=evidence_refs,
        source_refs=source_refs,
        now=now,
    )
    artifacts = {"delivery_recovery_decision": decision.model_dump(mode="json")}
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise DeliveryRecoveryDecisionError(
            f"task {task_id} disappeared while attaching delivery recovery decision"
        )
    return artifacts


def attach_delivery_recovery_decision_from_episode_review(
    task_id: str,
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    simulated_delivery_episode: SimulatedDeliveryEpisode | Mapping[str, Any],
    delivery_scorecard: DeliveryScorecard | Mapping[str, Any],
    delivery_episode_review: DeliveryEpisodeReview | Mapping[str, Any],
    hil_telemetry_review: HilTelemetryReview | Mapping[str, Any],
    autonomy_gate_result: ToyGridWorldAutonomyGateResult | Mapping[str, Any],
    alternate_landing_refs: Sequence[str] | None = None,
    reroute_refs: Sequence[str] | None = None,
    evidence_refs: Sequence[str] | None = None,
    source_refs: Sequence[str] | None = None,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach episode-review recovery decision without mutating task status."""

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise DeliveryRecoveryDecisionError(
            f"task {task_id} not found; cannot attach delivery recovery decision"
        )
    decision = build_delivery_recovery_decision_from_episode_review(
        delivery_mission_contract=delivery_mission_contract,
        simulated_delivery_episode=simulated_delivery_episode,
        delivery_scorecard=delivery_scorecard,
        delivery_episode_review=delivery_episode_review,
        hil_telemetry_review=hil_telemetry_review,
        autonomy_gate_result=autonomy_gate_result,
        alternate_landing_refs=alternate_landing_refs,
        reroute_refs=reroute_refs,
        evidence_refs=evidence_refs,
        source_refs=source_refs,
        now=now,
        metadata=metadata,
    )
    artifacts = {"delivery_recovery_decision": decision.model_dump(mode="json")}
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise DeliveryRecoveryDecisionError(
            f"task {task_id} disappeared while attaching delivery recovery decision"
        )
    return artifacts


__all__ = [
    "DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION",
    "DeliveryRecoveryAction",
    "DeliveryRecoveryDecision",
    "DeliveryRecoveryDecisionError",
    "DeliveryRecoveryRecommendation",
    "DeliveryRecoverySeverity",
    "attach_delivery_recovery_decision",
    "attach_delivery_recovery_decision_from_episode_review",
    "build_delivery_recovery_decision",
    "build_delivery_recovery_decision_from_episode_review",
]
