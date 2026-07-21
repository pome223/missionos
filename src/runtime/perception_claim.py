"""Perception claims: structured, source-hashed vision statements (issue #31).

A perception claim is what a vision model says about the environment, bound to
the exact frame it saw (sha256 ref) and to the sensors that corroborate it.
Claims are evidence for recovery deliberation, never commands. The support
rule is asymmetric: only a live VLM claim bound to the exact frame and a
spatiotemporally matching independent-sensor candidate may support a
progressive proposal. Any weaker camera claim may support conservative
(fail-safe) actions only — stopping on a false positive costs mission time,
proceeding on one costs the robot.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.runtime.delivery_recovery_safety import raise_for_command_like_payload
from src.runtime.mission_autonomy_envelope import CONSERVATIVE_RECOVERY_ACTIONS
from src.runtime.perception_corroboration_binding import (
    PerceptionCorroborationBinding,
    build_perception_corroboration_binding,
)

__all__ = [
    "CONSERVATIVE_RECOVERY_ACTIONS",
    "PERCEPTION_CLAIM_JSON_ENV",
    "PERCEPTION_CLAIM_SCHEMA_VERSION",
    "PerceptionClaim",
    "PerceptionClaimError",
    "PerceptionClaimKind",
    "build_perception_claim",
    "build_perception_claim_from_camera_observation",
    "build_perception_claims_from_env_or_responses",
    "extract_camera_observation_payloads_from_responses",
    "guard_perception_claim_support",
    "load_camera_observation_payloads_from_env",
]

PERCEPTION_CLAIM_SCHEMA_VERSION = "missionos_perception_claim.v1"

PerceptionClaimKind = Literal[
    "corridor_blocked_by_object",
    "path_clear",
    "landing_zone_obstructed",
    "unexpected_entity_detected",
    "floor_hazard_detected",
]

# Sensor namespaces that can corroborate a camera claim. Camera-derived
# sources cannot corroborate themselves and are rejected at build time.
CORROBORATING_SOURCE_PREFIXES = (
    "lidar_costmap:",
    "depth_sensor:",
    "range_sensor:",
    "bumper_event:",
    "odom:",
)
_CAMERA_SOURCE_PREFIXES = ("camera:", "image:", "vlm:")

_SOURCE_FRAME_REF_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class PerceptionClaimError(ValueError):
    """Raised when a perception claim violates its evidence-only contract."""


class PerceptionClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PERCEPTION_CLAIM_SCHEMA_VERSION] = (
        PERCEPTION_CLAIM_SCHEMA_VERSION
    )
    claim_id: str
    claim_kind: PerceptionClaimKind
    source_frame_ref: str
    confidence: float = Field(ge=0.0, le=1.0)
    corroborated_by: tuple[str, ...] = ()
    corroboration_binding: PerceptionCorroborationBinding | None = None
    observed_at: datetime
    evidence_only: Literal[True] = True
    approval_created: Literal[False] = False
    dispatch_authority_created: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_frame_ref")
    @classmethod
    def _validate_source_frame_ref(cls, value: str) -> str:
        if not _SOURCE_FRAME_REF_PATTERN.fullmatch(value):
            raise ValueError(
                "source_frame_ref must be a sha256:<hex64> hash of the frame"
            )
        return value

    @field_validator("corroborated_by")
    @classmethod
    def _validate_corroborating_sources(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        for ref in value:
            if ref.startswith(_CAMERA_SOURCE_PREFIXES):
                raise ValueError(
                    f"camera-derived source cannot corroborate a claim: {ref}"
                )
            if not ref.startswith(CORROBORATING_SOURCE_PREFIXES):
                raise ValueError(
                    f"unrecognized corroborating source namespace: {ref}"
                )
        return value

    @property
    def corroborated(self) -> bool:
        return bool(self.corroborated_by)

    @property
    def progressive_support_eligible(self) -> bool:
        return bool(
            self.corroboration_binding is not None
            and self.corroboration_binding.progressive_action_supported
        )


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stable_claim_id(payload: Mapping[str, Any]) -> str:
    digest = sha256(
        json.dumps(dict(payload), sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"perception_claim_{digest[:16]}"


def build_perception_claim(
    *,
    claim_kind: PerceptionClaimKind | str,
    source_frame_ref: str,
    confidence: float,
    corroborated_by: Sequence[str] | None = None,
    corroboration_binding: PerceptionCorroborationBinding | None = None,
    observed_at: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> PerceptionClaim:
    metadata_payload = dict(metadata or {})
    raise_for_command_like_payload(
        metadata_payload,
        root="metadata",
        error_type=PerceptionClaimError,
        prefix="perception claim refused command-like metadata",
    )
    observed = _utc(observed_at)
    corroboration = tuple(dict.fromkeys(corroborated_by or ()))
    claim_id = _stable_claim_id(
        {
            "claim_kind": claim_kind,
            "source_frame_ref": source_frame_ref,
            "confidence": confidence,
            "corroborated_by": corroboration,
            "corroboration_binding_id": (
                corroboration_binding.binding_id if corroboration_binding else ""
            ),
            "observed_at": observed.isoformat(),
        }
    )
    return PerceptionClaim(
        claim_id=claim_id,
        claim_kind=claim_kind,  # type: ignore[arg-type]
        source_frame_ref=source_frame_ref,
        confidence=confidence,
        corroborated_by=corroboration,
        corroboration_binding=corroboration_binding,
        observed_at=observed,
        metadata=metadata_payload,
    )


def guard_perception_claim_support(
    *,
    selected_action: str,
    cited_claim_ids: Sequence[str],
    perception_claims: Sequence[PerceptionClaim | Mapping[str, Any]],
) -> dict[str, Any]:
    """Check whether the provided claims permit the selected action.

    Returns a guardrail fragment; it never rewrites the action. The support
    rule is enforced on every claim PROVIDED in the planner's context, not
    only on the claims the model chose to cite — review found the earlier
    citation-gated check could be bypassed by simply omitting the citation
    while still having seen the claim. Since reliance cannot be verified,
    the rule fails closed: a progressive action is blocked while any claim
    without a core-owned live spatiotemporal binding exists in context.
    Conservative actions pass regardless. Citations remain required as an
    explicit reliance record (checked by the planner guard), and unknown
    cited ids still block.
    """

    claims_by_id: dict[str, PerceptionClaim] = {}
    for claim in perception_claims:
        model = (
            claim
            if isinstance(claim, PerceptionClaim)
            else PerceptionClaim.model_validate(dict(claim))
        )
        claims_by_id[model.claim_id] = model

    blocking_reasons: list[str] = []
    unknown_ids = [cid for cid in cited_claim_ids if cid not in claims_by_id]
    for cid in unknown_ids:
        blocking_reasons.append(f"cited_perception_claim_unknown:{cid}")

    action_is_conservative = selected_action in CONSERVATIVE_RECOVERY_ACTIONS
    progressive_support_missing = [
        claim_id
        for claim_id, model in claims_by_id.items()
        if not model.progressive_support_eligible
    ]
    if not action_is_conservative:
        for cid in progressive_support_missing:
            blocking_reasons.append(
                "perception_claim_without_bound_live_corroboration_requires_"
                f"conservative_action:{cid}"
            )

    return {
        "checks": {
            "cited_perception_claims_known": not unknown_ids,
            "perception_claim_support_respected": (
                action_is_conservative or not progressive_support_missing
            ),
        },
        "selected_action_is_conservative": action_is_conservative,
        "uncorroborated_claim_ids_in_context": progressive_support_missing,
        "progressive_support_missing_claim_ids_in_context": (
            progressive_support_missing
        ),
        "blocking_reasons": blocking_reasons,
    }


# --- Camera observation ingestion (issue #31, gym wiring) --------------------
#
# A VLM sidecar analyzing Gazebo camera frames reports raw observations
# through the same channels the Nvblox perception evidence pipeline uses:
# nested in a ROS2/Nav2 bridge response, or as an env-pointed JSON file.
# Corroboration is never taken from the raw payload — a compromised or
# hallucinating sidecar could just self-report a costmap match. MissionOS
# computes corroborated_by itself from the independently observed Nav2
# costmap/Nvblox obstacle signal already flowing through the same bridge
# response, which is why callers must pass it in explicitly.

PERCEPTION_CLAIM_JSON_ENV = "MISSIONOS_PERCEPTION_CLAIM_JSON"

_NESTED_CAMERA_OBSERVATION_KEYS = (
    "perception_claims",
    "camera_observations",
    "camera_observation",
    "vlm_observation",
)

_OBSTACLE_CLAIM_KINDS = frozenset(
    {
        "corridor_blocked_by_object",
        "landing_zone_obstructed",
        "unexpected_entity_detected",
        "floor_hazard_detected",
    }
)


def _as_payload_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        return [dict(value)]
    if isinstance(value, (list, tuple)):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    return []


def extract_camera_observation_payloads_from_responses(
    responses: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Collect raw camera/VLM observation payloads from bridge responses."""

    payloads: list[dict[str, Any]] = []
    for response in responses:
        containers: list[Mapping[str, Any]] = [response]
        for key in ("state_result", "progress_result"):
            value = response.get(key)
            if isinstance(value, Mapping):
                containers.append(value)
        for container in containers:
            for nested_key in _NESTED_CAMERA_OBSERVATION_KEYS:
                payloads.extend(_as_payload_list(container.get(nested_key)))
    return payloads


