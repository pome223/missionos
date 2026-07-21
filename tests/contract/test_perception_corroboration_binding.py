"""Contract tests for live camera/LiDAR corroboration binding (issue #82)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256

from src.runtime.perception_claim import (
    build_perception_claim_from_camera_observation,
    guard_perception_claim_support,
)
from src.runtime.perception_corroboration_binding import (
    build_perception_corroboration_binding,
)


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
IMAGE_SHA256 = "a" * 64
FRAME_REF = f"sha256:{IMAGE_SHA256}"


def _invocation_evidence(
    *,
    image_sha256: str = IMAGE_SHA256,
    invocation_kind: str = "llm_api",
    provider: str = "google_adk",
) -> dict[str, object]:
    stdout = '{"claim_kind":"corridor_blocked_by_object"}'
    stderr = ""
    return {
        "schema_version": "runtime_invocation_evidence.v1",
        "invocation_kind": invocation_kind,
        "invocation_target": "google_adk:gemini-test",
        "provider": provider,
        "model_id": "gemini-test",
        "input_image_sha256": image_sha256,
        "prompt_sha256": sha256(b"prompt").hexdigest(),
        "invocation_started_at": NOW.isoformat(),
        "invocation_completed_at": (NOW + timedelta(milliseconds=100)).isoformat(),
        "invocation_stdout_sha256": sha256(stdout.encode()).hexdigest(),
        "invocation_stderr_sha256": sha256(stderr.encode()).hexdigest(),
        "invocation_stdout_preimage": stdout,
        "invocation_stderr_preimage": stderr,
        "invocation_exit_code": 0,
        "invocation_ref": "vlm_invocation:test",
        "physical_execution_invoked": False,
    }


def _runtime_context(
    *,
    capture_hash: str = IMAGE_SHA256,
    invocation: dict[str, object] | None = None,
    lidar_observed_at: datetime | None = None,
    lidar_bearing_rad: float = 0.0,
    lidar_sector: str = "center",
    target_candidate_id: str = "lidar_candidate:test",
) -> dict[str, object]:
    return {
        "decision_epoch_ref": "proposal:test:segment:1:perception",
        "capture": {
            "camera_frame_sha256": capture_hash,
            "camera_lidar_observation": {
                "camera_observed_at": NOW.isoformat(),
                "camera_received_at": (NOW - timedelta(milliseconds=50)).isoformat(),
                "camera_frame_id": "camera_rgb_optical_frame",
                "camera_width": 640,
                "camera_fx": 554.25,
                "camera_cx": 320.0,
                "lidar_observed_at": (
                    lidar_observed_at or NOW + timedelta(milliseconds=100)
                ).isoformat(),
                "lidar_frame_id": "base_scan",
                "lidar_obstacle_observed": True,
                "lidar_horizontal_sector": lidar_sector,
                "lidar_candidate_bearing_rad": lidar_bearing_rad,
                "target_candidate_id": target_candidate_id,
                "lidar_evidence_ref": "laser_scan:test",
            },
        },
        "llm_invocation_evidence": invocation or _invocation_evidence(),
    }


def _binding(**context_overrides: object):
    context = _runtime_context(**context_overrides)
    return build_perception_corroboration_binding(
        source_frame_ref=FRAME_REF,
        claim_kind="corridor_blocked_by_object",
        camera_horizontal_sector="center",
        target_center_x_normalized=0.5,
        runtime_context=context,
    )


def test_live_same_frame_same_epoch_angular_candidate_supports_progressive() -> None:
    binding = _binding()

    assert binding.runtime_invocation_evidence_valid is True
    assert binding.live_vlm_invocation_observed is True
    assert binding.temporal_status == "bound"
    assert binding.spatial_status == "bound"
    assert binding.target_identity_status == "bound"
    assert binding.progressive_action_supported is True
    assert binding.blocking_reasons == ()
    assert binding.dispatch_authority_created is False
    assert binding.physical_execution_invoked is False


def test_hash_mismatch_blocks_progressive_but_keeps_conservative_support() -> None:
    binding = _binding(capture_hash="b" * 64)

    assert binding.progressive_action_supported is False
    assert "perception_binding_capture_image_hash_mismatch" in (
        binding.blocking_reasons
    )
    assert binding.conservative_action_supported is True


def test_stale_sensor_pair_blocks_progressive() -> None:
    binding = _binding(lidar_observed_at=NOW + timedelta(seconds=2))

    assert binding.temporal_status == "stale"
    assert binding.progressive_action_supported is False
    assert "perception_binding_timestamp_stale" in binding.blocking_reasons


def test_stale_vlm_invocation_after_capture_blocks_progressive() -> None:
    invocation = _invocation_evidence()
    invocation["invocation_started_at"] = (NOW + timedelta(minutes=3)).isoformat()
    invocation["invocation_completed_at"] = (
        NOW + timedelta(minutes=3, milliseconds=100)
    ).isoformat()

    binding = _binding(invocation=invocation)

    assert binding.vlm_temporal_status == "stale"
    assert binding.progressive_action_supported is False
    assert "perception_binding_vlm_invocation_stale" in binding.blocking_reasons


def test_angularly_different_lidar_candidate_blocks_progressive() -> None:
    binding = _binding(lidar_bearing_rad=0.45, lidar_sector="left")

    assert binding.spatial_status == "mismatched"
    assert binding.target_identity_status == "unbound"
    assert binding.progressive_action_supported is False
    assert "perception_binding_camera_lidar_angle_mismatch" in (
        binding.blocking_reasons
    )


def test_command_override_is_regression_evidence_not_live_vlm_evidence() -> None:
    invocation = _invocation_evidence(
        invocation_kind="subprocess",
        provider="command_override",
    )
    binding = _binding(invocation=invocation)

    assert binding.runtime_invocation_evidence_valid is True
    assert binding.live_vlm_invocation_observed is False
    assert binding.progressive_action_supported is False
    assert "perception_binding_live_vlm_invocation_required" in (
        binding.blocking_reasons
    )


def test_vlm_cannot_self_report_corroboration_or_authority() -> None:
    payload = {
        "claim_kind": "corridor_blocked_by_object",
        "source_frame_ref": FRAME_REF,
        "confidence": 0.9,
        "horizontal_sector": "center",
        "target_center_x_normalized": 0.5,
        "corroborated_by": ["lidar_costmap:fabricated"],
        "progressive_action_supported": True,
        "dispatch_authority_created": True,
    }
    claim = build_perception_claim_from_camera_observation(
        payload,
        costmap_obstacle_observed=False,
        runtime_context=_runtime_context(),
    )

    assert claim is not None
    assert claim.corroborated_by == (
        "range_sensor:ros2_laser_scan_angular_candidate",
    )
    assert "lidar_costmap:fabricated" not in claim.corroborated_by
    assert claim.corroboration_binding is not None
    assert claim.corroboration_binding.progressive_action_supported is True
    assert claim.dispatch_authority_created is False


def test_progressive_guard_requires_core_owned_binding_not_coarse_costmap_flag() -> None:
    unbound_claim = build_perception_claim_from_camera_observation(
        {
            "claim_kind": "corridor_blocked_by_object",
            "source_frame_ref": FRAME_REF,
            "confidence": 0.9,
        },
        costmap_obstacle_observed=True,
    )
    assert unbound_claim is not None
    assert unbound_claim.corroborated is True

    blocked = guard_perception_claim_support(
        selected_action="avoid_obstacle",
        cited_claim_ids=[unbound_claim.claim_id],
        perception_claims=[unbound_claim],
    )
    assert blocked["checks"]["perception_claim_support_respected"] is False

    bound_claim = build_perception_claim_from_camera_observation(
        {
            "claim_kind": "corridor_blocked_by_object",
            "source_frame_ref": FRAME_REF,
            "confidence": 0.9,
            "horizontal_sector": "center",
            "target_center_x_normalized": 0.5,
        },
        costmap_obstacle_observed=True,
        runtime_context=_runtime_context(),
    )
    assert bound_claim is not None
    allowed = guard_perception_claim_support(
        selected_action="avoid_obstacle",
        cited_claim_ids=[bound_claim.claim_id],
        perception_claims=[bound_claim],
    )
    assert allowed["checks"]["perception_claim_support_respected"] is True
