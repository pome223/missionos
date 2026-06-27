"""Rule-based progress review for simulated delivery episodes.

``delivery_progress_review.v1`` evaluates whether a simulated delivery episode
is merely preflight-ready, in progress, completed, or blocked. It reads existing
Mission OS artifacts only: delivery mission contract, Gazebo scenario,
simulated delivery episode, sanitized telemetry, and optional HIL review.

The review is intentionally non-executing. It never advances Gazebo, uploads a
PX4 mission, sends MAVLink/ROS payloads, issues return-to-home or landing
commands, mutates simulator entities, or executes actuators.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.runtime.delivery_mission_contract import (
    DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION,
    DeliveryMissionContract,
)
from src.runtime.gazebo_delivery_scenario import (
    GAZEBO_DELIVERY_SCENARIO_SCHEMA_VERSION,
    GazeboDeliveryScenario,
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
from src.runtime.simulated_delivery_episode import (
    SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION,
    SimulatedDeliveryEpisode,
    SimulatedDeliveryEpisodePhase,
)
from src.runtime.task_store import TaskStore, get_task_store


DELIVERY_PROGRESS_REVIEW_SCHEMA_VERSION = "delivery_progress_review.v1"

DELIVERY_PROGRESS_BUCKET_TELEMETRY_MISSING = "progress_telemetry_missing"
DELIVERY_PROGRESS_BUCKET_HIL_REVIEW_BLOCKED = "hil_review_blocked"
DELIVERY_PROGRESS_BUCKET_HIL_TELEMETRY_STALE = "hil_telemetry_stale"
DELIVERY_PROGRESS_BUCKET_EPISODE_BLOCKED = "simulated_delivery_episode_blocked"
DELIVERY_PROGRESS_BUCKET_PICKUP_REACHED = "pickup_reached"
DELIVERY_PROGRESS_BUCKET_DROPOFF_REACHED = "dropoff_reached"
DELIVERY_PROGRESS_BUCKET_ROUTE_PROGRESS_OBSERVED = "route_progress_observed"
DELIVERY_PROGRESS_BUCKET_ROUTE_GEOFENCE_VIOLATION = "route_geofence_violation"
DELIVERY_PROGRESS_BUCKET_LANDING_ZONE_UNAVAILABLE = "landing_zone_unavailable"
DELIVERY_PROGRESS_BUCKET_COMPLETION_CRITERIA_MET = "completion_criteria_met"


class DeliveryProgressStatus(str, Enum):
    PREFLIGHT_READY = "preflight_ready"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class DeliveryProgressSeverity(str, Enum):
    BLOCKING = "blocking"
    WARNING = "warning"
    INFO = "info"


class DeliveryProgressReviewError(RuntimeError):
    """Raised when delivery progress cannot be reviewed safely."""


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
        raise DeliveryProgressReviewError(
            "delivery progress review refused command-like keys: "
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


class DeliveryProgressFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bucket: str
    reason: str
    severity: DeliveryProgressSeverity
    detail: dict[str, Any] = Field(default_factory=dict)


class DeliveryProgressReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_PROGRESS_REVIEW_SCHEMA_VERSION] = (
        DELIVERY_PROGRESS_REVIEW_SCHEMA_VERSION
    )
    progress_review_id: str
    delivery_mission_contract_id: str
    delivery_mission_id: str
    gazebo_delivery_scenario_id: str
    simulated_delivery_episode_id: str
    sanitized_telemetry_id: str | None = None
    hil_telemetry_review_id: str | None = None
    status: DeliveryProgressStatus
    passed: bool
    pickup_reached: bool = False
    dropoff_reached: bool = False
    route_progress_percent: float = 0.0
    completion_criteria_met: bool = False
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    findings: tuple[DeliveryProgressFinding, ...] = ()
    telemetry_refs: tuple[str, ...] = ()
    episode_refs: tuple[str, ...] = ()
    scenario_refs: tuple[str, ...] = ()
    gate_refs: tuple[str, ...] = ()
    evaluated_at: datetime
    delivery_mission_contract_schema_version: Literal[
        DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    ] = DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    gazebo_delivery_scenario_schema_version: Literal[
        GAZEBO_DELIVERY_SCENARIO_SCHEMA_VERSION
    ] = GAZEBO_DELIVERY_SCENARIO_SCHEMA_VERSION
    simulated_delivery_episode_schema_version: Literal[
        SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION
    ] = SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION
    px4_gazebo_sanitized_telemetry_schema_version: Literal[
        PX4_GAZEBO_SANITIZED_TELEMETRY_SCHEMA_VERSION
    ] = PX4_GAZEBO_SANITIZED_TELEMETRY_SCHEMA_VERSION
    hil_telemetry_review_schema_version: Literal[HIL_TELEMETRY_REVIEW_SCHEMA_VERSION] = (
        HIL_TELEMETRY_REVIEW_SCHEMA_VERSION
    )
    rule_based: Literal[True] = True
    llm_judge_used: Literal[False] = False
    simulation_only: Literal[True] = True
    telemetry_only: Literal[True] = True
    read_only: Literal[True] = True
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
    metadata: dict[str, Any] = Field(default_factory=dict)


def _to_contract(value: DeliveryMissionContract | Mapping[str, Any]) -> DeliveryMissionContract:
    if isinstance(value, DeliveryMissionContract):
        return value
    return DeliveryMissionContract.model_validate(dict(value))


def _to_scenario(
    value: GazeboDeliveryScenario | Mapping[str, Any],
) -> GazeboDeliveryScenario:
    if isinstance(value, GazeboDeliveryScenario):
        return value
    return GazeboDeliveryScenario.model_validate(dict(value))


def _to_episode(
    value: SimulatedDeliveryEpisode | Mapping[str, Any],
) -> SimulatedDeliveryEpisode:
    if isinstance(value, SimulatedDeliveryEpisode):
        return value
    return SimulatedDeliveryEpisode.model_validate(dict(value))


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


def _bool_measurement(
    telemetry: Px4GazeboSanitizedTelemetry | None,
    key: str,
) -> bool:
    if telemetry is None:
        return False
    value = telemetry.measurements.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "reached"}
    return bool(value) if isinstance(value, int | float) else False


def _float_measurement(
    telemetry: Px4GazeboSanitizedTelemetry | None,
    key: str,
) -> float:
    if telemetry is None:
        return 0.0
    value = telemetry.measurements.get(key)
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_tuple(values: list[str]) -> tuple[str, ...]:
    return tuple(sorted({item for item in values if item}))


def build_delivery_progress_review(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    gazebo_delivery_scenario: GazeboDeliveryScenario | Mapping[str, Any],
    simulated_delivery_episode: SimulatedDeliveryEpisode | Mapping[str, Any],
    sanitized_telemetry: Px4GazeboSanitizedTelemetry | Mapping[str, Any] | None = None,
    hil_telemetry_review: HilTelemetryReview | Mapping[str, Any] | None = None,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> DeliveryProgressReview:
    """Build a non-executing progress review for a simulated delivery episode."""

    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    contract = _to_contract(delivery_mission_contract)
    scenario = _to_scenario(gazebo_delivery_scenario)
    episode = _to_episode(simulated_delivery_episode)
    telemetry = _to_sanitized_telemetry(sanitized_telemetry)
    hil_review = _to_hil_review(hil_telemetry_review)
    evaluated_at = _utc(now)

    if scenario.delivery_mission_contract_id != contract.contract_id:
        raise DeliveryProgressReviewError("gazebo scenario contract_id mismatch")
    if scenario.delivery_mission_id != contract.mission_id:
        raise DeliveryProgressReviewError("gazebo scenario mission_id mismatch")
    if episode.delivery_mission_contract_id != contract.contract_id:
        raise DeliveryProgressReviewError("simulated episode contract_id mismatch")
    if episode.mission_id != contract.mission_id:
        raise DeliveryProgressReviewError("simulated episode mission_id mismatch")

    findings: list[DeliveryProgressFinding] = []
    blocked: list[str] = []
    warnings: list[str] = []
    if telemetry is None:
        findings.append(
            DeliveryProgressFinding(
                bucket=DELIVERY_PROGRESS_BUCKET_TELEMETRY_MISSING,
                reason="delivery_progress_requires_sanitized_telemetry",
                severity=DeliveryProgressSeverity.BLOCKING,
                detail={"required": True},
            )
        )
        blocked.append(DELIVERY_PROGRESS_BUCKET_TELEMETRY_MISSING)

    if hil_review is not None and hil_review.status is HilTelemetryReviewStatus.BLOCKED:
        findings.append(
            DeliveryProgressFinding(
                bucket=DELIVERY_PROGRESS_BUCKET_HIL_REVIEW_BLOCKED,
                reason="hil_telemetry_review_blocked_delivery_progress",
                severity=DeliveryProgressSeverity.BLOCKING,
                detail={
                    "hil_telemetry_review_id": hil_review.review_id,
                    "blocked_reasons": list(hil_review.blocked_reasons),
                },
            )
        )
        blocked.append(DELIVERY_PROGRESS_BUCKET_HIL_REVIEW_BLOCKED)
        if HIL_REVIEW_BUCKET_STALE in hil_review.blocked_reasons:
            findings.append(
                DeliveryProgressFinding(
                    bucket=DELIVERY_PROGRESS_BUCKET_HIL_TELEMETRY_STALE,
                    reason="hil_telemetry_review_reported_stale_delivery_telemetry",
                    severity=DeliveryProgressSeverity.BLOCKING,
                    detail={"hil_telemetry_review_id": hil_review.review_id},
                )
            )
            blocked.append(DELIVERY_PROGRESS_BUCKET_HIL_TELEMETRY_STALE)

    if not episode.passed or episode.phase is SimulatedDeliveryEpisodePhase.BLOCKED:
        findings.append(
            DeliveryProgressFinding(
                bucket=DELIVERY_PROGRESS_BUCKET_EPISODE_BLOCKED,
                reason="simulated_delivery_episode_is_blocked",
                severity=DeliveryProgressSeverity.BLOCKING,
                detail={
                    "episode_id": episode.episode_id,
                    "blocked_reasons": list(episode.blocked_reasons),
                },
            )
        )
        blocked.append(DELIVERY_PROGRESS_BUCKET_EPISODE_BLOCKED)

    pickup_reached = _bool_measurement(telemetry, "pickup_reached")
    dropoff_reached = _bool_measurement(telemetry, "dropoff_reached")
    route_progress_percent = max(
        0.0,
        min(100.0, _float_measurement(telemetry, "route_progress_percent")),
    )
    route_geofence_violation = _bool_measurement(
        telemetry,
        "route_geofence_violation",
    ) or _bool_measurement(telemetry, "geofence_violation")
    if route_geofence_violation:
        findings.append(
            DeliveryProgressFinding(
                bucket=DELIVERY_PROGRESS_BUCKET_ROUTE_GEOFENCE_VIOLATION,
                reason="route_or_geofence_violation_observed_in_simulated_delivery",
                severity=DeliveryProgressSeverity.BLOCKING,
                detail={
                    "route_progress_percent": route_progress_percent,
                    "sanitized_telemetry_id": telemetry.telemetry_id
                    if telemetry
                    else None,
                    "gazebo_delivery_scenario_id": scenario.scenario_id,
                },
            )
        )
        blocked.append(DELIVERY_PROGRESS_BUCKET_ROUTE_GEOFENCE_VIOLATION)
    landing_zone_available = (
        telemetry.measurements.get("landing_zone_available")
        if telemetry is not None
        else None
    )
    dropoff_zone_available = (
        telemetry.measurements.get("dropoff_zone_available")
        if telemetry is not None
        else None
    )
    landing_zone_unavailable = (
        landing_zone_available is False
        or dropoff_zone_available is False
        or _bool_measurement(telemetry, "landing_zone_unavailable")
        or _bool_measurement(telemetry, "dropoff_zone_unavailable")
    )
    if landing_zone_unavailable:
        findings.append(
            DeliveryProgressFinding(
                bucket=DELIVERY_PROGRESS_BUCKET_LANDING_ZONE_UNAVAILABLE,
                reason="landing_or_dropoff_zone_unavailable_in_simulated_delivery",
                severity=DeliveryProgressSeverity.BLOCKING,
                detail={
                    "sanitized_telemetry_id": telemetry.telemetry_id
                    if telemetry
                    else None,
                    "landing_zone_available": landing_zone_available,
                    "dropoff_zone_available": dropoff_zone_available,
                    "dropoff_pad_id": scenario.dropoff_pad.pad_id,
                },
            )
        )
        blocked.append(DELIVERY_PROGRESS_BUCKET_LANDING_ZONE_UNAVAILABLE)
    if pickup_reached:
        findings.append(
            DeliveryProgressFinding(
                bucket=DELIVERY_PROGRESS_BUCKET_PICKUP_REACHED,
                reason="pickup_pad_reached_in_simulated_delivery",
                severity=DeliveryProgressSeverity.INFO,
                detail={"pickup_pad_id": scenario.pickup_pad.pad_id},
            )
        )
    if route_progress_percent > 0.0:
        findings.append(
            DeliveryProgressFinding(
                bucket=DELIVERY_PROGRESS_BUCKET_ROUTE_PROGRESS_OBSERVED,
                reason="route_progress_observed_in_simulated_delivery",
                severity=DeliveryProgressSeverity.INFO,
                detail={"route_progress_percent": route_progress_percent},
            )
        )
    if dropoff_reached:
        findings.append(
            DeliveryProgressFinding(
                bucket=DELIVERY_PROGRESS_BUCKET_DROPOFF_REACHED,
                reason="dropoff_pad_reached_in_simulated_delivery",
                severity=DeliveryProgressSeverity.INFO,
                detail={"dropoff_pad_id": scenario.dropoff_pad.pad_id},
            )
        )
    completion_criteria_met = dropoff_reached and not blocked
    if completion_criteria_met:
        findings.append(
            DeliveryProgressFinding(
                bucket=DELIVERY_PROGRESS_BUCKET_COMPLETION_CRITERIA_MET,
                reason="delivery_dropoff_reached_and_gate_not_blocked",
                severity=DeliveryProgressSeverity.INFO,
                detail={"success_criteria": list(contract.success_criteria)},
            )
        )

    blocked_reasons = _as_tuple(blocked)
    warning_reasons = _as_tuple(warnings)
    if blocked_reasons:
        status = DeliveryProgressStatus.BLOCKED
    elif completion_criteria_met:
        status = DeliveryProgressStatus.COMPLETED
    elif pickup_reached or route_progress_percent > 0.0:
        status = DeliveryProgressStatus.IN_PROGRESS
    else:
        status = DeliveryProgressStatus.PREFLIGHT_READY

    telemetry_refs = []
    if telemetry is not None:
        telemetry_refs.append(f"px4_gazebo_sanitized_telemetry:{telemetry.telemetry_id}")
    if hil_review is not None:
        telemetry_refs.append(f"hil_telemetry_review:{hil_review.review_id}")
    episode_refs = [f"simulated_delivery_episode:{episode.episode_id}"]
    scenario_refs = [f"gazebo_delivery_scenario:{scenario.scenario_id}"]
    gate_refs = list(episode.gate_refs)
    payload = {
        "delivery_mission_contract_id": contract.contract_id,
        "gazebo_delivery_scenario_id": scenario.scenario_id,
        "simulated_delivery_episode_id": episode.episode_id,
        "sanitized_telemetry_id": telemetry.telemetry_id if telemetry else None,
        "hil_telemetry_review_id": hil_review.review_id if hil_review else None,
        "status": status.value,
        "blocked_reasons": blocked_reasons,
        "warning_reasons": warning_reasons,
        "pickup_reached": pickup_reached,
        "dropoff_reached": dropoff_reached,
        "route_progress_percent": route_progress_percent,
        "route_geofence_violation": route_geofence_violation,
        "landing_zone_unavailable": landing_zone_unavailable,
    }
    return DeliveryProgressReview(
        progress_review_id=_stable_id("delivery_progress_review", payload),
        delivery_mission_contract_id=contract.contract_id,
        delivery_mission_id=contract.mission_id,
        gazebo_delivery_scenario_id=scenario.scenario_id,
        simulated_delivery_episode_id=episode.episode_id,
        sanitized_telemetry_id=telemetry.telemetry_id if telemetry else None,
        hil_telemetry_review_id=hil_review.review_id if hil_review else None,
        status=status,
        passed=status is not DeliveryProgressStatus.BLOCKED,
        pickup_reached=pickup_reached,
        dropoff_reached=dropoff_reached,
        route_progress_percent=route_progress_percent,
        completion_criteria_met=completion_criteria_met,
        blocked_reasons=blocked_reasons,
        warning_reasons=warning_reasons,
        findings=tuple(findings),
        telemetry_refs=tuple(sorted(telemetry_refs)),
        episode_refs=tuple(sorted(episode_refs)),
        scenario_refs=tuple(sorted(scenario_refs)),
        gate_refs=tuple(sorted(gate_refs)),
        evaluated_at=evaluated_at,
        metadata={
            **metadata_payload,
            "artifact_only": True,
            "progress_review_only": True,
            "simulation_progress_review_only": True,
            "no_dispatch_surface": True,
            "no_entity_mutation": True,
        },
    )


def attach_delivery_progress_review(
    task_id: str,
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    gazebo_delivery_scenario: GazeboDeliveryScenario | Mapping[str, Any],
    simulated_delivery_episode: SimulatedDeliveryEpisode | Mapping[str, Any],
    sanitized_telemetry: Px4GazeboSanitizedTelemetry | Mapping[str, Any] | None = None,
    hil_telemetry_review: HilTelemetryReview | Mapping[str, Any] | None = None,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach a progress review without mutating task status."""

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise DeliveryProgressReviewError(
            f"task {task_id} not found; cannot attach delivery progress review"
        )
    review = build_delivery_progress_review(
        delivery_mission_contract=delivery_mission_contract,
        gazebo_delivery_scenario=gazebo_delivery_scenario,
        simulated_delivery_episode=simulated_delivery_episode,
        sanitized_telemetry=sanitized_telemetry,
        hil_telemetry_review=hil_telemetry_review,
        now=now,
    )
    artifacts = {"delivery_progress_review": review.model_dump(mode="json")}
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise DeliveryProgressReviewError(
            f"task {task_id} disappeared while attaching delivery progress review"
        )
    return artifacts


