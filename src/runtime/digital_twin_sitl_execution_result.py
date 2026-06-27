"""Observed Digital Twin world-bound SITL execution result."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.digital_twin_mission_environment import (
    CoordinateTransformCandidate,
    DigitalTwinPx4MissionItemCandidate,
    DigitalTwinSITLBindingGate,
    GazeboWorldArtifact,
    coordinate_transform_candidate_ref,
    digital_twin_px4_mission_item_candidate_ref,
    digital_twin_sitl_binding_gate_ref,
    gazebo_world_artifact_ref,
)
from src.runtime.digital_twin_sitl_mavlink_upload import (
    DigitalTwinSITLMissionUploadReceipt,
    digital_twin_sitl_mission_upload_receipt_ref,
)
from src.runtime.digital_twin_sitl_process_runner import (
    DigitalTwinSITLProcessRun,
    digital_twin_sitl_process_run_ref,
)


DIGITAL_TWIN_SITL_EXECUTION_RESULT_SCHEMA_VERSION = (
    "digital_twin_sitl_execution_result.v1"
)


class DigitalTwinSITLExecutionResultError(RuntimeError):
    """Raised when Digital Twin SITL execution evidence is inconsistent."""


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


class DigitalTwinSITLExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DIGITAL_TWIN_SITL_EXECUTION_RESULT_SCHEMA_VERSION] = (
        DIGITAL_TWIN_SITL_EXECUTION_RESULT_SCHEMA_VERSION
    )
    execution_result_id: str
    gazebo_world_artifact_ref: str
    coordinate_transform_candidate_ref: str
    digital_twin_px4_mission_item_candidate_ref: str
    digital_twin_sitl_binding_gate_ref: str
    digital_twin_sitl_process_run_ref: str
    digital_twin_sitl_mission_upload_receipt_ref: str
    same_run_binding_ref: str
    execution_status: Literal[
        "world_bound_direct_load_upload_ack_telemetry_observed",
        "terrain_injected_world_upload_ack_telemetry_observed",
        "blocked",
    ]
    world_artifact_load_mode: Literal[
        "direct_world_artifact_load",
        "terrain_injection_into_default_world",
    ]
    px4_loaded_world_file_path: str
    world_bound: bool
    terrain_artifact_used: bool
    mission_upload_observed: bool
    mission_ack_observed: bool
    mission_ack_type: int | None = None
    flight_telemetry_observed: bool
    heartbeat_observed: bool
    payload_release_observed: Literal[False] = False
    dropoff_verified: Literal[False] = False
    observed_facts_only: Literal[True] = True
    simulation_only: Literal[True] = True
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    source_backed_inputs_summary: dict[str, Any] = Field(default_factory=dict)
    blocked_reasons: tuple[str, ...] = ()
    observed_at: datetime
    execution_result_hash: str
    sha256: str

    @field_validator("blocked_reasons", mode="before")
    @classmethod
    def _coerce_blocked_reasons(cls, value: Any) -> tuple[str, ...]:
        return tuple(str(item) for item in value or ())

    @field_validator("source_backed_inputs_summary", mode="before")
    @classmethod
    def _coerce_source_backed_summary(cls, value: Any) -> dict[str, Any]:
        return dict(value or {})

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_execution_result(self) -> "DigitalTwinSITLExecutionResult":
        if not self.gazebo_world_artifact_ref.startswith("gazebo_world_artifact:"):
            raise DigitalTwinSITLExecutionResultError(
                "Digital Twin SITL execution result requires world artifact ref"
            )
        if not self.coordinate_transform_candidate_ref.startswith(
            "coordinate_transform_candidate:"
        ):
            raise DigitalTwinSITLExecutionResultError(
                "Digital Twin SITL execution result requires transform ref"
            )
        if not self.digital_twin_px4_mission_item_candidate_ref.startswith(
            "digital_twin_px4_mission_item_candidate:"
        ):
            raise DigitalTwinSITLExecutionResultError(
                "Digital Twin SITL execution result requires mission item candidate ref"
            )
        if not self.digital_twin_sitl_binding_gate_ref.startswith(
            "digital_twin_sitl_binding_gate:"
        ):
            raise DigitalTwinSITLExecutionResultError(
                "Digital Twin SITL execution result requires binding gate ref"
            )
        if not self.digital_twin_sitl_process_run_ref.startswith(
            "digital_twin_sitl_process_run:"
        ):
            raise DigitalTwinSITLExecutionResultError(
                "Digital Twin SITL execution result requires process run ref"
            )
        if not self.digital_twin_sitl_mission_upload_receipt_ref.startswith(
            "digital_twin_sitl_mission_upload_receipt:"
        ):
            raise DigitalTwinSITLExecutionResultError(
                "Digital Twin SITL execution result requires upload receipt ref"
            )
        if self.same_run_binding_ref != self.digital_twin_sitl_binding_gate_ref:
            raise DigitalTwinSITLExecutionResultError(
                "Digital Twin SITL execution result binding ref mismatch"
            )
        if self.execution_status in {
            "world_bound_direct_load_upload_ack_telemetry_observed",
            "terrain_injected_world_upload_ack_telemetry_observed",
        }:
            if self.execution_status == "world_bound_direct_load_upload_ack_telemetry_observed":
                if (
                    self.world_artifact_load_mode != "direct_world_artifact_load"
                    or not self.world_bound
                ):
                    raise DigitalTwinSITLExecutionResultError(
                        "direct Digital Twin SITL result requires direct world binding"
                    )
            if self.execution_status == "terrain_injected_world_upload_ack_telemetry_observed":
                if (
                    self.world_artifact_load_mode
                    != "terrain_injection_into_default_world"
                    or self.world_bound
                    or not self.terrain_artifact_used
                ):
                    raise DigitalTwinSITLExecutionResultError(
                        "terrain-injected Digital Twin SITL result requires explicit terrain injection"
                    )
            if not (
                self.terrain_artifact_used
                and self.mission_upload_observed
                and self.mission_ack_observed
                and self.flight_telemetry_observed
                and self.heartbeat_observed
            ):
                raise DigitalTwinSITLExecutionResultError(
                    "successful Digital Twin SITL result requires observed facts"
                )
            if self.blocked_reasons:
                raise DigitalTwinSITLExecutionResultError(
                    "successful Digital Twin SITL result cannot include blocked reasons"
                )
        else:
            if not self.blocked_reasons:
                raise DigitalTwinSITLExecutionResultError(
                    "blocked Digital Twin SITL result requires blocked reasons"
                )
        if self.execution_result_hash != self.sha256:
            raise DigitalTwinSITLExecutionResultError(
                "Digital Twin SITL execution result hash mismatch"
            )
        if self.source_backed_inputs_summary:
            summary = self.source_backed_inputs_summary
            required_true = (
                "source_backed_target",
                "source_backed_terrain",
                "source_backed_weather",
                "vehicle_envelope_present",
                "mission_energy_budget_present",
            )
            missing = [key for key in required_true if summary.get(key) is not True]
            if missing:
                raise DigitalTwinSITLExecutionResultError(
                    "source-backed Digital Twin SITL result requires source-backed inputs"
                )
            required_refs = {
                "real_world_target_resolution_ref": "real_world_target_resolution:",
                "terrain_dem_source_snapshot_ref": "terrain_dem_source_snapshot:",
                "weather_source_snapshot_ref": "weather_source_snapshot:",
                "vehicle_flight_envelope_ref": "vehicle_flight_envelope:",
                "mission_energy_budget_ref": "mission_energy_budget:",
            }
            for key, prefix in required_refs.items():
                if not str(summary.get(key, "")).startswith(prefix):
                    raise DigitalTwinSITLExecutionResultError(
                        "source-backed Digital Twin SITL result requires source refs"
                    )
        return self


def digital_twin_sitl_execution_result_ref(
    result: DigitalTwinSITLExecutionResult,
) -> str:
    return f"digital_twin_sitl_execution_result:{result.execution_result_id}"


def build_digital_twin_sitl_execution_result(
    *,
    gazebo_world_artifact: GazeboWorldArtifact | Mapping[str, Any],
    coordinate_transform_candidate: CoordinateTransformCandidate | Mapping[str, Any],
    px4_mission_item_candidate: DigitalTwinPx4MissionItemCandidate | Mapping[str, Any],
    sitl_binding_gate: DigitalTwinSITLBindingGate | Mapping[str, Any],
    sitl_process_run: DigitalTwinSITLProcessRun | Mapping[str, Any],
    mission_upload_receipt: DigitalTwinSITLMissionUploadReceipt | Mapping[str, Any],
    source_backed_inputs_summary: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> DigitalTwinSITLExecutionResult:
    world = (
        gazebo_world_artifact
        if isinstance(gazebo_world_artifact, GazeboWorldArtifact)
        else GazeboWorldArtifact.model_validate(gazebo_world_artifact)
    )
    transform = (
        coordinate_transform_candidate
        if isinstance(coordinate_transform_candidate, CoordinateTransformCandidate)
        else CoordinateTransformCandidate.model_validate(coordinate_transform_candidate)
    )
    mission_item = (
        px4_mission_item_candidate
        if isinstance(px4_mission_item_candidate, DigitalTwinPx4MissionItemCandidate)
        else DigitalTwinPx4MissionItemCandidate.model_validate(px4_mission_item_candidate)
    )
    binding_gate = (
        sitl_binding_gate
        if isinstance(sitl_binding_gate, DigitalTwinSITLBindingGate)
        else DigitalTwinSITLBindingGate.model_validate(sitl_binding_gate)
    )
    process_run = (
        sitl_process_run
        if isinstance(sitl_process_run, DigitalTwinSITLProcessRun)
        else DigitalTwinSITLProcessRun.model_validate(sitl_process_run)
    )
    upload_receipt = (
        mission_upload_receipt
        if isinstance(mission_upload_receipt, DigitalTwinSITLMissionUploadReceipt)
        else DigitalTwinSITLMissionUploadReceipt.model_validate(mission_upload_receipt)
    )
    world_ref = gazebo_world_artifact_ref(world)
    transform_ref = coordinate_transform_candidate_ref(transform)
    mission_item_ref = digital_twin_px4_mission_item_candidate_ref(mission_item)
    binding_ref = digital_twin_sitl_binding_gate_ref(binding_gate)
    process_ref = digital_twin_sitl_process_run_ref(process_run)
    receipt_ref = digital_twin_sitl_mission_upload_receipt_ref(upload_receipt)
    blocked_reasons: list[str] = []
    if transform.gazebo_world_artifact_ref != world_ref:
        blocked_reasons.append("transform_world_ref_mismatch")
    if mission_item.coordinate_transform_candidate_ref != transform_ref:
        blocked_reasons.append("mission_item_transform_ref_mismatch")
    if binding_gate.gazebo_world_artifact_ref != world_ref:
        blocked_reasons.append("binding_gate_world_ref_mismatch")
    if binding_gate.digital_twin_px4_mission_item_candidate_ref != mission_item_ref:
        blocked_reasons.append("binding_gate_mission_item_ref_mismatch")
    if upload_receipt.digital_twin_px4_mission_item_candidate_ref != mission_item_ref:
        blocked_reasons.append("upload_receipt_mission_item_ref_mismatch")
    if upload_receipt.digital_twin_sitl_process_run_ref != process_ref:
        blocked_reasons.append("upload_receipt_process_run_ref_mismatch")
    if upload_receipt.same_run_binding_ref != binding_ref:
        blocked_reasons.append("upload_receipt_binding_ref_mismatch")
    if binding_gate.binding_gate_status != "eligible_for_operator_approved_sitl_binding":
        blocked_reasons.append("binding_gate_not_eligible")
    if not process_run.gazebo_execution_invoked:
        blocked_reasons.append("gazebo_execution_not_invoked")
    if not process_run.px4_process_invoked:
        blocked_reasons.append("px4_process_not_invoked")
    if process_run.startup_error_observed:
        blocked_reasons.append("process_startup_error_observed")
    if not upload_receipt.mission_upload_observed:
        blocked_reasons.append("mission_upload_not_observed")
    if not upload_receipt.mission_ack_observed:
        blocked_reasons.append("mission_ack_not_observed")
    if not upload_receipt.heartbeat_observed:
        blocked_reasons.append("heartbeat_not_observed")
    world_artifact_load_mode = process_run.world_artifact_load_mode
    world_bound = (
        process_run.gazebo_world_artifact_ref == world_ref
        and process_run.gazebo_execution_invoked
        and process_run.px4_process_invoked
        and world_artifact_load_mode == "direct_world_artifact_load"
    )
    terrain_artifact_used = (
        process_run.gazebo_world_artifact_ref == world_ref
        and process_run.gazebo_execution_invoked
        and process_run.px4_process_invoked
        and world_artifact_load_mode == "terrain_injection_into_default_world"
    )
    if not world_bound and not terrain_artifact_used:
        blocked_reasons.append("world_not_bound_to_px4_gazebo_process")
    reasons = tuple(sorted(set(blocked_reasons)))
    if reasons:
        execution_status = "blocked"
    elif world_bound:
        execution_status = "world_bound_direct_load_upload_ack_telemetry_observed"
    else:
        execution_status = "terrain_injected_world_upload_ack_telemetry_observed"
    observed_at = _utc(now)
    payload = {
        "schema_version": DIGITAL_TWIN_SITL_EXECUTION_RESULT_SCHEMA_VERSION,
        "gazebo_world_artifact_ref": world_ref,
        "coordinate_transform_candidate_ref": transform_ref,
        "digital_twin_px4_mission_item_candidate_ref": mission_item_ref,
        "digital_twin_sitl_binding_gate_ref": binding_ref,
        "digital_twin_sitl_process_run_ref": process_ref,
        "digital_twin_sitl_mission_upload_receipt_ref": receipt_ref,
        "same_run_binding_ref": binding_ref,
        "execution_status": execution_status,
        "world_artifact_load_mode": world_artifact_load_mode,
        "px4_loaded_world_file_path": process_run.px4_loaded_world_file_path,
        "world_bound": world_bound,
        "terrain_artifact_used": terrain_artifact_used,
        "mission_upload_observed": upload_receipt.mission_upload_observed,
        "mission_ack_observed": upload_receipt.mission_ack_observed,
        "mission_ack_type": upload_receipt.mission_ack_type,
        "flight_telemetry_observed": upload_receipt.telemetry_observed,
        "heartbeat_observed": upload_receipt.heartbeat_observed,
        "payload_release_observed": False,
        "dropoff_verified": False,
        "observed_facts_only": True,
        "simulation_only": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "approval_free_stronger_execution_allowed": False,
        "source_backed_inputs_summary": dict(source_backed_inputs_summary or {}),
        "blocked_reasons": reasons,
        "observed_at": observed_at.isoformat(),
    }
    digest = _content_hash(payload)
    return DigitalTwinSITLExecutionResult(
        execution_result_id="digital_twin_sitl_execution_result_" + digest[:12],
        gazebo_world_artifact_ref=world_ref,
        coordinate_transform_candidate_ref=transform_ref,
        digital_twin_px4_mission_item_candidate_ref=mission_item_ref,
        digital_twin_sitl_binding_gate_ref=binding_ref,
        digital_twin_sitl_process_run_ref=process_ref,
        digital_twin_sitl_mission_upload_receipt_ref=receipt_ref,
        same_run_binding_ref=binding_ref,
        execution_status=execution_status,  # type: ignore[arg-type]
        world_artifact_load_mode=world_artifact_load_mode,
        px4_loaded_world_file_path=process_run.px4_loaded_world_file_path,
        world_bound=world_bound,
        terrain_artifact_used=terrain_artifact_used,
        mission_upload_observed=upload_receipt.mission_upload_observed,
        mission_ack_observed=upload_receipt.mission_ack_observed,
        mission_ack_type=upload_receipt.mission_ack_type,
        flight_telemetry_observed=upload_receipt.telemetry_observed,
        heartbeat_observed=upload_receipt.heartbeat_observed,
        source_backed_inputs_summary=dict(source_backed_inputs_summary or {}),
        blocked_reasons=reasons,
        observed_at=observed_at,
        execution_result_hash=digest,
        sha256=digest,
    )


__all__ = [
    "DIGITAL_TWIN_SITL_EXECUTION_RESULT_SCHEMA_VERSION",
    "DigitalTwinSITLExecutionResult",
    "DigitalTwinSITLExecutionResultError",
    "build_digital_twin_sitl_execution_result",
    "digital_twin_sitl_execution_result_ref",
]
