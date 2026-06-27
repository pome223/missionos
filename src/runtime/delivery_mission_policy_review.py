"""Rule-based review for delivery mission policy conditions.

``delivery_mission_policy_review.v1`` evaluates a
``delivery_mission_contract.v1`` against read-only telemetry/review artifacts.
It only emits policy findings such as block, abort recommendation, return-home
recommendation, or operator escalation. It never emits commands, dispatch
payloads, MAVLink/ROS actions, Gazebo mutations, or actuator instructions.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.runtime.delivery_mission_contract import (
    DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION,
    DeliveryMissionContract,
)
from src.runtime.hil_telemetry_review import (
    HIL_REVIEW_BUCKET_STALE,
    HIL_TELEMETRY_REVIEW_SCHEMA_VERSION,
    HilTelemetryReview,
    HilTelemetryReviewStatus,
)
from src.runtime.px4_gazebo_telemetry import (
    PX4_GAZEBO_SANITIZED_TELEMETRY_SCHEMA_VERSION,
    Px4GazeboSanitizedTelemetry,
)
from src.runtime.task_store import TaskStore, get_task_store


DELIVERY_MISSION_POLICY_REVIEW_SCHEMA_VERSION = "delivery_mission_policy_review.v1"

DELIVERY_POLICY_BUCKET_REQUIRED_TELEMETRY_MISSING = "required_telemetry_missing"
DELIVERY_POLICY_BUCKET_HIL_REVIEW_BLOCKED = "hil_review_blocked"
DELIVERY_POLICY_BUCKET_HIL_TELEMETRY_STALE = "hil_telemetry_stale"
DELIVERY_POLICY_BUCKET_BATTERY_ABORT_RECOMMENDED = "battery_abort_recommended"
DELIVERY_POLICY_BUCKET_BATTERY_RETURN_HOME_RECOMMENDED = (
    "battery_return_home_recommended"
)
DELIVERY_POLICY_BUCKET_ROUTE_GEOFENCE_VIOLATION = "route_geofence_violation"
DELIVERY_POLICY_BUCKET_LANDING_ZONE_UNAVAILABLE = "landing_zone_unavailable"


class DeliveryMissionPolicyReviewStatus(str, Enum):
    PASSED = "passed"
    BLOCKED = "blocked"


class DeliveryMissionPolicySeverity(str, Enum):
    BLOCKING = "blocking"
    WARNING = "warning"
    INFO = "info"


class DeliveryMissionPolicyReviewError(RuntimeError):
    """Raised when delivery mission policy cannot be reviewed."""


class DeliveryMissionPolicyFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bucket: str
    reason: str
    severity: DeliveryMissionPolicySeverity
    detail: dict[str, Any] = Field(default_factory=dict)


class DeliveryMissionPolicyReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_MISSION_POLICY_REVIEW_SCHEMA_VERSION] = (
        DELIVERY_MISSION_POLICY_REVIEW_SCHEMA_VERSION
    )
    review_id: str
    delivery_mission_contract_id: str
    delivery_mission_id: str
    sanitized_telemetry_id: str | None = None
    hil_telemetry_review_id: str | None = None
    passed: bool
    status: DeliveryMissionPolicyReviewStatus
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    findings: tuple[DeliveryMissionPolicyFinding, ...] = ()
    missing_telemetry_measurements: tuple[str, ...] = ()
    battery_percent: float | None = None
    operator_escalation_required: bool = False
    abort_recommended: bool = False
    return_to_home_recommended: bool = False
    evaluated_at: datetime
    delivery_mission_contract_schema_version: Literal[
        DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    ] = DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    px4_gazebo_sanitized_telemetry_schema_version: Literal[
        PX4_GAZEBO_SANITIZED_TELEMETRY_SCHEMA_VERSION
    ] = PX4_GAZEBO_SANITIZED_TELEMETRY_SCHEMA_VERSION
    hil_telemetry_review_schema_version: Literal[HIL_TELEMETRY_REVIEW_SCHEMA_VERSION] = (
        HIL_TELEMETRY_REVIEW_SCHEMA_VERSION
    )
    rule_based: Literal[True] = True
    llm_judge_used: Literal[False] = False
    telemetry_only: Literal[True] = True
    read_only: Literal[True] = True
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)


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


def _to_contract(
    value: DeliveryMissionContract | Mapping[str, Any],
) -> DeliveryMissionContract:
    if isinstance(value, DeliveryMissionContract):
        return value
    return DeliveryMissionContract.model_validate(dict(value))


def _to_sanitized_telemetry(
    value: Px4GazeboSanitizedTelemetry | Mapping[str, Any] | None,
) -> Px4GazeboSanitizedTelemetry | None:
    if value is None:
        return None
    if isinstance(value, Px4GazeboSanitizedTelemetry):
        return value
    return Px4GazeboSanitizedTelemetry.model_validate(dict(value))


def _to_hil_review(
    value: HilTelemetryReview | Mapping[str, Any] | None,
) -> HilTelemetryReview | None:
    if value is None:
        return None
    if isinstance(value, HilTelemetryReview):
        return value
    return HilTelemetryReview.model_validate(dict(value))


def _measurement_value(
    telemetry: Px4GazeboSanitizedTelemetry | None,
    key: str,
) -> float | int | bool | str | None:
    if telemetry is None:
        return None
    return telemetry.measurements.get(key)


def _battery_percent(telemetry: Px4GazeboSanitizedTelemetry | None) -> float | None:
    value = _measurement_value(telemetry, "battery_percent")
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool_measurement(
    telemetry: Px4GazeboSanitizedTelemetry | None,
    key: str,
) -> bool:
    value = _measurement_value(telemetry, key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "unavailable", "violated"}
    return bool(value) if isinstance(value, int | float) else False


def _required_measurements_missing(
    contract: DeliveryMissionContract,
    telemetry: Px4GazeboSanitizedTelemetry | None,
) -> tuple[str, ...]:
    required = set(contract.telemetry_requirements.required_measurements)
    available = set(telemetry.measurement_keys if telemetry is not None else ())
    return tuple(sorted(required - available))


def build_delivery_mission_policy_review(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    sanitized_telemetry: Px4GazeboSanitizedTelemetry | Mapping[str, Any] | None = None,
    hil_telemetry_review: HilTelemetryReview | Mapping[str, Any] | None = None,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> DeliveryMissionPolicyReview:
    """Evaluate delivery policy conditions from read-only artifacts.

    The review is intentionally advisory / blocking only. Return-to-home and
    abort outputs are recommendations, not commands.
    """

    contract = _to_contract(delivery_mission_contract)
    telemetry = _to_sanitized_telemetry(sanitized_telemetry)
    hil_review = _to_hil_review(hil_telemetry_review)
    evaluated_at = _utc(now)

    findings: list[DeliveryMissionPolicyFinding] = []
    blocked: list[str] = []
    warnings: list[str] = []
    operator_escalation_required = False
    abort_recommended = False
    return_to_home_recommended = False

    missing = _required_measurements_missing(contract, telemetry)
    if missing:
        findings.append(
            DeliveryMissionPolicyFinding(
                bucket=DELIVERY_POLICY_BUCKET_REQUIRED_TELEMETRY_MISSING,
                reason="delivery_required_telemetry_measurements_missing",
                severity=DeliveryMissionPolicySeverity.BLOCKING,
                detail={"missing_measurements": list(missing)},
            )
        )
        blocked.append(DELIVERY_POLICY_BUCKET_REQUIRED_TELEMETRY_MISSING)
        operator_escalation_required = True

    if hil_review is None:
        findings.append(
            DeliveryMissionPolicyFinding(
                bucket=DELIVERY_POLICY_BUCKET_HIL_REVIEW_BLOCKED,
                reason="hil_telemetry_review_required_but_absent",
                severity=DeliveryMissionPolicySeverity.BLOCKING,
                detail={"required": True},
            )
        )
        blocked.append(DELIVERY_POLICY_BUCKET_HIL_REVIEW_BLOCKED)
        operator_escalation_required = True
    elif hil_review.status is HilTelemetryReviewStatus.BLOCKED:
        findings.append(
            DeliveryMissionPolicyFinding(
                bucket=DELIVERY_POLICY_BUCKET_HIL_REVIEW_BLOCKED,
                reason="hil_telemetry_review_blocked",
                severity=DeliveryMissionPolicySeverity.BLOCKING,
                detail={
                    "hil_telemetry_review_id": hil_review.review_id,
                    "blocked_reasons": list(hil_review.blocked_reasons),
                },
            )
        )
        blocked.append(DELIVERY_POLICY_BUCKET_HIL_REVIEW_BLOCKED)
        operator_escalation_required = True
        if HIL_REVIEW_BUCKET_STALE in hil_review.blocked_reasons:
            findings.append(
                DeliveryMissionPolicyFinding(
                    bucket=DELIVERY_POLICY_BUCKET_HIL_TELEMETRY_STALE,
                    reason="hil_telemetry_review_reported_stale_telemetry",
                    severity=DeliveryMissionPolicySeverity.BLOCKING,
                    detail={"hil_telemetry_review_id": hil_review.review_id},
                )
            )
            blocked.append(DELIVERY_POLICY_BUCKET_HIL_TELEMETRY_STALE)

    battery_percent = _battery_percent(telemetry)
    if battery_percent is None:
        if "battery_percent" in contract.telemetry_requirements.required_measurements:
            operator_escalation_required = True
    elif battery_percent <= contract.battery_policy.reserve_landing_percent:
        findings.append(
            DeliveryMissionPolicyFinding(
                bucket=DELIVERY_POLICY_BUCKET_BATTERY_ABORT_RECOMMENDED,
                reason="battery_percent_at_or_below_reserve_landing_threshold",
                severity=DeliveryMissionPolicySeverity.BLOCKING,
                detail={
                    "battery_percent": battery_percent,
                    "reserve_landing_percent": contract.battery_policy.reserve_landing_percent,
                },
            )
        )
        blocked.append(DELIVERY_POLICY_BUCKET_BATTERY_ABORT_RECOMMENDED)
        abort_recommended = True
        operator_escalation_required = True
    elif battery_percent <= contract.battery_policy.return_to_home_percent:
        findings.append(
            DeliveryMissionPolicyFinding(
                bucket=DELIVERY_POLICY_BUCKET_BATTERY_RETURN_HOME_RECOMMENDED,
                reason="battery_percent_at_or_below_return_to_home_threshold",
                severity=DeliveryMissionPolicySeverity.WARNING,
                detail={
                    "battery_percent": battery_percent,
                    "return_to_home_percent": contract.battery_policy.return_to_home_percent,
                },
            )
        )
        warnings.append(DELIVERY_POLICY_BUCKET_BATTERY_RETURN_HOME_RECOMMENDED)
        return_to_home_recommended = True

    route_geofence_violation = _bool_measurement(
        telemetry,
        "route_geofence_violation",
    ) or _bool_measurement(telemetry, "geofence_violation")
    if route_geofence_violation:
        findings.append(
            DeliveryMissionPolicyFinding(
                bucket=DELIVERY_POLICY_BUCKET_ROUTE_GEOFENCE_VIOLATION,
                reason="route_or_geofence_violation_observed_in_delivery_telemetry",
                severity=DeliveryMissionPolicySeverity.BLOCKING,
                detail={
                    "sanitized_telemetry_id": telemetry.telemetry_id
                    if telemetry
                    else None,
                },
            )
        )
        blocked.append(DELIVERY_POLICY_BUCKET_ROUTE_GEOFENCE_VIOLATION)
        operator_escalation_required = True

    landing_zone_available = _measurement_value(telemetry, "landing_zone_available")
    dropoff_zone_available = _measurement_value(telemetry, "dropoff_zone_available")
    landing_zone_unavailable = (
        landing_zone_available is False
        or dropoff_zone_available is False
        or _bool_measurement(telemetry, "landing_zone_unavailable")
        or _bool_measurement(telemetry, "dropoff_zone_unavailable")
    )
    if landing_zone_unavailable:
        findings.append(
            DeliveryMissionPolicyFinding(
                bucket=DELIVERY_POLICY_BUCKET_LANDING_ZONE_UNAVAILABLE,
                reason="landing_or_dropoff_zone_unavailable_in_delivery_telemetry",
                severity=DeliveryMissionPolicySeverity.BLOCKING,
                detail={
                    "sanitized_telemetry_id": telemetry.telemetry_id
                    if telemetry
                    else None,
                    "landing_zone_available": landing_zone_available,
                    "dropoff_zone_available": dropoff_zone_available,
                },
            )
        )
        blocked.append(DELIVERY_POLICY_BUCKET_LANDING_ZONE_UNAVAILABLE)
        operator_escalation_required = True

    blocked_reasons = tuple(sorted(set(blocked)))
    warning_reasons = tuple(sorted(set(warnings)))
    passed = not blocked_reasons
    review_id = _stable_id(
        "delivery_mission_policy_review",
        {
            "delivery_mission_contract_id": contract.contract_id,
            "sanitized_telemetry_id": telemetry.telemetry_id if telemetry else None,
            "hil_telemetry_review_id": hil_review.review_id if hil_review else None,
            "blocked_reasons": blocked_reasons,
            "warning_reasons": warning_reasons,
            "missing_telemetry_measurements": missing,
            "battery_percent": battery_percent,
            "route_geofence_violation": route_geofence_violation,
            "landing_zone_unavailable": landing_zone_unavailable,
        },
    )

    return DeliveryMissionPolicyReview(
        review_id=review_id,
        delivery_mission_contract_id=contract.contract_id,
        delivery_mission_id=contract.mission_id,
        sanitized_telemetry_id=telemetry.telemetry_id if telemetry else None,
        hil_telemetry_review_id=hil_review.review_id if hil_review else None,
        passed=passed,
        status=(
            DeliveryMissionPolicyReviewStatus.PASSED
            if passed
            else DeliveryMissionPolicyReviewStatus.BLOCKED
        ),
        blocked_reasons=blocked_reasons,
        warning_reasons=warning_reasons,
        findings=tuple(findings),
        missing_telemetry_measurements=missing,
        battery_percent=battery_percent,
        operator_escalation_required=operator_escalation_required,
        abort_recommended=abort_recommended,
        return_to_home_recommended=return_to_home_recommended,
        evaluated_at=evaluated_at,
        metadata={
            **(metadata or {}),
            "artifact_only": True,
            "policy_review_only": True,
            "return_to_home_is_recommendation_only": True,
            "abort_is_recommendation_only": True,
            "no_dispatch_surface": True,
        },
    )


def attach_delivery_mission_policy_review(
    task_id: str,
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    sanitized_telemetry: Px4GazeboSanitizedTelemetry | Mapping[str, Any] | None = None,
    hil_telemetry_review: HilTelemetryReview | Mapping[str, Any] | None = None,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach the policy review to a task without mutating task status.

    The review is a read-only artifact. It does not create approvals,
    promotion artifacts, runtime reuse artifacts, or command surfaces.
    """

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise DeliveryMissionPolicyReviewError(
            f"task {task_id} not found; cannot attach delivery mission policy review"
        )
    review = build_delivery_mission_policy_review(
        delivery_mission_contract=delivery_mission_contract,
        sanitized_telemetry=sanitized_telemetry,
        hil_telemetry_review=hil_telemetry_review,
        now=now,
    )
    artifacts = {"delivery_mission_policy_review": review.model_dump(mode="json")}
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise DeliveryMissionPolicyReviewError(
            f"task {task_id} disappeared while attaching delivery mission policy review"
        )
    return artifacts


__all__ = [
    "DELIVERY_MISSION_POLICY_REVIEW_SCHEMA_VERSION",
    "DELIVERY_POLICY_BUCKET_BATTERY_ABORT_RECOMMENDED",
    "DELIVERY_POLICY_BUCKET_BATTERY_RETURN_HOME_RECOMMENDED",
    "DELIVERY_POLICY_BUCKET_HIL_REVIEW_BLOCKED",
    "DELIVERY_POLICY_BUCKET_HIL_TELEMETRY_STALE",
    "DELIVERY_POLICY_BUCKET_LANDING_ZONE_UNAVAILABLE",
    "DELIVERY_POLICY_BUCKET_REQUIRED_TELEMETRY_MISSING",
    "DELIVERY_POLICY_BUCKET_ROUTE_GEOFENCE_VIOLATION",
    "DeliveryMissionPolicyFinding",
    "DeliveryMissionPolicyReview",
    "DeliveryMissionPolicyReviewError",
    "DeliveryMissionPolicyReviewStatus",
    "DeliveryMissionPolicySeverity",
    "attach_delivery_mission_policy_review",
    "build_delivery_mission_policy_review",
]