__all__ = [
    "DELIVERY_PROGRESS_BUCKET_COMPLETION_CRITERIA_MET",
    "DELIVERY_PROGRESS_BUCKET_DROPOFF_REACHED",
    "DELIVERY_PROGRESS_BUCKET_EPISODE_BLOCKED",
    "DELIVERY_PROGRESS_BUCKET_HIL_REVIEW_BLOCKED",
    "DELIVERY_PROGRESS_BUCKET_HIL_TELEMETRY_STALE",
    "DELIVERY_PROGRESS_BUCKET_LANDING_ZONE_UNAVAILABLE",
    "DELIVERY_PROGRESS_BUCKET_PICKUP_REACHED",
    "DELIVERY_PROGRESS_BUCKET_ROUTE_PROGRESS_OBSERVED",
    "DELIVERY_PROGRESS_BUCKET_ROUTE_GEOFENCE_VIOLATION",
    "DELIVERY_PROGRESS_BUCKET_TELEMETRY_MISSING",
    "DELIVERY_PROGRESS_REVIEW_SCHEMA_VERSION",
    "DeliveryProgressFinding",
    "DeliveryProgressReview",
    "DeliveryProgressReviewError",
    "DeliveryProgressSeverity",
    "DeliveryProgressStatus",
    "attach_delivery_progress_review",
    "build_delivery_progress_review",
]
