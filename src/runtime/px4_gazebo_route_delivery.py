"""Route delivery completion gate for PX4/Gazebo pickup-dropoff missions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.px4_gazebo_route_dispatcher import (
    PX4GazeboRouteCommandDispatchResult,
    PX4GazeboRouteDispatchStatus,
    PX4GazeboRouteProgressEvidence,
)
from src.runtime.px4_gazebo_route_plan import PX4GazeboPickupDropoffRoutePlan
from src.runtime.task_store import TaskStore, get_task_store

PX4_GAZEBO_ROUTE_DELIVERY_COMPLETION_GATE_SCHEMA_VERSION = (
    "px4_gazebo_route_delivery_completion_gate.v1"
)
PX4_GAZEBO_ROUTE_DELIVERY_RUNNER_RESULT_SCHEMA_VERSION = (
    "px4_gazebo_route_delivery_runner_result.v1"
)
ROUTE_DELIVERY_COMPLETION_BASIS = (
    "route_dispatch_progress_and_telemetry_completion_gate"
)


class PX4GazeboRouteDeliveryError(RuntimeError):
    """Raised when route delivery evidence cannot be accepted safely."""


class PX4GazeboRouteDeliveryStatus(str, Enum):
    COMPLETED = "completed"
    BLOCKED = "blocked"


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


def _ordered_tuple(values: Sequence[str] | None) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in values or ():
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


def _route_plan_ref(route_plan: PX4GazeboPickupDropoffRoutePlan) -> str:
    return f"px4_gazebo_pickup_dropoff_route_plan:{route_plan.route_plan_id}"


def _route_dispatch_ref(dispatch: PX4GazeboRouteCommandDispatchResult) -> str:
    return f"px4_gazebo_route_command_dispatch_result:{dispatch.dispatch_result_id}"


def _route_progress_ref(progress: PX4GazeboRouteProgressEvidence) -> str:
    return f"px4_gazebo_route_progress_evidence:{progress.progress_evidence_id}"


def _coerce_route_plan(
    value: PX4GazeboPickupDropoffRoutePlan | Mapping[str, Any],
) -> PX4GazeboPickupDropoffRoutePlan:
    if isinstance(value, PX4GazeboPickupDropoffRoutePlan):
        return value
    return PX4GazeboPickupDropoffRoutePlan.model_validate(dict(value))


def _coerce_dispatch(
    value: PX4GazeboRouteCommandDispatchResult | Mapping[str, Any],
) -> PX4GazeboRouteCommandDispatchResult:
    if isinstance(value, PX4GazeboRouteCommandDispatchResult):
        return value
    return PX4GazeboRouteCommandDispatchResult.model_validate(dict(value))


def _coerce_progress(
    value: PX4GazeboRouteProgressEvidence | Mapping[str, Any],
) -> PX4GazeboRouteProgressEvidence:
    if isinstance(value, PX4GazeboRouteProgressEvidence):
        return value
    return PX4GazeboRouteProgressEvidence.model_validate(dict(value))


class _RouteDeliverySafetyBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    simulation_only: Literal[True] = True
    px4_sitl_only: Literal[True] = True
    gazebo_only: Literal[True] = True
    operator_approval_required: Literal[True] = True
    bounded_allowlist_enforced: Literal[True] = True
    route_plan_required: Literal[True] = True
    route_dispatch_required: Literal[True] = True
    route_progress_required: Literal[True] = True
    telemetry_completion_gate_required: Literal[True] = True
    hardware_target_allowed: Literal[False] = False
    real_world_authority_granted: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    arbitrary_mission_upload_allowed: Literal[False] = False
    unbounded_setpoint_stream_allowed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    retry_attempted: Literal[False] = False
    stronger_execution_attempted: Literal[False] = False


class PX4GazeboRouteDeliveryCompletionGate(_RouteDeliverySafetyBoundary):
    schema_version: Literal[
        PX4_GAZEBO_ROUTE_DELIVERY_COMPLETION_GATE_SCHEMA_VERSION
    ] = PX4_GAZEBO_ROUTE_DELIVERY_COMPLETION_GATE_SCHEMA_VERSION
    completion_gate_id: str
    final_status: PX4GazeboRouteDeliveryStatus
    completion_basis: Literal[ROUTE_DELIVERY_COMPLETION_BASIS] = (
        ROUTE_DELIVERY_COMPLETION_BASIS
    )
    route_plan_ref: str = Field(min_length=1)
    route_dispatch_result_ref: str = Field(min_length=1)
    route_progress_evidence_ref: str = Field(min_length=1)
    required_completion_evidence: tuple[str, ...]
    observed_completion_evidence: tuple[str, ...]
    missing_completion_evidence: tuple[str, ...]
    blocked_reasons: tuple[str, ...]
    dropoff_region_reached: bool
    route_geofence_violation: bool
    route_progress_fresh: bool
    route_progress_age_seconds: float = Field(ge=0)
    max_route_progress_age_seconds: float = Field(gt=0)
    pose_observed: bool
    expected_vehicle_ref: str = Field(min_length=1)
    observed_vehicle_ref: str | None = None
    wrong_delivery_vehicle: bool
    horizontal_progress_m: float = Field(ge=0)
    horizontal_route_motion_observed: bool
    px4_telemetry_correlated: bool
    gazebo_pose_correlated: bool
    actual_px4_gazebo_horizontal_smoke_observed: bool
    evaluated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("evaluated_at", mode="before")
    @classmethod
    def _coerce_evaluated_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator(
        "required_completion_evidence",
        "observed_completion_evidence",
        "missing_completion_evidence",
        "blocked_reasons",
        mode="before",
    )
    @classmethod
    def _coerce_evidence(cls, value: Any) -> tuple[str, ...]:
        return _ordered_tuple([str(item) for item in value])

    @model_validator(mode="after")
    def _validate_gate(self) -> "PX4GazeboRouteDeliveryCompletionGate":
        if self.final_status == PX4GazeboRouteDeliveryStatus.COMPLETED:
            if self.blocked_reasons:
                raise PX4GazeboRouteDeliveryError(
                    "completed route delivery cannot have blocked reasons"
                )
            if self.missing_completion_evidence:
                raise PX4GazeboRouteDeliveryError(
                    "completed route delivery cannot have missing completion evidence"
                )
            if not self.dropoff_region_reached:
                raise PX4GazeboRouteDeliveryError(
                    "completed route delivery requires dropoff region reached"
                )
            if self.route_geofence_violation:
                raise PX4GazeboRouteDeliveryError(
                    "completed route delivery cannot have geofence violation"
                )
            if not self.horizontal_route_motion_observed:
                raise PX4GazeboRouteDeliveryError(
                    "completed route delivery requires horizontal motion evidence"
                )
            if not self.px4_telemetry_correlated or not self.gazebo_pose_correlated:
                raise PX4GazeboRouteDeliveryError(
                    "completed route delivery requires PX4 and Gazebo correlation"
                )
            if not self.route_progress_fresh:
                raise PX4GazeboRouteDeliveryError(
                    "completed route delivery requires fresh route progress"
                )
            if not self.pose_observed:
                raise PX4GazeboRouteDeliveryError(
                    "completed route delivery requires observed pose"
                )
            if self.wrong_delivery_vehicle:
                raise PX4GazeboRouteDeliveryError(
                    "completed route delivery cannot use wrong vehicle evidence"
                )
        return self


class PX4GazeboRouteDeliveryRunnerResult(_RouteDeliverySafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_ROUTE_DELIVERY_RUNNER_RESULT_SCHEMA_VERSION] = (
        PX4_GAZEBO_ROUTE_DELIVERY_RUNNER_RESULT_SCHEMA_VERSION
    )
    runner_result_id: str
    final_status: PX4GazeboRouteDeliveryStatus
    completion_gate_ref: str = Field(min_length=1)
    completion_basis: Literal[ROUTE_DELIVERY_COMPLETION_BASIS] = (
        ROUTE_DELIVERY_COMPLETION_BASIS
    )
    route_plan_ref: str = Field(min_length=1)
    route_dispatch_result_ref: str = Field(min_length=1)
    route_progress_evidence_ref: str = Field(min_length=1)
    observed_completion_evidence: tuple[str, ...]
    missing_completion_evidence: tuple[str, ...]
    completed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("completed_at", mode="before")
    @classmethod
    def _coerce_completed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator(
        "observed_completion_evidence",
        "missing_completion_evidence",
        mode="before",
    )
    @classmethod
    def _coerce_evidence(cls, value: Any) -> tuple[str, ...]:
        return _ordered_tuple([str(item) for item in value])


def build_px4_gazebo_route_delivery_completion_gate(
    *,
    route_plan: PX4GazeboPickupDropoffRoutePlan | Mapping[str, Any],
    route_dispatch_result: PX4GazeboRouteCommandDispatchResult | Mapping[str, Any],
    route_progress_evidence: PX4GazeboRouteProgressEvidence | Mapping[str, Any],
    horizontal_route_motion_observed: bool,
    px4_telemetry_correlated: bool,
    gazebo_pose_correlated: bool,
    route_progress_age_seconds: float = 0.0,
    max_route_progress_age_seconds: float = 5.0,
    pose_observed: bool = True,
    expected_vehicle_ref: str = "gazebo_vehicle:x500_0",
    observed_vehicle_ref: str | None = "gazebo_vehicle:x500_0",
    actual_px4_gazebo_horizontal_smoke_observed: bool = False,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboRouteDeliveryCompletionGate:
    resolved_route = _coerce_route_plan(route_plan)
    dispatch = _coerce_dispatch(route_dispatch_result)
    progress = _coerce_progress(route_progress_evidence)
    if dispatch.route_plan_ref != _route_plan_ref(resolved_route):
        raise PX4GazeboRouteDeliveryError(
            "route delivery completion gate route plan mismatch"
        )
    if progress.route_plan_ref != _route_plan_ref(resolved_route):
        raise PX4GazeboRouteDeliveryError(
            "route delivery completion gate progress route mismatch"
        )
    if progress.route_dispatch_result_ref != _route_dispatch_ref(dispatch):
        raise PX4GazeboRouteDeliveryError(
            "route delivery completion gate dispatch/progress mismatch"
        )
    if dispatch.route_dispatch_status != PX4GazeboRouteDispatchStatus.SENT:
        raise PX4GazeboRouteDeliveryError(
            "route delivery completion gate requires sent route dispatch"
        )
    observed = []
    if progress.horizontal_progress_m > 0:
        observed.append("pickup_pad_departed")
    if progress.dropoff_region_reached and not progress.route_geofence_violation:
        observed.append("dropoff_pad_reached")
    if px4_telemetry_correlated:
        observed.append("px4_telemetry_correlated")
    if gazebo_pose_correlated:
        observed.append("gazebo_pose_correlated")
    if horizontal_route_motion_observed:
        observed.append("horizontal_route_motion_observed")
    observed_tuple = _ordered_tuple(observed)
    required = resolved_route.required_completion_evidence
    missing = tuple(item for item in required if item not in observed_tuple)
    route_progress_fresh = float(route_progress_age_seconds) <= float(
        max_route_progress_age_seconds
    )
    wrong_delivery_vehicle = (
        observed_vehicle_ref is not None
        and observed_vehicle_ref != expected_vehicle_ref
    )
    blocked_reasons = []
    if missing:
        blocked_reasons.extend(f"missing_{item}" for item in missing)
    if progress.route_geofence_violation:
        blocked_reasons.append("route_geofence_violation")
    if not route_progress_fresh:
        blocked_reasons.append("stale_route_progress")
    if not pose_observed:
        blocked_reasons.append("route_pose_missing")
    if wrong_delivery_vehicle:
        blocked_reasons.append("wrong_delivery_vehicle")
    if not progress.dropoff_region_reached:
        blocked_reasons.append("dropoff_region_not_reached")
    if not horizontal_route_motion_observed:
        blocked_reasons.append("horizontal_route_motion_missing")
    final_status = (
        PX4GazeboRouteDeliveryStatus.COMPLETED
        if not missing
        and progress.dropoff_region_reached
        and not progress.route_geofence_violation
        and horizontal_route_motion_observed
        and route_progress_fresh
        and pose_observed
        and not wrong_delivery_vehicle
        else PX4GazeboRouteDeliveryStatus.BLOCKED
    )
    evaluated_at = _utc(now)
    payload = {
        "route_plan_id": resolved_route.route_plan_id,
        "dispatch_result_id": dispatch.dispatch_result_id,
        "progress_evidence_id": progress.progress_evidence_id,
        "observed_completion_evidence": observed_tuple,
        "missing_completion_evidence": missing,
        "blocked_reasons": blocked_reasons,
        "final_status": final_status.value,
        "evaluated_at": evaluated_at.isoformat(),
    }
    return PX4GazeboRouteDeliveryCompletionGate(
        completion_gate_id=_stable_id("px4_gazebo_route_delivery_completion", payload),
        final_status=final_status,
        route_plan_ref=_route_plan_ref(resolved_route),
        route_dispatch_result_ref=_route_dispatch_ref(dispatch),
        route_progress_evidence_ref=_route_progress_ref(progress),
        required_completion_evidence=required,
        observed_completion_evidence=observed_tuple,
        missing_completion_evidence=missing,
        blocked_reasons=_ordered_tuple(blocked_reasons),
        dropoff_region_reached=progress.dropoff_region_reached,
        route_geofence_violation=progress.route_geofence_violation,
        route_progress_fresh=route_progress_fresh,
        route_progress_age_seconds=float(route_progress_age_seconds),
        max_route_progress_age_seconds=float(max_route_progress_age_seconds),
        pose_observed=bool(pose_observed),
        expected_vehicle_ref=expected_vehicle_ref,
        observed_vehicle_ref=observed_vehicle_ref,
        wrong_delivery_vehicle=wrong_delivery_vehicle,
        horizontal_progress_m=progress.horizontal_progress_m,
        horizontal_route_motion_observed=bool(horizontal_route_motion_observed),
        px4_telemetry_correlated=bool(px4_telemetry_correlated),
        gazebo_pose_correlated=bool(gazebo_pose_correlated),
        actual_px4_gazebo_horizontal_smoke_observed=bool(
            actual_px4_gazebo_horizontal_smoke_observed
        ),
        evaluated_at=evaluated_at,
        metadata={
            **(metadata or {}),
            "issue": 346,
            "parent_epic": 339,
            "actual_horizontal_smoke_issue": 345,
        },
    )


def build_px4_gazebo_route_delivery_runner_result(
    *,
    completion_gate: PX4GazeboRouteDeliveryCompletionGate | Mapping[str, Any],
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboRouteDeliveryRunnerResult:
    gate = (
        completion_gate
        if isinstance(completion_gate, PX4GazeboRouteDeliveryCompletionGate)
        else PX4GazeboRouteDeliveryCompletionGate.model_validate(dict(completion_gate))
    )
    completed_at = _utc(now)
    payload = {
        "completion_gate_id": gate.completion_gate_id,
        "final_status": gate.final_status.value,
        "completed_at": completed_at.isoformat(),
    }
    return PX4GazeboRouteDeliveryRunnerResult(
        runner_result_id=_stable_id("px4_gazebo_route_delivery_runner", payload),
        final_status=gate.final_status,
        completion_gate_ref=(
            f"px4_gazebo_route_delivery_completion_gate:{gate.completion_gate_id}"
        ),
        route_plan_ref=gate.route_plan_ref,
        route_dispatch_result_ref=gate.route_dispatch_result_ref,
        route_progress_evidence_ref=gate.route_progress_evidence_ref,
        observed_completion_evidence=gate.observed_completion_evidence,
        missing_completion_evidence=gate.missing_completion_evidence,
        completed_at=completed_at,
        metadata={
            **(metadata or {}),
            "issue": 346,
            "parent_epic": 339,
        },
    )


def run_px4_gazebo_route_delivery_task(
    task_id: str,
    *,
    completion_gate: PX4GazeboRouteDeliveryCompletionGate | Mapping[str, Any],
    now: datetime | None = None,
    task_store_factory: Any | None = None,
) -> dict[str, Any]:
    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise PX4GazeboRouteDeliveryError(
            f"task {task_id} not found; cannot run PX4/Gazebo route delivery"
        )
    gate = (
        completion_gate
        if isinstance(completion_gate, PX4GazeboRouteDeliveryCompletionGate)
        else PX4GazeboRouteDeliveryCompletionGate.model_validate(dict(completion_gate))
    )
    runner_result = build_px4_gazebo_route_delivery_runner_result(
        completion_gate=gate,
        now=now,
    )
    updated = store.update(
        task_id,
        status=runner_result.final_status.value,
        artifacts={
            "px4_gazebo_route_delivery_completion_gate": gate.model_dump(mode="json"),
            "px4_gazebo_route_delivery_runner_result": runner_result.model_dump(
                mode="json"
            ),
        },
        ended_at=time.time(),
    )
    if updated is None:
        raise PX4GazeboRouteDeliveryError(
            f"task {task_id} disappeared while running PX4/Gazebo route delivery"
        )
    return updated


__all__ = [
    "PX4_GAZEBO_ROUTE_DELIVERY_COMPLETION_GATE_SCHEMA_VERSION",
    "PX4_GAZEBO_ROUTE_DELIVERY_RUNNER_RESULT_SCHEMA_VERSION",
    "ROUTE_DELIVERY_COMPLETION_BASIS",
    "PX4GazeboRouteDeliveryCompletionGate",
    "PX4GazeboRouteDeliveryError",
    "PX4GazeboRouteDeliveryRunnerResult",
    "PX4GazeboRouteDeliveryStatus",
    "build_px4_gazebo_route_delivery_completion_gate",
    "build_px4_gazebo_route_delivery_runner_result",
    "run_px4_gazebo_route_delivery_task",
]
