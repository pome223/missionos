"""Nvblox perception evidence for obstacle-aware MissionOS claims.

Nvblox can provide perception evidence such as depth input, pose input, scene
reconstruction, and a Nav2 costmap update. This module deliberately keeps that
evidence separate from approval, dispatch, execution, motion observation, and
obstacle-avoidance completion claims.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


NVBLOX_PERCEPTION_EVIDENCE_SCHEMA = "missionos_nvblox_perception_evidence.v1"
NVBLOX_PERCEPTION_EVIDENCE_JSON_ENV = "MISSIONOS_NVBLOX_PERCEPTION_EVIDENCE_JSON"
NVBLOX_PERCEPTION_EVIDENCE_REQUIRED_ENV = (
    "MISSIONOS_NVBLOX_PERCEPTION_EVIDENCE_REQUIRED"
)
NVBLOX_PERCEPTION_SOURCE = "isaac_ros_nvblox"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_DIRECT_EVIDENCE_KEYS = {
    "perception_source",
    "depth_input_observed",
    "pose_input_observed",
    "scene_reconstruction_observed",
    "nav2_costmap_updated_from_perception",
    "dynamic_obstacle_observed",
}
_NESTED_EVIDENCE_KEYS = (
    "nvblox_perception_evidence",
    "nvblox_evidence",
    "perception_evidence",
)
NVBLOX_DOES_NOT_CLAIM_OBSTACLE_AVOIDANCE = (
    "Nvblox perception evidence alone cannot claim obstacle avoidance; "
    "MissionOS requires trajectory/verifier evidence that the observed motion "
    "cleared the obstacle."
)


class NvbloxPerceptionEvidenceError(ValueError):
    """Raised when Nvblox perception evidence cannot be parsed."""


class NvbloxPerceptionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[NVBLOX_PERCEPTION_EVIDENCE_SCHEMA] = (
        NVBLOX_PERCEPTION_EVIDENCE_SCHEMA
    )
    evidence_status: Literal[
        "not_requested",
        "not_configured",
        "available",
        "unavailable",
    ]
    perception_evidence_available: bool = False
    perception_source: str = NVBLOX_PERCEPTION_SOURCE
    depth_input_observed: bool = False
    pose_input_observed: bool = False
    scene_reconstruction_observed: bool = False
    nav2_costmap_updated_from_perception: bool = False
    dynamic_obstacle_observed: bool = False
    perception_artifact_refs: tuple[str, ...] = Field(default_factory=tuple)
    limitations: tuple[str, ...] = Field(default_factory=tuple)
    blocking_reasons: tuple[str, ...] = Field(default_factory=tuple)
    supports_obstacle_aware_claim_when_paired_with_trajectory_evidence: bool = False
    claim_boundary: str = NVBLOX_DOES_NOT_CLAIM_OBSTACLE_AVOIDANCE
    approval_authority_created: Literal[False] = False
    dispatch_authority_created: Literal[False] = False
    completion_claimed: Literal[False] = False
    obstacle_avoidance_completion_claimed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    mission_delivery_completion_claimed: Literal[False] = False
    progress_counted: Literal[False] = False
    evidence_source_ref: str | None = None
    raw_payload_snapshot: dict[str, Any] = Field(default_factory=dict)


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUE_VALUES


def nvblox_perception_evidence_required_from_env(
    environ: Mapping[str, str] | None = None,
) -> bool:
    values = environ or os.environ
    return _truthy(values.get(NVBLOX_PERCEPTION_EVIDENCE_REQUIRED_ENV))


def _bool_value(payload: Mapping[str, Any], key: str) -> bool:
    return payload.get(key) is True


def _artifact_refs(payload: Mapping[str, Any]) -> tuple[str, ...]:
    refs: list[str] = []
    raw_refs = payload.get("perception_artifact_refs")
    if isinstance(raw_refs, (list, tuple)):
        refs.extend(str(item) for item in raw_refs if str(item))
    for key in (
        "nvblox_artifact_ref",
        "mesh_artifact_ref",
        "costmap_artifact_ref",
        "raw_logs_ref",
    ):
        value = payload.get(key)
        if value:
            refs.append(str(value))
    return tuple(dict.fromkeys(refs))


def _limitations(payload: Mapping[str, Any]) -> tuple[str, ...]:
    limitations: list[str] = []
    raw_limitations = payload.get("limitations")
    if isinstance(raw_limitations, (list, tuple)):
        limitations.extend(str(item) for item in raw_limitations if str(item))
    if not limitations:
        limitations.append(NVBLOX_DOES_NOT_CLAIM_OBSTACLE_AVOIDANCE)
    return tuple(dict.fromkeys(limitations))


def _blocking_reasons_for_payload(payload: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    if str(payload.get("perception_source") or NVBLOX_PERCEPTION_SOURCE) != (
        NVBLOX_PERCEPTION_SOURCE
    ):
        reasons.append("nvblox_perception_source_unexpected")
    if not _bool_value(payload, "depth_input_observed"):
        reasons.append("nvblox_depth_input_not_observed")
    if not _bool_value(payload, "pose_input_observed"):
        reasons.append("nvblox_pose_input_not_observed")
    if not _bool_value(payload, "scene_reconstruction_observed"):
        reasons.append("nvblox_scene_reconstruction_not_observed")
    if not _bool_value(payload, "nav2_costmap_updated_from_perception"):
        reasons.append("nvblox_nav2_costmap_update_not_observed")
    raw_reasons = payload.get("blocking_reasons")
    if isinstance(raw_reasons, (list, tuple)):
        reasons.extend(str(item) for item in raw_reasons if str(item))
    return list(dict.fromkeys(reasons))


def build_nvblox_perception_evidence(
    payload: Mapping[str, Any] | NvbloxPerceptionEvidence | None = None,
    *,
    required: bool = False,
    evidence_source_ref: str | None = None,
) -> NvbloxPerceptionEvidence:
    """Build fail-closed perception evidence without creating authority claims."""

    if isinstance(payload, NvbloxPerceptionEvidence):
        return payload
    if payload is None:
        if required:
            return NvbloxPerceptionEvidence(
                evidence_status="not_configured",
                blocking_reasons=("nvblox_perception_evidence_not_configured",),
                limitations=(NVBLOX_DOES_NOT_CLAIM_OBSTACLE_AVOIDANCE,),
                evidence_source_ref=evidence_source_ref,
            )
        return NvbloxPerceptionEvidence(
            evidence_status="not_requested",
            limitations=(NVBLOX_DOES_NOT_CLAIM_OBSTACLE_AVOIDANCE,),
            evidence_source_ref=evidence_source_ref,
        )

    normalized = dict(payload)
    reasons = _blocking_reasons_for_payload(normalized)
    available = not reasons
    return NvbloxPerceptionEvidence(
        evidence_status="available" if available else "unavailable",
        perception_evidence_available=available,
        perception_source=str(
            normalized.get("perception_source") or NVBLOX_PERCEPTION_SOURCE
        ),
        depth_input_observed=_bool_value(normalized, "depth_input_observed"),
        pose_input_observed=_bool_value(normalized, "pose_input_observed"),
        scene_reconstruction_observed=_bool_value(
            normalized, "scene_reconstruction_observed"
        ),
        nav2_costmap_updated_from_perception=_bool_value(
            normalized, "nav2_costmap_updated_from_perception"
        ),
        dynamic_obstacle_observed=_bool_value(
            normalized, "dynamic_obstacle_observed"
        ),
        perception_artifact_refs=_artifact_refs(normalized),
        limitations=_limitations(normalized),
        blocking_reasons=tuple(reasons),
        supports_obstacle_aware_claim_when_paired_with_trajectory_evidence=(
            available
            and _bool_value(normalized, "nav2_costmap_updated_from_perception")
        ),
        evidence_source_ref=evidence_source_ref,
        raw_payload_snapshot=normalized,
    )


def extract_nvblox_perception_payload_from_responses(
    responses: tuple[Mapping[str, Any], ...],
) -> dict[str, Any] | None:
    """Return the first Nvblox-like payload from bridge response surfaces."""

    for response in responses:
        containers: list[Mapping[str, Any]] = [response]
        for key in ("state_result", "progress_result"):
            value = response.get(key)
            if isinstance(value, Mapping):
                containers.append(value)
        for container in containers:
            for nested_key in _NESTED_EVIDENCE_KEYS:
                nested = container.get(nested_key)
                if isinstance(nested, Mapping):
                    return dict(nested)
            if any(key in container for key in _DIRECT_EVIDENCE_KEYS):
                return {
                    key: container.get(key)
                    for key in _DIRECT_EVIDENCE_KEYS
                    if key in container
                }
    return None


def load_nvblox_perception_payload_from_env(
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any] | None, str | None, tuple[str, ...]]:
    values = environ or os.environ
    path_value = str(values.get(NVBLOX_PERCEPTION_EVIDENCE_JSON_ENV) or "").strip()
    if not path_value:
        return None, None, ()
    path = Path(path_value)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return (
            None,
            f"{NVBLOX_PERCEPTION_EVIDENCE_JSON_ENV}:{path}",
            ("nvblox_perception_evidence_json_unreadable", str(exc)),
        )
    if not isinstance(payload, Mapping):
        return (
            None,
            f"{NVBLOX_PERCEPTION_EVIDENCE_JSON_ENV}:{path}",
            ("nvblox_perception_evidence_json_not_object",),
        )
    return dict(payload), f"{NVBLOX_PERCEPTION_EVIDENCE_JSON_ENV}:{path}", ()


def build_nvblox_perception_evidence_from_env_or_responses(
    responses: tuple[Mapping[str, Any], ...],
    *,
    environ: Mapping[str, str] | None = None,
) -> NvbloxPerceptionEvidence:
    values = environ or os.environ
    required = nvblox_perception_evidence_required_from_env(values)
    response_payload = extract_nvblox_perception_payload_from_responses(responses)
    if response_payload is not None:
        return build_nvblox_perception_evidence(
            response_payload,
            required=required,
            evidence_source_ref="ros2_nav2_bridge_response",
        )

    env_payload, env_ref, load_reasons = load_nvblox_perception_payload_from_env(values)
    if load_reasons:
        return NvbloxPerceptionEvidence(
            evidence_status="unavailable",
            blocking_reasons=tuple(load_reasons),
            limitations=(NVBLOX_DOES_NOT_CLAIM_OBSTACLE_AVOIDANCE,),
            evidence_source_ref=env_ref,
        )
    return build_nvblox_perception_evidence(
        env_payload,
        required=required,
        evidence_source_ref=env_ref,
    )


__all__ = [
    "NVBLOX_DOES_NOT_CLAIM_OBSTACLE_AVOIDANCE",
    "NVBLOX_PERCEPTION_EVIDENCE_JSON_ENV",
    "NVBLOX_PERCEPTION_EVIDENCE_REQUIRED_ENV",
    "NVBLOX_PERCEPTION_EVIDENCE_SCHEMA",
    "NVBLOX_PERCEPTION_SOURCE",
    "NvbloxPerceptionEvidence",
    "NvbloxPerceptionEvidenceError",
    "build_nvblox_perception_evidence",
    "build_nvblox_perception_evidence_from_env_or_responses",
    "extract_nvblox_perception_payload_from_responses",
    "load_nvblox_perception_payload_from_env",
    "nvblox_perception_evidence_required_from_env",
]