def load_camera_observation_payloads_from_env(
    environ: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], str | None, tuple[str, ...]]:
    values = environ or os.environ
    path_value = str(values.get(PERCEPTION_CLAIM_JSON_ENV) or "").strip()
    if not path_value:
        return [], None, ()
    path = Path(path_value)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return (
            [],
            f"{PERCEPTION_CLAIM_JSON_ENV}:{path}",
            ("perception_claim_json_unreadable", str(exc)),
        )
    return _as_payload_list(raw), f"{PERCEPTION_CLAIM_JSON_ENV}:{path}", ()


def build_perception_claim_from_camera_observation(
    payload: Mapping[str, Any],
    *,
    costmap_obstacle_observed: bool,
    observed_at: datetime | None = None,
    runtime_context: Mapping[str, Any] | None = None,
) -> PerceptionClaim | None:
    """Build a claim from a raw camera payload, or None if it is malformed.

    corroborated_by is computed here, not read from the payload: only an
    independently observed non-camera signal (the caller-supplied
    costmap_obstacle_observed) can corroborate an obstacle-kind claim.
    """

    claim_kind = payload.get("claim_kind")
    source_frame_ref = payload.get("source_frame_ref")
    confidence = payload.get("confidence")
    if not claim_kind or not source_frame_ref or confidence is None:
        return None
    corroborated_by = (
        ("lidar_costmap:nav2_costmap_obstacle_observed",)
        if costmap_obstacle_observed and claim_kind in _OBSTACLE_CLAIM_KINDS
        else ()
    )
    binding = (
        build_perception_corroboration_binding(
            source_frame_ref=str(source_frame_ref),
            claim_kind=str(claim_kind),
            camera_horizontal_sector=str(
                payload.get("horizontal_sector") or "unknown"
            ),
            target_center_x_normalized=payload.get(
                "target_center_x_normalized"
            ),
            runtime_context=runtime_context,
        )
        if runtime_context is not None
        else None
    )
    if (
        binding is not None
        and binding.temporal_status == "bound"
        and binding.spatial_status == "bound"
        and binding.target_identity_status == "bound"
    ):
        corroborated_by = tuple(
            dict.fromkeys(
                (
                    *corroborated_by,
                    "range_sensor:ros2_laser_scan_angular_candidate",
                )
            )
        )
    claim_observed_at = observed_at
    if binding is not None and binding.camera_observed_at:
        try:
            claim_observed_at = datetime.fromisoformat(
                binding.camera_observed_at.replace("Z", "+00:00")
            )
        except ValueError:
            pass
    # The corroboration is honest about its own coarseness: the costmap
    # signal comes from the same segment's bridge receipt (same mission
    # moment, local costmap around the same robot pose) but is NOT bound to
    # the claim's specific region or target — a different nearby obstacle
    # could satisfy it. Downstream consumers must not read corroborated as
    # "the same object was cross-confirmed" until spatial/target binding
    # exists.
    metadata = (
        {
            "legacy_costmap_corroboration": {
                "temporal": "same_segment_bridge_receipt",
                "spatial": "unbound",
                "target_identity": "unbound",
            }
        }
        if costmap_obstacle_observed and claim_kind in _OBSTACLE_CLAIM_KINDS
        else {}
    )
    try:
        return build_perception_claim(
            claim_kind=claim_kind,
            source_frame_ref=str(source_frame_ref),
            confidence=float(confidence),
            corroborated_by=corroborated_by,
            corroboration_binding=binding,
            observed_at=claim_observed_at,
            metadata=metadata,
        )
    except (ValueError, TypeError):
        return None


def build_perception_claims_from_env_or_responses(
    responses: Sequence[Mapping[str, Any]],
    *,
    costmap_obstacle_observed: bool,
    environ: Mapping[str, str] | None = None,
    observed_at: datetime | None = None,
    runtime_context: Mapping[str, Any] | None = None,
) -> tuple[PerceptionClaim, ...]:
    """Build whatever well-formed camera-derived claims are available.

    Missing or malformed camera evidence yields an empty tuple; this is
    optional evidence, not a required gate, so callers proceed without it.
    """

    payloads = extract_camera_observation_payloads_from_responses(responses)
    if not payloads:
        env_payloads, _ref, load_reasons = load_camera_observation_payloads_from_env(
            environ
        )
        if not load_reasons:
            payloads = env_payloads
    claims = [
        claim
        for claim in (
            build_perception_claim_from_camera_observation(
                payload,
                costmap_obstacle_observed=costmap_obstacle_observed,
                observed_at=observed_at,
                runtime_context=runtime_context,
            )
            for payload in payloads
        )
        if claim is not None
    ]
    return tuple(claims)
