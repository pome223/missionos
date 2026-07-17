"""Contract tests for camera observation ingestion (issue #31, gym wiring).

Corroboration must never come from the raw camera/VLM payload itself — a
compromised or hallucinating sidecar could just self-report a costmap match.
These tests pin that MissionOS computes corroborated_by from the
independently observed costmap signal the caller supplies, not from
anything the payload claims.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json

from src.runtime.perception_claim import (
    PERCEPTION_CLAIM_JSON_ENV,
    build_perception_claim_from_camera_observation,
    build_perception_claims_from_env_or_responses,
    extract_camera_observation_payloads_from_responses,
    load_camera_observation_payloads_from_env,
)

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
FRAME_REF = "sha256:" + "b" * 64


def test_extracts_nested_camera_observation_from_bridge_response() -> None:
    responses = (
        {
            "ack_status": "accepted",
            "state_result": {
                "camera_observation": {
                    "claim_kind": "corridor_blocked_by_object",
                    "source_frame_ref": FRAME_REF,
                    "confidence": 0.75,
                },
            },
        },
    )
    payloads = extract_camera_observation_payloads_from_responses(responses)
    assert payloads == [
        {
            "claim_kind": "corridor_blocked_by_object",
            "source_frame_ref": FRAME_REF,
            "confidence": 0.75,
        }
    ]


def test_extracts_list_of_perception_claims_key() -> None:
    responses = (
        {
            "perception_claims": [
                {
                    "claim_kind": "path_clear",
                    "source_frame_ref": FRAME_REF,
                    "confidence": 0.9,
                },
            ],
        },
    )
    payloads = extract_camera_observation_payloads_from_responses(responses)
    assert len(payloads) == 1
    assert payloads[0]["claim_kind"] == "path_clear"


def test_build_from_camera_observation_ignores_payload_corroboration_claim() -> None:
    payload = {
        "claim_kind": "corridor_blocked_by_object",
        "source_frame_ref": FRAME_REF,
        "confidence": 0.8,
        # A sidecar cannot self-corroborate by just listing a source here.
        "corroborated_by": ["lidar_costmap:fabricated"],
    }
    claim = build_perception_claim_from_camera_observation(
        payload,
        costmap_obstacle_observed=False,
        observed_at=NOW,
    )
    assert claim is not None
    assert claim.corroborated_by == ()
    assert claim.corroborated is False


def test_build_from_camera_observation_corroborates_from_independent_costmap() -> None:
    payload = {
        "claim_kind": "corridor_blocked_by_object",
        "source_frame_ref": FRAME_REF,
        "confidence": 0.8,
    }
    claim = build_perception_claim_from_camera_observation(
        payload,
        costmap_obstacle_observed=True,
        observed_at=NOW,
    )
    assert claim is not None
    assert claim.corroborated_by == ("lidar_costmap:nav2_costmap_obstacle_observed",)
    assert claim.corroborated is True


def test_costmap_agreement_does_not_corroborate_non_obstacle_claim_kind() -> None:
    payload = {
        "claim_kind": "path_clear",
        "source_frame_ref": FRAME_REF,
        "confidence": 0.8,
    }
    claim = build_perception_claim_from_camera_observation(
        payload,
        costmap_obstacle_observed=True,
        observed_at=NOW,
    )
    assert claim is not None
    assert claim.corroborated is False


def test_malformed_payload_is_skipped_not_raised() -> None:
    assert (
        build_perception_claim_from_camera_observation(
            {"claim_kind": "corridor_blocked_by_object"},
            costmap_obstacle_observed=True,
            observed_at=NOW,
        )
        is None
    )
    assert (
        build_perception_claim_from_camera_observation(
            {
                "claim_kind": "corridor_blocked_by_object",
                "source_frame_ref": "not-a-hash",
                "confidence": 0.8,
            },
            costmap_obstacle_observed=True,
            observed_at=NOW,
        )
        is None
    )


def test_build_claims_from_responses_skips_malformed_and_keeps_well_formed() -> None:
    responses = (
        {
            "camera_observation": {
                "claim_kind": "corridor_blocked_by_object",
                "source_frame_ref": FRAME_REF,
                "confidence": 0.8,
            },
        },
        {
            "camera_observation": {"claim_kind": "path_clear"},
        },
    )
    claims = build_perception_claims_from_env_or_responses(
        responses,
        costmap_obstacle_observed=True,
        observed_at=NOW,
    )
    assert len(claims) == 1
    assert claims[0].claim_kind == "corridor_blocked_by_object"
    assert claims[0].corroborated is True


def test_no_camera_observation_yields_no_claims() -> None:
    claims = build_perception_claims_from_env_or_responses(
        ({"ack_status": "accepted"},),
        costmap_obstacle_observed=True,
        observed_at=NOW,
        environ={},
    )
    assert claims == ()


def test_env_fallback_loads_camera_observations_when_responses_empty(
    tmp_path,
) -> None:
    path = tmp_path / "camera_observation.json"
    path.write_text(
        json.dumps(
            [
                {
                    "claim_kind": "corridor_blocked_by_object",
                    "source_frame_ref": FRAME_REF,
                    "confidence": 0.6,
                }
            ]
        ),
        encoding="utf-8",
    )
    claims = build_perception_claims_from_env_or_responses(
        (),
        costmap_obstacle_observed=False,
        environ={PERCEPTION_CLAIM_JSON_ENV: str(path)},
        observed_at=NOW,
    )
    assert len(claims) == 1
    assert claims[0].confidence == 0.6


def test_env_load_reports_unreadable_json(tmp_path) -> None:
    payloads, ref, reasons = load_camera_observation_payloads_from_env(
        {PERCEPTION_CLAIM_JSON_ENV: str(tmp_path / "missing.json")}
    )
    assert payloads == []
    assert ref is not None
    assert "perception_claim_json_unreadable" in reasons


def test_corroboration_carries_honest_binding_metadata() -> None:
    """Corroborated claims must state what the corroboration does and does
    not bind: same-segment temporal window, but no spatial/target identity
    binding — a different nearby obstacle could satisfy the costmap signal."""

    claim = build_perception_claim_from_camera_observation(
        {
            "claim_kind": "corridor_blocked_by_object",
            "source_frame_ref": FRAME_REF,
            "confidence": 0.8,
        },
        costmap_obstacle_observed=True,
        observed_at=NOW,
    )
    assert claim is not None
    binding = claim.metadata["corroboration_binding"]
    assert binding["temporal"] == "same_segment_bridge_receipt"
    assert binding["spatial"] == "unbound"
    assert binding["target_identity"] == "unbound"

    uncorroborated = build_perception_claim_from_camera_observation(
        {
            "claim_kind": "corridor_blocked_by_object",
            "source_frame_ref": FRAME_REF,
            "confidence": 0.8,
        },
        costmap_obstacle_observed=False,
        observed_at=NOW,
    )
    assert uncorroborated is not None
    assert "corroboration_binding" not in uncorroborated.metadata
