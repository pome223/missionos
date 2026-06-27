"""Rule-based scorecard and review for simulated delivery episodes.

This layer evaluates an already-created ``simulated_delivery_episode.v1`` and
its replay/HIL/gate evidence. It does not start Gazebo, dispatch MAVLink/ROS,
upload missions, mutate simulators, approve promotions, or create reuse
artifacts.
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

from src.runtime.delivery_mission_contract import (
    DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION,
    DeliveryMissionContract,
)
from src.runtime.hil_telemetry_review import (
    HIL_REVIEW_BUCKET_MISSING,
    HIL_REVIEW_BUCKET_STALE,
    HIL_TELEMETRY_REVIEW_SCHEMA_VERSION,
    HilTelemetryReview,
)
from src.runtime.px4_gazebo_telemetry import (
    Px4GazeboSanitizedTelemetry,
)
from src.runtime.simulated_delivery_episode import (
    DELIVERY_REPLAY_TRACE_SCHEMA_VERSION,
    SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION,
    DeliveryReplayTrace,
    SimulatedDeliveryEpisode,
    SimulatedDeliveryEpisodePhase,
)
from src.runtime.task_store import TaskStore, get_task_store
from src.runtime.toy_grid_world import (
    TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION,
    ToyGridWorldAutonomyGateResult,
)

DELIVERY_SCORECARD_SCHEMA_VERSION = "delivery_scorecard.v1"
DELIVERY_EPISODE_REVIEW_SCHEMA_VERSION = "delivery_episode_review.v1"

DELIVERY_REVIEW_BUCKET_DELIVERY_COMPLETED = "delivery_completed"
DELIVERY_REVIEW_BUCKET_DROPOFF_MISSING = "dropoff_missing"
DELIVERY_REVIEW_BUCKET_STAGED_ASCENT_REQUIRED = "staged_ascent_required"
DELIVERY_REVIEW_BUCKET_STAGED_ASCENT_INCOMPLETE = "staged_ascent_incomplete"
DELIVERY_REVIEW_BUCKET_BATTERY_LOW = "battery_low"
DELIVERY_REVIEW_BUCKET_BATTERY_RESERVE_VIOLATION = "battery_reserve_violation"
DELIVERY_REVIEW_BUCKET_TELEMETRY_MISSING = "telemetry_missing"
DELIVERY_REVIEW_BUCKET_TELEMETRY_STALE = "telemetry_stale"
DELIVERY_REVIEW_BUCKET_AUTONOMY_GATE_FAILED = "autonomy_gate_failed"
DELIVERY_REVIEW_BUCKET_LANDING_ZONE_UNAVAILABLE = "landing_zone_unavailable"
DELIVERY_REVIEW_BUCKET_ROUTE_CONSTRAINT_VIOLATION = "route_constraint_violation"
DELIVERY_REVIEW_BUCKET_HIGH_ALTITUDE_RISK = "high_altitude_risk"
DELIVERY_REVIEW_BUCKET_PAYLOAD_MARGIN_RISK = "payload_margin_risk"
DELIVERY_REVIEW_BUCKET_VEHICLE_HEALTH_UNSAFE = "vehicle_health_unsafe"
DELIVERY_REVIEW_BUCKET_OPERATOR_ESCALATION_REQUIRED = "operator_escalation_required"
DELIVERY_REVIEW_BUCKET_REPLAY_NOT_DETERMINISTIC = "replay_not_deterministic"


class DeliveryEpisodeReviewError(RuntimeError):
    """Raised when delivery episode review cannot be built safely."""


class DeliveryEpisodeReviewSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


class DeliveryEpisodeReviewStatus(str, Enum):
    PASSED = "passed"
    BLOCKED = "blocked"


_FORBIDDEN_COMMAND_KEYS = frozenset(
    {
        "action",
        "actuator",
        "actuator_execution_allowed",
        "command",
        "command_payload_allowed",
        "dispatch",
        "execute",
        "gazebo_mutation",
        "live_execution_allowed",
        "mavlink_command",
        "mission_upload",
        "physical_execution_invoked",
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
        raise DeliveryEpisodeReviewError(
            "delivery episode review refused command-like keys: "
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


class DeliveryEpisodeReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bucket: str
    reason: str
    severity: DeliveryEpisodeReviewSeverity
    detail: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _reject_command_like_detail(self) -> "DeliveryEpisodeReviewFinding":
        _raise_for_command_like_keys(self.detail, root="finding.detail")
        return self


class DeliveryScorecard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_SCORECARD_SCHEMA_VERSION] = (
        DELIVERY_SCORECARD_SCHEMA_VERSION
    )
    scorecard_id: str
    delivery_mission_contract_ref: str
    simulated_delivery_episode_ref: str
    delivery_replay_trace_ref: str
    hil_telemetry_review_ref: str
    autonomy_gate_result_ref: str
    pickup_completed: bool
    dropoff_completed: bool
    delivery_completed: bool
    staged_ascent_completed: bool
    battery_reserve_ok: bool
    telemetry_freshness_ok: bool
    route_constraints_ok: bool
    landing_zone_policy_ok: bool
    gate_passed: bool
    operator_escalation_count: int = Field(ge=0)
    recovery_decision_count: int = Field(ge=0)
    abort_recommended: bool
    return_to_home_recommended: bool
    blocked_reason_count: int = Field(ge=0)
    warning_reason_count: int = Field(ge=0)
    passed: bool
    blocked_buckets: tuple[str, ...] = ()
    warning_buckets: tuple[str, ...] = ()
    evaluated_at: datetime
    delivery_mission_contract_schema_version: Literal[
        DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    ] = DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    simulated_delivery_episode_schema_version: Literal[
        SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION
    ] = SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION
    delivery_replay_trace_schema_version: Literal[
        DELIVERY_REPLAY_TRACE_SCHEMA_VERSION
    ] = DELIVERY_REPLAY_TRACE_SCHEMA_VERSION
    hil_telemetry_review_schema_version: Literal[
        HIL_TELEMETRY_REVIEW_SCHEMA_VERSION
    ] = HIL_TELEMETRY_REVIEW_SCHEMA_VERSION
    autonomy_gate_result_schema_version: Literal[
        TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION
    ] = TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION
    rule_based: Literal[True] = True
    llm_judge_used: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    approval_promotion_reuse_created: Literal[False] = False


class DeliveryEpisodeReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_EPISODE_REVIEW_SCHEMA_VERSION] = (
        DELIVERY_EPISODE_REVIEW_SCHEMA_VERSION
    )
    review_id: str
    scorecard_ref: str
    delivery_mission_contract_ref: str
    simulated_delivery_episode_ref: str
    delivery_replay_trace_ref: str
    hil_telemetry_review_ref: str
    autonomy_gate_result_ref: str
    status: DeliveryEpisodeReviewStatus
    passed: bool
    buckets: tuple[str, ...] = ()
    blocked_buckets: tuple[str, ...] = ()
    warning_buckets: tuple[str, ...] = ()
    findings: tuple[DeliveryEpisodeReviewFinding, ...] = ()
    evaluated_at: datetime
    rule_based: Literal[True] = True
    llm_judge_used: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    approval_promotion_reuse_created: Literal[False] = False


def _contract_ref(contract: DeliveryMissionContract) -> str:
    return f"delivery_mission_contract:{contract.contract_id}"


def _episode_ref(episode: SimulatedDeliveryEpisode) -> str:
    return f"simulated_delivery_episode:{episode.episode_id}"


def _trace_ref(trace: DeliveryReplayTrace) -> str:
    return f"delivery_replay_trace:{trace.trace_id}"


def _hil_ref(review: HilTelemetryReview) -> str:
    return f"hil_telemetry_review:{review.review_id}"


def _gate_ref(gate: ToyGridWorldAutonomyGateResult) -> str:
    return f"autonomy_gate_result:{gate.gate_id}"


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


def _to_trace(value: DeliveryReplayTrace | Mapping[str, Any]) -> DeliveryReplayTrace:
    if isinstance(value, DeliveryReplayTrace):
        return value
    return DeliveryReplayTrace.model_validate(dict(value))


def _to_hil(value: HilTelemetryReview | Mapping[str, Any]) -> HilTelemetryReview:
    if isinstance(value, HilTelemetryReview):
        return value
    return HilTelemetryReview.model_validate(dict(value))


def _to_gate(
    value: ToyGridWorldAutonomyGateResult | Mapping[str, Any],
) -> ToyGridWorldAutonomyGateResult:
    if isinstance(value, ToyGridWorldAutonomyGateResult):
        return value
    return ToyGridWorldAutonomyGateResult.model_validate(dict(value))


def _to_telemetry(
    value: Px4GazeboSanitizedTelemetry | Mapping[str, Any] | None,
) -> Px4GazeboSanitizedTelemetry | None:
    if value is None:
        return None
    if isinstance(value, Px4GazeboSanitizedTelemetry):
        return value
    return Px4GazeboSanitizedTelemetry.model_validate(dict(value))


def _phase_present(
    episode: SimulatedDeliveryEpisode, phase: SimulatedDeliveryEpisodePhase
) -> bool:
    return any(step.phase is phase for step in episode.steps)


def _replay_is_deterministic(trace: DeliveryReplayTrace) -> bool:
    if not trace.events:
        return False
    times = [event.t_relative_seconds for event in trace.events]
    return times == sorted(times) and all(event.artifact_ref for event in trace.events)


def _battery_reserve_ok(
    contract: DeliveryMissionContract,
    telemetry: Px4GazeboSanitizedTelemetry | None,
) -> bool:
    if telemetry is None:
        return False
    value = telemetry.measurements.get("battery_percent")
    if value is None:
        return True
    return float(value) >= float(contract.battery_policy.reserve_landing_percent)


def _landing_zone_ok(telemetry: Px4GazeboSanitizedTelemetry | None) -> bool:
    if telemetry is None:
        return False
    return telemetry.measurements.get("landing_zone_available", True) is not False


def _route_constraints_ok(episode: SimulatedDeliveryEpisode) -> bool:
    reasons = set(episode.blocked_reasons)
    return not bool(
        reasons
        & {
            DELIVERY_REVIEW_BUCKET_ROUTE_CONSTRAINT_VIOLATION,
            "route_geofence_violation",
            "route_constraint_violation",
        }
    )


def _scenario_requires_staged_ascent(episode: SimulatedDeliveryEpisode) -> bool:
    return (
        episode.metadata.get("scenario_profile") == "mountain_summit_payload_delivery"
        or episode.metadata.get("route_profile") == "staged_ascent_required"
    )


def _finding(
    bucket: str,
    *,
    severity: DeliveryEpisodeReviewSeverity,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> DeliveryEpisodeReviewFinding:
    return DeliveryEpisodeReviewFinding(
        bucket=bucket,
        reason=reason,
        severity=severity,
        detail=detail or {},
    )


def build_delivery_episode_scorecard_review(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    simulated_delivery_episode: SimulatedDeliveryEpisode | Mapping[str, Any],
    delivery_replay_trace: DeliveryReplayTrace | Mapping[str, Any],
    hil_telemetry_review: HilTelemetryReview | Mapping[str, Any],
    autonomy_gate_result: ToyGridWorldAutonomyGateResult | Mapping[str, Any],
    sanitized_telemetry: Px4GazeboSanitizedTelemetry | Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, DeliveryScorecard | DeliveryEpisodeReview]:
    """Build deterministic scorecard/review artifacts for one delivery episode."""

    contract = _to_contract(delivery_mission_contract)
    episode = _to_episode(simulated_delivery_episode)
    trace = _to_trace(delivery_replay_trace)
    hil = _to_hil(hil_telemetry_review)
    gate = _to_gate(autonomy_gate_result)
    telemetry = _to_telemetry(sanitized_telemetry)
    evaluated_at = _utc(now)

    contract_ref = _contract_ref(contract)
    episode_ref = _episode_ref(episode)
    trace_ref = _trace_ref(trace)
    hil_ref = _hil_ref(hil)
    gate_ref = _gate_ref(gate)

    if episode.delivery_mission_contract_id != contract.contract_id:
        raise DeliveryEpisodeReviewError("episode/contract ref mismatch")
    if episode.delivery_replay_trace_ref != trace_ref:
        raise DeliveryEpisodeReviewError("episode/replay trace ref mismatch")
    if trace.episode_ref != episode_ref:
        raise DeliveryEpisodeReviewError("replay trace/episode ref mismatch")
    if episode.hil_telemetry_review_ref and episode.hil_telemetry_review_ref != hil_ref:
        raise DeliveryEpisodeReviewError("episode/HIL review ref mismatch")
    if (
        episode.autonomy_gate_result_ref
        and episode.autonomy_gate_result_ref != gate_ref
    ):
        raise DeliveryEpisodeReviewError("episode/autonomy gate ref mismatch")

    pickup_completed = _phase_present(episode, SimulatedDeliveryEpisodePhase.PREFLIGHT)
    dropoff_completed = bool(episode.dropoff_verified) and _phase_present(
        episode,
        SimulatedDeliveryEpisodePhase.DROPOFF_VERIFIED,
    )
    staged_ascent_required = _scenario_requires_staged_ascent(episode)
    staged_ascent_completed = _phase_present(
        episode,
        SimulatedDeliveryEpisodePhase.STAGED_ASCENT,
    )
    replay_deterministic = _replay_is_deterministic(trace)
    battery_ok = _battery_reserve_ok(contract, telemetry)
    telemetry_freshness_ok = (
        hil.passed and HIL_REVIEW_BUCKET_STALE not in hil.blocked_reasons
    )
    route_ok = _route_constraints_ok(episode)
    landing_ok = _landing_zone_ok(telemetry)
    gate_passed = bool(gate.passed)

    findings: list[DeliveryEpisodeReviewFinding] = []
    if dropoff_completed and episode.passed and gate_passed:
        findings.append(
            _finding(
                DELIVERY_REVIEW_BUCKET_DELIVERY_COMPLETED,
                severity=DeliveryEpisodeReviewSeverity.INFO,
                reason="episode_completed_with_dropoff_and_gate",
            )
        )
    if not dropoff_completed:
        findings.append(
            _finding(
                DELIVERY_REVIEW_BUCKET_DROPOFF_MISSING,
                severity=DeliveryEpisodeReviewSeverity.BLOCKING,
                reason="dropoff evidence missing or not verified",
            )
        )
    if staged_ascent_required:
        findings.append(
            _finding(
                DELIVERY_REVIEW_BUCKET_STAGED_ASCENT_REQUIRED,
                severity=DeliveryEpisodeReviewSeverity.WARNING,
                reason="mountain/high-payload profile requires staged ascent",
            )
        )
        if not staged_ascent_completed:
            findings.append(
                _finding(
                    DELIVERY_REVIEW_BUCKET_STAGED_ASCENT_INCOMPLETE,
                    severity=DeliveryEpisodeReviewSeverity.BLOCKING,
                    reason="staged ascent was required but not completed",
                )
            )
    if telemetry is None:
        findings.append(
            _finding(
                DELIVERY_REVIEW_BUCKET_TELEMETRY_MISSING,
                severity=DeliveryEpisodeReviewSeverity.BLOCKING,
                reason="sanitized telemetry was not provided",
            )
        )
    if HIL_REVIEW_BUCKET_MISSING in hil.blocked_reasons:
        findings.append(
            _finding(
                DELIVERY_REVIEW_BUCKET_TELEMETRY_MISSING,
                severity=DeliveryEpisodeReviewSeverity.BLOCKING,
                reason="HIL review reported missing telemetry",
            )
        )
    if HIL_REVIEW_BUCKET_STALE in hil.blocked_reasons:
        findings.append(
            _finding(
                DELIVERY_REVIEW_BUCKET_TELEMETRY_STALE,
                severity=DeliveryEpisodeReviewSeverity.BLOCKING,
                reason="HIL review reported stale telemetry",
            )
        )
    if not gate_passed:
        findings.append(
            _finding(
                DELIVERY_REVIEW_BUCKET_AUTONOMY_GATE_FAILED,
                severity=DeliveryEpisodeReviewSeverity.BLOCKING,
                reason="autonomy gate failed",
                detail={"blocked_reasons": list(gate.blocked_reasons)},
            )
        )
    if not battery_ok:
        findings.append(
            _finding(
                DELIVERY_REVIEW_BUCKET_BATTERY_RESERVE_VIOLATION,
                severity=DeliveryEpisodeReviewSeverity.BLOCKING,
                reason="battery reserve was below contract policy",
            )
        )
    elif telemetry is not None and float(
        telemetry.measurements.get("battery_percent", 100)
    ) < float(contract.battery_policy.return_to_home_percent):
        findings.append(
            _finding(
                DELIVERY_REVIEW_BUCKET_BATTERY_LOW,
                severity=DeliveryEpisodeReviewSeverity.WARNING,
                reason="battery below return-to-home policy",
            )
        )
    if not landing_ok:
        findings.append(
            _finding(
                DELIVERY_REVIEW_BUCKET_LANDING_ZONE_UNAVAILABLE,
                severity=DeliveryEpisodeReviewSeverity.BLOCKING,
                reason="landing zone was unavailable",
            )
        )
    if not route_ok:
        findings.append(
            _finding(
                DELIVERY_REVIEW_BUCKET_ROUTE_CONSTRAINT_VIOLATION,
                severity=DeliveryEpisodeReviewSeverity.BLOCKING,
                reason="episode reported route constraint violation",
            )
        )
    if staged_ascent_required:
        findings.append(
            _finding(
                DELIVERY_REVIEW_BUCKET_HIGH_ALTITUDE_RISK,
                severity=DeliveryEpisodeReviewSeverity.WARNING,
                reason="mountain summit profile carries high altitude risk",
            )
        )
        findings.append(
            _finding(
                DELIVERY_REVIEW_BUCKET_PAYLOAD_MARGIN_RISK,
                severity=DeliveryEpisodeReviewSeverity.WARNING,
                reason="mountain summit profile carries payload margin risk",
            )
        )
    if telemetry is not None and str(
        telemetry.measurements.get("vehicle_health", "nominal")
    ).lower() not in {"nominal", "ok", "healthy"}:
        findings.append(
            _finding(
                DELIVERY_REVIEW_BUCKET_VEHICLE_HEALTH_UNSAFE,
                severity=DeliveryEpisodeReviewSeverity.BLOCKING,
                reason="vehicle health was not nominal",
            )
        )
    if episode.operator_escalation_required:
        findings.append(
            _finding(
                DELIVERY_REVIEW_BUCKET_OPERATOR_ESCALATION_REQUIRED,
                severity=DeliveryEpisodeReviewSeverity.BLOCKING,
                reason="episode requires operator escalation",
            )
        )
    if not replay_deterministic:
        findings.append(
            _finding(
                DELIVERY_REVIEW_BUCKET_REPLAY_NOT_DETERMINISTIC,
                severity=DeliveryEpisodeReviewSeverity.BLOCKING,
                reason="replay trace is missing deterministic event ordering",
            )
        )

    blocked_buckets = _as_tuple(
        [
            finding.bucket
            for finding in findings
            if finding.severity is DeliveryEpisodeReviewSeverity.BLOCKING
        ]
    )
    warning_buckets = _as_tuple(
        [
            finding.bucket
            for finding in findings
            if finding.severity is DeliveryEpisodeReviewSeverity.WARNING
        ]
    )
    buckets = _as_tuple([finding.bucket for finding in findings])
    delivery_completed = (
        episode.final_status.value == "completed"
        and dropoff_completed
        and gate_passed
        and not blocked_buckets
    )
    passed = delivery_completed
    scorecard_payload = {
        "contract_ref": contract_ref,
        "episode_ref": episode_ref,
        "trace_ref": trace_ref,
        "hil_ref": hil_ref,
        "gate_ref": gate_ref,
        "delivery_completed": delivery_completed,
        "blocked_buckets": blocked_buckets,
        "warning_buckets": warning_buckets,
    }
    scorecard = DeliveryScorecard(
        scorecard_id=_stable_id("delivery_scorecard", scorecard_payload),
        delivery_mission_contract_ref=contract_ref,
        simulated_delivery_episode_ref=episode_ref,
        delivery_replay_trace_ref=trace_ref,
        hil_telemetry_review_ref=hil_ref,
        autonomy_gate_result_ref=gate_ref,
        pickup_completed=pickup_completed,
        dropoff_completed=dropoff_completed,
        delivery_completed=delivery_completed,
        staged_ascent_completed=staged_ascent_completed,
        battery_reserve_ok=battery_ok,
        telemetry_freshness_ok=telemetry_freshness_ok,
        route_constraints_ok=route_ok,
        landing_zone_policy_ok=landing_ok,
        gate_passed=gate_passed,
        operator_escalation_count=1 if episode.operator_escalation_required else 0,
        recovery_decision_count=(
            1 if episode.abort_recommended or episode.return_to_home_recommended else 0
        ),
        abort_recommended=episode.abort_recommended,
        return_to_home_recommended=episode.return_to_home_recommended,
        blocked_reason_count=len(episode.blocked_reasons) + len(blocked_buckets),
        warning_reason_count=len(episode.warning_reasons) + len(warning_buckets),
        passed=passed,
        blocked_buckets=blocked_buckets,
        warning_buckets=warning_buckets,
        evaluated_at=evaluated_at,
    )
    review_payload = {
        "scorecard_ref": f"delivery_scorecard:{scorecard.scorecard_id}",
        "buckets": buckets,
        "blocked_buckets": blocked_buckets,
        "warning_buckets": warning_buckets,
    }
    review = DeliveryEpisodeReview(
        review_id=_stable_id("delivery_episode_review", review_payload),
        scorecard_ref=f"delivery_scorecard:{scorecard.scorecard_id}",
        delivery_mission_contract_ref=contract_ref,
        simulated_delivery_episode_ref=episode_ref,
        delivery_replay_trace_ref=trace_ref,
        hil_telemetry_review_ref=hil_ref,
        autonomy_gate_result_ref=gate_ref,
        status=(
            DeliveryEpisodeReviewStatus.PASSED
            if passed
            else DeliveryEpisodeReviewStatus.BLOCKED
        ),
        passed=passed,
        buckets=buckets,
        blocked_buckets=blocked_buckets,
        warning_buckets=warning_buckets,
        findings=tuple(findings),
        evaluated_at=evaluated_at,
    )
    return {"delivery_scorecard": scorecard, "delivery_episode_review": review}


def attach_delivery_episode_scorecard_review(
    task_id: str,
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    simulated_delivery_episode: SimulatedDeliveryEpisode | Mapping[str, Any],
    delivery_replay_trace: DeliveryReplayTrace | Mapping[str, Any],
    hil_telemetry_review: HilTelemetryReview | Mapping[str, Any],
    autonomy_gate_result: ToyGridWorldAutonomyGateResult | Mapping[str, Any],
    sanitized_telemetry: Px4GazeboSanitizedTelemetry | Mapping[str, Any] | None = None,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach delivery scorecard/review artifacts without mutating task status."""

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise DeliveryEpisodeReviewError(
            f"task {task_id} not found; cannot attach delivery episode review"
        )
    built = build_delivery_episode_scorecard_review(
        delivery_mission_contract=delivery_mission_contract,
        simulated_delivery_episode=simulated_delivery_episode,
        delivery_replay_trace=delivery_replay_trace,
        hil_telemetry_review=hil_telemetry_review,
        autonomy_gate_result=autonomy_gate_result,
        sanitized_telemetry=sanitized_telemetry,
        now=now,
    )
    artifacts = {
        "delivery_scorecard": built["delivery_scorecard"].model_dump(mode="json"),
        "delivery_episode_review": built["delivery_episode_review"].model_dump(
            mode="json"
        ),
    }
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise DeliveryEpisodeReviewError(
            f"task {task_id} disappeared while attaching delivery episode review"
        )
    return artifacts


