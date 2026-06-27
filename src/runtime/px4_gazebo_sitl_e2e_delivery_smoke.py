"""PX4/Gazebo SITL same-session delivery E2E smoke summary artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

PX4_GAZEBO_SITL_E2E_DELIVERY_SMOKE_RESULT_SCHEMA_VERSION = (
    "px4_gazebo_sitl_e2e_delivery_smoke_result.v1"
)
PX4_GAZEBO_SITL_E2E_DELIVERY_EPIC_EXIT_RESULT_SCHEMA_VERSION = (
    "px4_gazebo_sitl_e2e_delivery_epic_exit_result.v1"
)


class PX4GazeboSITLE2EDeliverySmokeError(RuntimeError):
    """Raised when an E2E delivery smoke result is inconsistent."""


class PX4GazeboSITLE2EDeliverySmokeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        PX4_GAZEBO_SITL_E2E_DELIVERY_SMOKE_RESULT_SCHEMA_VERSION
    ] = PX4_GAZEBO_SITL_E2E_DELIVERY_SMOKE_RESULT_SCHEMA_VERSION
    result_id: str
    prompt: str
    result_status: Literal["flight_only_completed_payload_release_pending"]
    executed_in_same_sitl_session: Literal[True]
    mission_upload_observed: Literal[True]
    mission_ack_observed: Literal[True]
    mission_ack_type: Literal[0]
    mission_request_sequences: tuple[int, ...]
    actual_takeoff_observed: Literal[True]
    actual_dropoff_region_reached: Literal[True]
    actual_land_observed: Literal[True]
    payload_release_observed: Literal[False] = False
    payload_release_verified: Literal[False] = False
    epic_exit_complete: Literal[False] = False
    blocked_reasons: tuple[str, ...] = ("payload_release_event_not_observed",)
    external_dispatch_performed: Literal[True]
    external_dispatch_scope: Literal["same_session_sitl_mission_upload"]
    mavlink_dispatch_performed: Literal[True]
    px4_mission_upload_performed: Literal[True]
    gazebo_simulator_command_performed: Literal[True]
    gazebo_entity_mutation_performed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    artifact_manifest: dict[str, Any]
    observed_at: datetime

    @field_validator("mission_request_sequences", "blocked_reasons", mode="before")
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[Any, ...]:
        return tuple(value or ())

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_result(self) -> "PX4GazeboSITLE2EDeliverySmokeResult":
        if self.mission_request_sequences != (0, 1, 2, 3):
            raise PX4GazeboSITLE2EDeliverySmokeError(
                "same-session SITL upload must observe mission request sequence 0..3"
            )
        if self.epic_exit_complete:
            raise PX4GazeboSITLE2EDeliverySmokeError(
                "flight-only smoke cannot mark epic exit complete"
            )
        if self.payload_release_observed or self.payload_release_verified:
            raise PX4GazeboSITLE2EDeliverySmokeError(
                "payload release must not be synthesized by flight-only smoke"
            )
        if "payload_release_event_not_observed" not in self.blocked_reasons:
            raise PX4GazeboSITLE2EDeliverySmokeError(
                "flight-only smoke must record pending payload release"
            )
        return self


class PX4GazeboSITLE2EDeliveryEpicExitResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        PX4_GAZEBO_SITL_E2E_DELIVERY_EPIC_EXIT_RESULT_SCHEMA_VERSION
    ] = PX4_GAZEBO_SITL_E2E_DELIVERY_EPIC_EXIT_RESULT_SCHEMA_VERSION
    result_id: str
    prompt: str
    result_status: Literal["delivery_completed_payload_release_verified"]
    executed_in_same_sitl_session: Literal[True]
    mission_upload_observed: Literal[True]
    mission_ack_observed: Literal[True]
    mission_ack_type: Literal[0]
    mission_request_sequences: tuple[int, ...]
    actual_takeoff_observed: Literal[True]
    actual_dropoff_region_reached: Literal[True]
    actual_land_observed: Literal[True]
    payload_release_observed: Literal[True]
    payload_release_verified: Literal[True]
    epic_exit_complete: Literal[True]
    blocked_reasons: tuple[str, ...] = ()
    payload_release_event_ref: str
    dropoff_verification_ref: str
    payload_release_event_source: Literal["gazebo_detachable_joint_detach_event"]
    external_dispatch_performed: Literal[True]
    external_dispatch_scope: Literal[
        "same_session_sitl_mission_upload_and_detachable_joint_release"
    ]
    mavlink_dispatch_performed: Literal[True]
    px4_mission_upload_performed: Literal[True]
    gazebo_simulator_command_performed: Literal[True]
    gazebo_detachable_joint_release_performed: Literal[True]
    gazebo_detachable_joint_release_observed: Literal[True]
    gazebo_entity_mutation_performed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    artifact_manifest: dict[str, Any]
    observed_at: datetime

    @field_validator("mission_request_sequences", "blocked_reasons", mode="before")
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[Any, ...]:
        return tuple(value or ())

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_result(self) -> "PX4GazeboSITLE2EDeliveryEpicExitResult":
        if self.mission_request_sequences != (0, 1, 2, 3):
            raise PX4GazeboSITLE2EDeliverySmokeError(
                "same-session SITL upload must observe mission request sequence 0..3"
            )
        if self.blocked_reasons:
            raise PX4GazeboSITLE2EDeliverySmokeError(
                "epic-exit delivery result cannot have blocked reasons"
            )
        if not self.payload_release_event_ref.startswith(
            "px4_gazebo_sitl_payload_release_event:"
        ):
            raise PX4GazeboSITLE2EDeliverySmokeError(
                "epic-exit delivery result requires payload release event ref"
            )
        if not self.dropoff_verification_ref.startswith(
            "px4_gazebo_sitl_dropoff_verification:"
        ):
            raise PX4GazeboSITLE2EDeliverySmokeError(
                "epic-exit delivery result requires dropoff verification ref"
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


def build_px4_gazebo_sitl_e2e_delivery_smoke_result(
    *,
    prompt: str,
    horizontal_summary: Mapping[str, Any],
    artifact_manifest: Mapping[str, Any],
    observed_at: datetime | None = None,
) -> PX4GazeboSITLE2EDeliverySmokeResult:
    payload = {
        "prompt": prompt,
        "artifact_dir": artifact_manifest.get("horizontal_route_artifact_dir"),
        "mission_requests": horizontal_summary.get(
            "preupload_mission_request_sequences"
        ),
        "completed_pose_z_m": horizontal_summary.get("completed_pose_z_m"),
    }
    return PX4GazeboSITLE2EDeliverySmokeResult(
        result_id=_stable_id("px4_gazebo_sitl_e2e_delivery_smoke_result", payload),
        prompt=prompt,
        result_status="flight_only_completed_payload_release_pending",
        executed_in_same_sitl_session=True,
        mission_upload_observed=True,
        mission_ack_observed=True,
        mission_ack_type=0,
        mission_request_sequences=tuple(
            int(item)
            for item in horizontal_summary.get(
                "preupload_mission_request_sequences", ()
            )
        ),
        actual_takeoff_observed=int(horizontal_summary.get("climb_sample_count", 0))
        > 0,
        actual_dropoff_region_reached=bool(
            horizontal_summary.get("dropoff_region_reached", False)
        ),
        actual_land_observed=float(horizontal_summary.get("completed_pose_z_m", 999.0))
        <= 0.15,
        external_dispatch_performed=True,
        external_dispatch_scope="same_session_sitl_mission_upload",
        mavlink_dispatch_performed=True,
        px4_mission_upload_performed=True,
        gazebo_simulator_command_performed=True,
        gazebo_entity_mutation_performed=False,
        hardware_target_allowed=False,
        physical_execution_invoked=False,
        artifact_manifest=dict(artifact_manifest),
        observed_at=_utc(observed_at),
    )


def build_px4_gazebo_sitl_e2e_delivery_epic_exit_result(
    *,
    prompt: str,
    horizontal_summary: Mapping[str, Any],
    payload_release_event_ref: str,
    dropoff_verification_ref: str,
    artifact_manifest: Mapping[str, Any],
    observed_at: datetime | None = None,
) -> PX4GazeboSITLE2EDeliveryEpicExitResult:
    if horizontal_summary.get("payload_release_observed") is not True:
        raise PX4GazeboSITLE2EDeliverySmokeError(
            "epic-exit delivery result requires observed payload release"
        )
    if (
        horizontal_summary.get("payload_release_event_source")
        != "gazebo_detachable_joint_detach_event"
    ):
        raise PX4GazeboSITLE2EDeliverySmokeError(
            "epic-exit delivery result requires Gazebo detachable-joint release"
        )
    payload = {
        "prompt": prompt,
        "artifact_dir": artifact_manifest.get("horizontal_route_artifact_dir"),
        "mission_requests": horizontal_summary.get(
            "preupload_mission_request_sequences"
        ),
        "release_ref": payload_release_event_ref,
        "verification_ref": dropoff_verification_ref,
    }
    return PX4GazeboSITLE2EDeliveryEpicExitResult(
        result_id=_stable_id("px4_gazebo_sitl_e2e_delivery_epic_exit_result", payload),
        prompt=prompt,
        result_status="delivery_completed_payload_release_verified",
        executed_in_same_sitl_session=True,
        mission_upload_observed=True,
        mission_ack_observed=True,
        mission_ack_type=0,
        mission_request_sequences=tuple(
            int(item)
            for item in horizontal_summary.get(
                "preupload_mission_request_sequences", ()
            )
        ),
        actual_takeoff_observed=int(horizontal_summary.get("climb_sample_count", 0))
        > 0,
        actual_dropoff_region_reached=bool(
            horizontal_summary.get("dropoff_region_reached", False)
        ),
        actual_land_observed=float(horizontal_summary.get("completed_pose_z_m", 999.0))
        <= 0.15,
        payload_release_observed=True,
        payload_release_verified=True,
        epic_exit_complete=True,
        blocked_reasons=(),
        payload_release_event_ref=payload_release_event_ref,
        dropoff_verification_ref=dropoff_verification_ref,
        payload_release_event_source="gazebo_detachable_joint_detach_event",
        external_dispatch_performed=True,
        external_dispatch_scope=(
            "same_session_sitl_mission_upload_and_detachable_joint_release"
        ),
        mavlink_dispatch_performed=True,
        px4_mission_upload_performed=True,
        gazebo_simulator_command_performed=True,
        gazebo_detachable_joint_release_performed=True,
        gazebo_detachable_joint_release_observed=True,
        gazebo_entity_mutation_performed=False,
        hardware_target_allowed=False,
        physical_execution_invoked=False,
        artifact_manifest=dict(artifact_manifest),
        observed_at=_utc(observed_at),
    )


__all__ = [
    "PX4_GAZEBO_SITL_E2E_DELIVERY_EPIC_EXIT_RESULT_SCHEMA_VERSION",
    "PX4_GAZEBO_SITL_E2E_DELIVERY_SMOKE_RESULT_SCHEMA_VERSION",
    "PX4GazeboSITLE2EDeliveryEpicExitResult",
    "PX4GazeboSITLE2EDeliverySmokeError",
    "PX4GazeboSITLE2EDeliverySmokeResult",
    "build_px4_gazebo_sitl_e2e_delivery_epic_exit_result",
    "build_px4_gazebo_sitl_e2e_delivery_smoke_result",
]
