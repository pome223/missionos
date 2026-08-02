"""Project a bound perception claim into Mission Contract runtime evidence.

The semantic claim remains model-inferred. Camera/LiDAR corroboration binds
the claim to sensor evidence, but it does not make the semantic object identity
machine-observed or deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Mapping

from missionos_core import (
    EvidenceOrigin,
    HardwareExecutionMode,
    MissionObservation,
    canonical_sha256,
    mission_observation_source_receipt_binding_sha256,
    parse_hardware_execution_mode,
)
from pydantic import ValidationError

from src.runtime.perception_claim import PerceptionClaim


PERCEPTION_MISSION_OBSERVATION_EVIDENCE_KIND = (
    "model_inferred_perception_claim"
)


@dataclass(frozen=True)
class PerceptionMissionObservationProjection:
    status: Literal["projected", "refused"]
    observation: MissionObservation | None
    reasons: tuple[str, ...] = ()
    approval_created: Literal[False] = False
    dispatch_authority_created: Literal[False] = False
    runtime_effect_requested: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    completion_claimed: Literal[False] = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "observation": (
                self.observation.to_material()
                if self.observation is not None
                else None
            ),
            "reasons": list(self.reasons),
            "approval_created": self.approval_created,
            "dispatch_authority_created": self.dispatch_authority_created,
            "runtime_effect_requested": self.runtime_effect_requested,
            "physical_execution_invoked": self.physical_execution_invoked,
            "completion_claimed": self.completion_claimed,
        }


def project_perception_claim_to_mission_observation(
    *,
    claim: PerceptionClaim | Mapping[str, Any],
    requirement_id: str,
    execution_scope: HardwareExecutionMode | str,
    source_clock_domain_ref: str,
    receipt_clock_domain_ref: str,
) -> PerceptionMissionObservationProjection:
    """Preserve source and receipt time without inventing a clock mapping."""

    reasons: list[str] = []
    if not isinstance(claim, PerceptionClaim):
        try:
            claim = PerceptionClaim.model_validate(claim)
        except (TypeError, ValidationError, ValueError):
            return PerceptionMissionObservationProjection(
                status="refused",
                observation=None,
                reasons=("perception_mission_observation_claim_invalid",),
            )

    normalized_scope = parse_hardware_execution_mode(execution_scope)
    if normalized_scope is None:
        reasons.append("perception_mission_observation_scope_invalid")
    if not str(requirement_id or "").strip():
        reasons.append("perception_mission_observation_requirement_missing")
    if not str(source_clock_domain_ref or "").strip():
        reasons.append(
            "perception_mission_observation_source_clock_domain_missing"
        )
    if not str(receipt_clock_domain_ref or "").strip():
        reasons.append(
            "perception_mission_observation_receipt_clock_domain_missing"
        )

    binding = claim.corroboration_binding
    if binding is None:
        reasons.append("perception_mission_observation_binding_missing")
    else:
        if binding.blocking_reasons:
            reasons.append("perception_mission_observation_binding_blocked")
        if binding.temporal_status != "bound":
            reasons.append(
                "perception_mission_observation_source_temporal_unbound"
            )
        if binding.vlm_temporal_status != "bound":
            reasons.append(
                "perception_mission_observation_receipt_temporal_unbound"
            )
        if binding.source_frame_ref != claim.source_frame_ref:
            reasons.append(
                "perception_mission_observation_source_frame_mismatch"
            )
        try:
            binding_source_time = datetime.fromisoformat(
                binding.camera_observed_at.replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except ValueError:
            binding_source_time = None
        if binding_source_time is None or (
            claim.observed_at.astimezone(timezone.utc) != binding_source_time
        ):
            reasons.append(
                "perception_mission_observation_source_time_mismatch"
            )

    if reasons or binding is None or normalized_scope is None:
        return PerceptionMissionObservationProjection(
            status="refused",
            observation=None,
            reasons=tuple(dict.fromkeys(reasons)),
        )

    claim_material = claim.model_dump(mode="json")
    observation_id = f"perception-claim:{claim.claim_id}"
    content_sha256 = canonical_sha256(claim_material)
    observation = MissionObservation(
        observation_id=observation_id,
        requirement_id=requirement_id,
        evidence_kind=PERCEPTION_MISSION_OBSERVATION_EVIDENCE_KIND,
        origin=EvidenceOrigin.MODEL_INFERRED,
        observed_at=binding.camera_observed_at,
        source_clock_domain_ref=source_clock_domain_ref,
        received_at=binding.camera_received_at,
        receipt_clock_domain_ref=receipt_clock_domain_ref,
        source_receipt_binding_sha256=(
            mission_observation_source_receipt_binding_sha256(
                observation_id=observation_id,
                content_sha256=content_sha256,
                observed_at=binding.camera_observed_at,
                source_clock_domain_ref=source_clock_domain_ref,
                received_at=binding.camera_received_at,
                receipt_clock_domain_ref=receipt_clock_domain_ref,
            )
        ),
        content_sha256=content_sha256,
        execution_scope=normalized_scope,
    )
    return PerceptionMissionObservationProjection(
        status="projected",
        observation=observation,
    )


__all__ = [
    "PERCEPTION_MISSION_OBSERVATION_EVIDENCE_KIND",
    "PerceptionMissionObservationProjection",
    "project_perception_claim_to_mission_observation",
]