__all__ = [
    "DELIVERY_EPISODE_REVIEW_SCHEMA_VERSION",
    "DELIVERY_SCORECARD_SCHEMA_VERSION",
    "DELIVERY_REVIEW_BUCKET_AUTONOMY_GATE_FAILED",
    "DELIVERY_REVIEW_BUCKET_BATTERY_LOW",
    "DELIVERY_REVIEW_BUCKET_BATTERY_RESERVE_VIOLATION",
    "DELIVERY_REVIEW_BUCKET_DELIVERY_COMPLETED",
    "DELIVERY_REVIEW_BUCKET_DROPOFF_MISSING",
    "DELIVERY_REVIEW_BUCKET_HIGH_ALTITUDE_RISK",
    "DELIVERY_REVIEW_BUCKET_LANDING_ZONE_UNAVAILABLE",
    "DELIVERY_REVIEW_BUCKET_OPERATOR_ESCALATION_REQUIRED",
    "DELIVERY_REVIEW_BUCKET_PAYLOAD_MARGIN_RISK",
    "DELIVERY_REVIEW_BUCKET_REPLAY_NOT_DETERMINISTIC",
    "DELIVERY_REVIEW_BUCKET_ROUTE_CONSTRAINT_VIOLATION",
    "DELIVERY_REVIEW_BUCKET_STAGED_ASCENT_INCOMPLETE",
    "DELIVERY_REVIEW_BUCKET_STAGED_ASCENT_REQUIRED",
    "DELIVERY_REVIEW_BUCKET_TELEMETRY_MISSING",
    "DELIVERY_REVIEW_BUCKET_TELEMETRY_STALE",
    "DELIVERY_REVIEW_BUCKET_VEHICLE_HEALTH_UNSAFE",
    "DeliveryEpisodeReview",
    "DeliveryEpisodeReviewError",
    "DeliveryEpisodeReviewFinding",
    "DeliveryEpisodeReviewSeverity",
    "DeliveryEpisodeReviewStatus",
    "DeliveryScorecard",
    "attach_delivery_episode_scorecard_review",
    "build_delivery_episode_scorecard_review",
]
