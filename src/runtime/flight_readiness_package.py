"""Human-review Flight Readiness Package for source-backed Digital Twin SITL."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.digital_twin_sitl_execution_result import (
    DigitalTwinSITLExecutionResult,
    digital_twin_sitl_execution_result_ref,
)


FLIGHT_READINESS_PACKAGE_SCHEMA_VERSION = "flight_readiness_package.v1"


class FlightReadinessPackageError(RuntimeError):
    """Raised when a Flight Readiness Package overclaims readiness."""


def _utc(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _content_hash(payload: Mapping[str, Any]) -> str:
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _text_tuple(value: Sequence[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(item) for item in value or ())


class FlightReadinessPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[FLIGHT_READINESS_PACKAGE_SCHEMA_VERSION] = (
        FLIGHT_READINESS_PACKAGE_SCHEMA_VERSION
    )
    package_id: str
    execution_result_ref: str
    real_world_target_resolution_ref: str
    terrain_dem_source_snapshot_ref: str
    weather_source_snapshot_ref: str
    vehicle_flight_envelope_ref: str
    mission_energy_budget_ref: str
    gazebo_world_artifact_ref: str
    digital_twin_px4_mission_item_candidate_ref: str
    digital_twin_sitl_process_run_ref: str
    digital_twin_sitl_mission_upload_receipt_ref: str
    source_backed_inputs_summary: dict[str, Any] = Field(default_factory=dict)
    readiness_status: Literal["ready_for_human_hardware_review", "blocked"]
    execution_status: str
    world_artifact_load_mode: str
    mission_upload_observed: bool
    mission_ack_observed: bool
    heartbeat_observed: bool
    flight_telemetry_observed: bool
    payload_release_status: Literal["pending", "observed"] = "pending"
    dropoff_verification_status: Literal["pending", "verified"] = "pending"
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    operator_checklist: tuple[str, ...] = ()
    observed_facts_only: Literal[True] = True
    simulation_only: Literal[True] = True
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    generated_at: datetime
    package_hash: str
    sha256: str

    @field_validator(
        "blocked_reasons",
        "warning_reasons",
        "operator_checklist",
        mode="before",
    )
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value or ())

    @field_validator("source_backed_inputs_summary", mode="before")
    @classmethod
    def _coerce_summary(cls, value: Any) -> dict[str, Any]:
        return dict(value or {})

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_package(self) -> "FlightReadinessPackage":
        required_prefixes = {
            "execution_result_ref": "digital_twin_sitl_execution_result:",
            "real_world_target_resolution_ref": "real_world_target_resolution:",
            "terrain_dem_source_snapshot_ref": "terrain_dem_source_snapshot:",
            "weather_source_snapshot_ref": "weather_source_snapshot:",
            "vehicle_flight_envelope_ref": "vehicle_flight_envelope:",
            "mission_energy_budget_ref": "mission_energy_budget:",
            "gazebo_world_artifact_ref": "gazebo_world_artifact:",
            "digital_twin_px4_mission_item_candidate_ref": (
                "digital_twin_px4_mission_item_candidate:"
            ),
            "digital_twin_sitl_process_run_ref": "digital_twin_sitl_process_run:",
            "digital_twin_sitl_mission_upload_receipt_ref": (
                "digital_twin_sitl_mission_upload_receipt:"
            ),
        }
        for field_name, prefix in required_prefixes.items():
            if not str(getattr(self, field_name)).startswith(prefix):
                raise FlightReadinessPackageError(
                    "flight readiness package requires source and execution refs"
                )
        if self.package_hash != self.sha256:
            raise FlightReadinessPackageError("flight readiness package hash mismatch")
        if self.payload_release_status == "pending" and (
            "payload_release_pending" not in self.warning_reasons
        ):
            raise FlightReadinessPackageError(
                "flight readiness package must mark pending payload evidence"
            )
        if self.dropoff_verification_status == "pending" and (
            "dropoff_verification_pending" not in self.warning_reasons
        ):
            raise FlightReadinessPackageError(
                "flight readiness package must mark pending dropoff evidence"
            )
        if self.readiness_status == "ready_for_human_hardware_review":
            if self.blocked_reasons:
                raise FlightReadinessPackageError(
                    "ready flight readiness package cannot include blockers"
                )
            if not (
                self.mission_upload_observed
                and self.mission_ack_observed
                and self.heartbeat_observed
                and self.flight_telemetry_observed
            ):
                raise FlightReadinessPackageError(
                    "ready flight readiness package requires observed SITL facts"
                )
            required_true = (
                "source_backed_target",
                "source_backed_terrain",
                "source_backed_weather",
                "vehicle_envelope_present",
                "mission_energy_budget_present",
            )
            missing = [
                key
                for key in required_true
                if self.source_backed_inputs_summary.get(key) is not True
            ]
            if missing:
                raise FlightReadinessPackageError(
                    "ready flight readiness package requires source-backed evidence"
                )
        elif not self.blocked_reasons:
            raise FlightReadinessPackageError(
                "blocked flight readiness package requires blocked reasons"
            )
        return self


def flight_readiness_package_ref(package: FlightReadinessPackage) -> str:
    return f"flight_readiness_package:{package.package_id}"


def _package_from_fields(
    *,
    execution_result_ref: str,
    real_world_target_resolution_ref: str,
    terrain_dem_source_snapshot_ref: str,
    weather_source_snapshot_ref: str,
    vehicle_flight_envelope_ref: str,
    mission_energy_budget_ref: str,
    gazebo_world_artifact_ref: str,
    digital_twin_px4_mission_item_candidate_ref: str,
    digital_twin_sitl_process_run_ref: str,
    digital_twin_sitl_mission_upload_receipt_ref: str,
    source_backed_inputs_summary: Mapping[str, Any],
    execution_status: str,
    world_artifact_load_mode: str,
    mission_upload_observed: bool,
    mission_ack_observed: bool,
    heartbeat_observed: bool,
    flight_telemetry_observed: bool,
    payload_release_observed: bool,
    dropoff_verified: bool,
    blocked_reasons: Sequence[str],
    now: datetime | None = None,
) -> FlightReadinessPackage:
    warnings = [
        "payload_release_pending" if not payload_release_observed else "",
        "dropoff_verification_pending" if not dropoff_verified else "",
    ]
    if world_artifact_load_mode == "terrain_injection_into_default_world":
        warnings.append("terrain_injection_mode_not_direct_world_load")
    blocked = list(blocked_reasons)
    success_statuses = {
        "terrain_injected_world_upload_ack_telemetry_observed",
        "world_bound_direct_load_upload_ack_telemetry_observed",
    }
    if execution_status not in success_statuses:
        blocked.append("execution_result_not_successful")
    if not source_backed_inputs_summary:
        blocked.append("source_backed_inputs_missing")
    if not mission_upload_observed:
        blocked.append("mission_upload_not_observed")
    if not mission_ack_observed:
        blocked.append("mission_ack_not_observed")
    if not heartbeat_observed:
        blocked.append("heartbeat_not_observed")
    if not flight_telemetry_observed:
        blocked.append("flight_telemetry_not_observed")
    readiness_status: Literal["ready_for_human_hardware_review", "blocked"] = (
        "blocked" if blocked else "ready_for_human_hardware_review"
    )
    checklist = (
        "review_source_backed_target_dem_weather_vehicle_refs",
        "review_energy_budget_and_vehicle_envelope",
        "review_world_artifact_and_terrain_injection_mode",
        "review_candidate_derived_mission_items",
        "confirm_mission_ack_and_heartbeat_observed",
        "review_payload_release_pending",
        "review_dropoff_verification_pending",
        "confirm_no_hardware_or_physical_execution_authority",
    )
    generated_at = _utc(now)
    payload = {
        "schema_version": FLIGHT_READINESS_PACKAGE_SCHEMA_VERSION,
        "execution_result_ref": execution_result_ref,
        "real_world_target_resolution_ref": real_world_target_resolution_ref,
        "terrain_dem_source_snapshot_ref": terrain_dem_source_snapshot_ref,
        "weather_source_snapshot_ref": weather_source_snapshot_ref,
        "vehicle_flight_envelope_ref": vehicle_flight_envelope_ref,
        "mission_energy_budget_ref": mission_energy_budget_ref,
        "gazebo_world_artifact_ref": gazebo_world_artifact_ref,
        "digital_twin_px4_mission_item_candidate_ref": (
            digital_twin_px4_mission_item_candidate_ref
        ),
        "digital_twin_sitl_process_run_ref": digital_twin_sitl_process_run_ref,
        "digital_twin_sitl_mission_upload_receipt_ref": (
            digital_twin_sitl_mission_upload_receipt_ref
        ),
        "source_backed_inputs_summary": dict(source_backed_inputs_summary),
        "readiness_status": readiness_status,
        "execution_status": execution_status,
        "world_artifact_load_mode": world_artifact_load_mode,
        "mission_upload_observed": mission_upload_observed,
        "mission_ack_observed": mission_ack_observed,
        "heartbeat_observed": heartbeat_observed,
        "flight_telemetry_observed": flight_telemetry_observed,
        "payload_release_status": "observed" if payload_release_observed else "pending",
        "dropoff_verification_status": "verified" if dropoff_verified else "pending",
        "blocked_reasons": tuple(sorted(set(item for item in blocked if item))),
        "warning_reasons": tuple(item for item in warnings if item),
        "operator_checklist": checklist,
        "observed_facts_only": True,
        "simulation_only": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "approval_free_stronger_execution_allowed": False,
        "generated_at": generated_at.isoformat(),
    }
    digest = _content_hash(payload)
    return FlightReadinessPackage(
        package_id="flight_readiness_package_" + digest[:12],
        package_hash=digest,
        sha256=digest,
        **payload,
    )


def build_flight_readiness_package(
    *,
    execution_result: DigitalTwinSITLExecutionResult | Mapping[str, Any],
    now: datetime | None = None,
) -> FlightReadinessPackage:
    result = (
        execution_result
        if isinstance(execution_result, DigitalTwinSITLExecutionResult)
        else DigitalTwinSITLExecutionResult.model_validate(execution_result)
    )
    summary = result.source_backed_inputs_summary
    return _package_from_fields(
        execution_result_ref=digital_twin_sitl_execution_result_ref(result),
        real_world_target_resolution_ref=str(
            summary.get("real_world_target_resolution_ref", "")
        ),
        terrain_dem_source_snapshot_ref=str(
            summary.get("terrain_dem_source_snapshot_ref", "")
        ),
        weather_source_snapshot_ref=str(summary.get("weather_source_snapshot_ref", "")),
        vehicle_flight_envelope_ref=str(
            summary.get("vehicle_flight_envelope_ref", "")
        ),
        mission_energy_budget_ref=str(summary.get("mission_energy_budget_ref", "")),
        gazebo_world_artifact_ref=result.gazebo_world_artifact_ref,
        digital_twin_px4_mission_item_candidate_ref=(
            result.digital_twin_px4_mission_item_candidate_ref
        ),
        digital_twin_sitl_process_run_ref=result.digital_twin_sitl_process_run_ref,
        digital_twin_sitl_mission_upload_receipt_ref=(
            result.digital_twin_sitl_mission_upload_receipt_ref
        ),
        source_backed_inputs_summary=summary,
        execution_status=result.execution_status,
        world_artifact_load_mode=result.world_artifact_load_mode,
        mission_upload_observed=result.mission_upload_observed,
        mission_ack_observed=result.mission_ack_observed,
        heartbeat_observed=result.heartbeat_observed,
        flight_telemetry_observed=result.flight_telemetry_observed,
        payload_release_observed=result.payload_release_observed,
        dropoff_verified=result.dropoff_verified,
        blocked_reasons=result.blocked_reasons,
        now=now,
    )


def build_flight_readiness_package_from_source_backed_e2e_summary(
    summary: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> FlightReadinessPackage:
    source_summary = dict(summary.get("source_backed_inputs_summary") or {})
    return _package_from_fields(
        execution_result_ref=str(summary.get("execution_result_ref", "")),
        real_world_target_resolution_ref=str(
            source_summary.get("real_world_target_resolution_ref", "")
        ),
        terrain_dem_source_snapshot_ref=str(
            source_summary.get("terrain_dem_source_snapshot_ref", "")
        ),
        weather_source_snapshot_ref=str(
            source_summary.get("weather_source_snapshot_ref", "")
        ),
        vehicle_flight_envelope_ref=str(
            source_summary.get("vehicle_flight_envelope_ref", "")
        ),
        mission_energy_budget_ref=str(source_summary.get("mission_energy_budget_ref", "")),
        gazebo_world_artifact_ref=str(summary.get("gazebo_world_artifact_ref", "")),
        digital_twin_px4_mission_item_candidate_ref=str(
            summary.get("digital_twin_px4_mission_item_candidate_ref", "")
        ),
        digital_twin_sitl_process_run_ref=str(summary.get("process_run_ref", "")),
        digital_twin_sitl_mission_upload_receipt_ref=str(
            summary.get("mission_upload_receipt_ref", "")
        ),
        source_backed_inputs_summary=source_summary,
        execution_status=str(summary.get("execution_status", "")),
        world_artifact_load_mode=str(summary.get("world_artifact_load_mode", "")),
        mission_upload_observed=bool(summary.get("mission_upload_observed")),
        mission_ack_observed=bool(summary.get("mission_ack_observed")),
        heartbeat_observed=bool(summary.get("heartbeat_observed")),
        flight_telemetry_observed=bool(summary.get("flight_telemetry_observed")),
        payload_release_observed=bool(summary.get("payload_release_observed")),
        dropoff_verified=bool(summary.get("dropoff_verified")),
        blocked_reasons=tuple(str(item) for item in summary.get("blocked_reasons", ())),
        now=now,
    )


__all__ = [
    "FLIGHT_READINESS_PACKAGE_SCHEMA_VERSION",
    "FlightReadinessPackage",
    "FlightReadinessPackageError",
    "build_flight_readiness_package",
    "build_flight_readiness_package_from_source_backed_e2e_summary",
    "flight_readiness_package_ref",
]
