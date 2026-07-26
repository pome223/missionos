"""Source-bound camera/LiDAR corroboration for perception claims.

This module evaluates evidence.  It does not approve, dispatch, execute, or
claim mission completion.  A positive verdict means only that one camera
claim may support a progressive recovery *proposal* because the exact image,
the live VLM invocation, and an independently observed LiDAR candidate were
bound to the same decision epoch.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from src.runtime.runtime_claim_evidence import (
    RuntimeClaimValidationError,
    validate_runtime_invocation_evidence,
)


PERCEPTION_CORROBORATION_BINDING_SCHEMA_VERSION = (
    "missionos_perception_corroboration_binding.v1"
)

BindingStatus = Literal["bound", "unbound", "mismatched", "stale", "unavailable"]
HorizontalSector = Literal["left", "center", "right", "unknown"]


class PerceptionCorroborationBinding(BaseModel):
    """Core-owned verdict over VLM and independent sensor evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        "missionos_perception_corroboration_binding.v1"
    ] = PERCEPTION_CORROBORATION_BINDING_SCHEMA_VERSION
    binding_id: str
    decision_epoch_ref: str
    source_frame_ref: str
    capture_image_sha256: str
    vlm_input_image_sha256: str
    vlm_invocation_ref: str
    runtime_invocation_evidence_valid: bool
    live_vlm_invocation_observed: bool
    camera_observed_at: str
    lidar_observed_at: str
    temporal_delta_ms: float | None = Field(default=None, ge=0.0)
    temporal_status: BindingStatus
    camera_received_at: str
    vlm_invocation_started_at: str
    vlm_capture_delta_ms: float | None = None
    vlm_temporal_status: BindingStatus
    camera_horizontal_sector: HorizontalSector
    lidar_horizontal_sector: HorizontalSector
    target_center_x_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    camera_candidate_bearing_rad: float | None = None
    lidar_candidate_bearing_rad: float | None = None
    angular_delta_rad: float | None = Field(default=None, ge=0.0)
    spatial_status: BindingStatus
    target_candidate_id: str
    target_identity_status: BindingStatus
    target_identity_method: Literal[
        "camera_intrinsics_lidar_angular_candidate",
        "unavailable",
    ]
    evidence_refs: tuple[str, ...] = ()
    conservative_action_supported: Literal[True] = True
    progressive_action_supported: bool = False
    blocking_reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = (
        "geometric_candidate_binding_does_not_verify_semantic_object_identity",
        "perception_binding_does_not_create_approval_or_dispatch_authority",
    )
    evidence_only: Literal[True] = True
    approval_created: Literal[False] = False
    dispatch_authority_created: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    completion_claimed: Literal[False] = False


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _iso8601(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _temporal_binding(
    camera_observed_at: Any,
    lidar_observed_at: Any,
    *,
    max_delta_ms: float,
) -> tuple[BindingStatus, float | None, tuple[str, ...]]:
    camera_time = _iso8601(camera_observed_at)
    lidar_time = _iso8601(lidar_observed_at)
    if camera_time is None or lidar_time is None:
        return "unavailable", None, ("perception_binding_timestamp_missing",)
    delta_ms = abs((lidar_time - camera_time).total_seconds()) * 1000.0
    if delta_ms > max_delta_ms:
        return "stale", delta_ms, ("perception_binding_timestamp_stale",)
    return "bound", delta_ms, ()


def _vlm_capture_temporal_binding(
    camera_received_at: Any,
    invocation_started_at: Any,
    *,
    max_delta_ms: float,
) -> tuple[BindingStatus, float | None, tuple[str, ...]]:
    capture_time = _iso8601(camera_received_at)
    invocation_time = _iso8601(invocation_started_at)
    if capture_time is None or invocation_time is None:
        return "unavailable", None, ("perception_binding_vlm_timestamp_missing",)
    delta_ms = (invocation_time - capture_time).total_seconds() * 1000.0
    if delta_ms < -1000.0:
        return "mismatched", delta_ms, (
            "perception_binding_vlm_invocation_precedes_capture",
        )
    if delta_ms > max_delta_ms:
        return "stale", delta_ms, ("perception_binding_vlm_invocation_stale",)
    return "bound", delta_ms, ()


def build_perception_corroboration_binding(
    *,
    source_frame_ref: str,
    claim_kind: str,
    camera_horizontal_sector: str,
    target_center_x_normalized: float | None,
    runtime_context: Mapping[str, Any] | None,
    max_temporal_delta_ms: float = 750.0,
    max_vlm_capture_delta_ms: float = 120_000.0,
    max_angular_delta_rad: float = 0.20,
) -> PerceptionCorroborationBinding:
    """Bind one exact VLM frame to one camera/LiDAR observation window.

    ``runtime_context`` is produced by MissionOS after the sidecar returns.  A
    raw VLM response cannot populate it, so a model cannot self-corroborate.
    """

    context = dict(runtime_context or {})
    capture = dict(context.get("capture") or {})
    sensor = dict(capture.get("camera_lidar_observation") or {})
    invocation = dict(context.get("llm_invocation_evidence") or {})
    decision_epoch_ref = str(context.get("decision_epoch_ref") or "").strip()
    capture_hash = str(capture.get("camera_frame_sha256") or "").strip()
    invocation_hash = str(invocation.get("input_image_sha256") or "").strip()
    expected_hash = source_frame_ref.removeprefix("sha256:")
    invocation_ref = str(invocation.get("invocation_ref") or "").strip()

    reasons: list[str] = []
    invocation_valid = False
    if not invocation:
        reasons.append("perception_binding_runtime_invocation_evidence_missing")
    else:
        try:
            validate_runtime_invocation_evidence(invocation)
            invocation_valid = True
        except RuntimeClaimValidationError as exc:
            reasons.append(f"perception_binding_runtime_invocation_evidence_invalid:{exc}")
    live_vlm_invocation = bool(
        invocation_valid
        and invocation.get("invocation_kind") == "llm_api"
        and (
            invocation.get("provider") in {"google_adk", "google_adk_gemini"}
            or str(invocation.get("provider") or "").startswith(
                "google_adk_litellm_"
            )
        )
        and str(invocation.get("invocation_target") or "").startswith("google_adk:")
        and str(invocation.get("model_id") or "").strip()
        and invocation.get("invocation_exit_code") == 0
        and invocation.get("physical_execution_invoked") is False
    )
    if not live_vlm_invocation:
        reasons.append("perception_binding_live_vlm_invocation_required")
    if not decision_epoch_ref:
        reasons.append("perception_binding_decision_epoch_missing")
    if not capture_hash or capture_hash != expected_hash:
        reasons.append("perception_binding_capture_image_hash_mismatch")
    if not invocation_hash or invocation_hash != expected_hash:
        reasons.append("perception_binding_vlm_input_image_hash_mismatch")
    if not invocation_ref:
        reasons.append("perception_binding_vlm_invocation_ref_missing")

    temporal_status, temporal_delta_ms, temporal_reasons = _temporal_binding(
        sensor.get("camera_observed_at"),
        sensor.get("lidar_observed_at"),
        max_delta_ms=max_temporal_delta_ms,
    )
    reasons.extend(temporal_reasons)
    vlm_temporal_status, vlm_capture_delta_ms, vlm_temporal_reasons = (
        _vlm_capture_temporal_binding(
            sensor.get("camera_received_at"),
            invocation.get("invocation_started_at"),
            max_delta_ms=max_vlm_capture_delta_ms,
        )
    )
    reasons.extend(vlm_temporal_reasons)

    normalized_camera_sector = (
        camera_horizontal_sector
        if camera_horizontal_sector in {"left", "center", "right"}
        else "unknown"
    )
    raw_lidar_sector = str(sensor.get("lidar_horizontal_sector") or "unknown")
    normalized_lidar_sector = (
        raw_lidar_sector
        if raw_lidar_sector in {"left", "center", "right"}
        else "unknown"
    )
    lidar_obstacle_observed = sensor.get("lidar_obstacle_observed") is True
    camera_width = sensor.get("camera_width")
    camera_fx = sensor.get("camera_fx")
    camera_cx = sensor.get("camera_cx")
    lidar_bearing = sensor.get("lidar_candidate_bearing_rad")
    try:
        normalized_target_center = (
            float(target_center_x_normalized)
            if target_center_x_normalized is not None
            else None
        )
        camera_bearing = (
            math.atan2(
                float(camera_cx)
                - normalized_target_center * (float(camera_width) - 1.0),
                float(camera_fx),
            )
            if normalized_target_center is not None
            and 0.0 <= normalized_target_center <= 1.0
            and float(camera_width) > 1.0
            and float(camera_fx) > 0.0
            else None
        )
        normalized_lidar_bearing = (
            float(lidar_bearing) if lidar_bearing is not None else None
        )
    except (TypeError, ValueError):
        normalized_target_center = None
        camera_bearing = None
        normalized_lidar_bearing = None
    angular_delta = (
        abs(camera_bearing - normalized_lidar_bearing)
        if camera_bearing is not None and normalized_lidar_bearing is not None
        else None
    )
    if claim_kind == "path_clear":
        spatial_status = "unbound"
        reasons.append("perception_binding_progressive_path_clear_unsupported")
    elif not lidar_obstacle_observed or normalized_lidar_sector == "unknown":
        spatial_status: BindingStatus = "unavailable"
        reasons.append("perception_binding_lidar_candidate_missing")
    elif normalized_camera_sector == "unknown" or camera_bearing is None:
        spatial_status = "unbound"
        reasons.append("perception_binding_camera_target_geometry_unbound")
    elif angular_delta is None or angular_delta > max_angular_delta_rad:
        spatial_status = "mismatched"
        reasons.append("perception_binding_camera_lidar_angle_mismatch")
    elif normalized_camera_sector != normalized_lidar_sector:
        spatial_status = "mismatched"
        reasons.append("perception_binding_camera_lidar_sector_mismatch")
    else:
        spatial_status = "bound"

    target_candidate_id = str(sensor.get("target_candidate_id") or "").strip()
    if spatial_status == "bound" and target_candidate_id:
        target_status: BindingStatus = "bound"
        target_method: Literal[
            "camera_intrinsics_lidar_angular_candidate", "unavailable"
        ] = "camera_intrinsics_lidar_angular_candidate"
    else:
        target_status = "unavailable" if not target_candidate_id else "unbound"
        target_method = "unavailable"
        reasons.append("perception_binding_target_candidate_unbound")

    evidence_refs = tuple(
        dict.fromkeys(
            str(value)
            for value in (
                source_frame_ref,
                invocation_ref,
                sensor.get("lidar_evidence_ref"),
                target_candidate_id,
            )
            if str(value or "").strip()
        )
    )
    progressive_supported = bool(
        live_vlm_invocation
        and capture_hash == expected_hash
        and invocation_hash == expected_hash
        and decision_epoch_ref
        and temporal_status == "bound"
        and vlm_temporal_status == "bound"
        and spatial_status == "bound"
        and target_status == "bound"
        and not reasons
    )
    payload = {
        "decision_epoch_ref": decision_epoch_ref,
        "source_frame_ref": source_frame_ref,
        "capture_image_sha256": capture_hash,
        "vlm_input_image_sha256": invocation_hash,
        "vlm_invocation_ref": invocation_ref,
        "camera_observed_at": str(sensor.get("camera_observed_at") or ""),
        "lidar_observed_at": str(sensor.get("lidar_observed_at") or ""),
        "camera_received_at": str(sensor.get("camera_received_at") or ""),
        "vlm_invocation_started_at": str(
            invocation.get("invocation_started_at") or ""
        ),
        "camera_horizontal_sector": normalized_camera_sector,
        "lidar_horizontal_sector": normalized_lidar_sector,
        "target_center_x_normalized": normalized_target_center,
        "camera_candidate_bearing_rad": camera_bearing,
        "lidar_candidate_bearing_rad": normalized_lidar_bearing,
        "angular_delta_rad": angular_delta,
        "target_candidate_id": target_candidate_id,
    }
    return PerceptionCorroborationBinding(
        binding_id=f"perception_binding:{_canonical_sha256(payload)[:16]}",
        runtime_invocation_evidence_valid=invocation_valid,
        live_vlm_invocation_observed=live_vlm_invocation,
        temporal_delta_ms=temporal_delta_ms,
        temporal_status=temporal_status,
        vlm_capture_delta_ms=vlm_capture_delta_ms,
        vlm_temporal_status=vlm_temporal_status,
        spatial_status=spatial_status,
        target_identity_status=target_status,
        target_identity_method=target_method,
        evidence_refs=evidence_refs,
        progressive_action_supported=progressive_supported,
        blocking_reasons=tuple(dict.fromkeys(reasons)),
        **payload,
    )


__all__ = [
    "PERCEPTION_CORROBORATION_BINDING_SCHEMA_VERSION",
    "PerceptionCorroborationBinding",
    "build_perception_corroboration_binding",
]
