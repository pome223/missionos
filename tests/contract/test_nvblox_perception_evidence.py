from __future__ import annotations

import json
from pathlib import Path

from src.runtime.nvblox_perception_evidence import (
    NVBLOX_PERCEPTION_EVIDENCE_JSON_ENV,
    NVBLOX_PERCEPTION_EVIDENCE_REQUIRED_ENV,
    NVBLOX_PERCEPTION_EVIDENCE_SCHEMA,
    build_nvblox_perception_evidence,
    build_nvblox_perception_evidence_from_env_or_responses,
    extract_nvblox_perception_payload_from_responses,
)


FIXTURE_DIR = (
    Path(__file__).resolve().parents[2] / "examples" / "fixture_missions" / "nvblox"
)


def test_nvblox_perception_evidence_required_without_payload_is_not_configured() -> None:
    evidence = build_nvblox_perception_evidence(required=True)

    payload = evidence.model_dump(mode="json")
    assert payload["schema_version"] == NVBLOX_PERCEPTION_EVIDENCE_SCHEMA
    assert payload["evidence_status"] == "not_configured"
    assert payload["perception_evidence_available"] is False
    assert "nvblox_perception_evidence_not_configured" in payload["blocking_reasons"]
    assert payload["approval_authority_created"] is False
    assert payload["dispatch_authority_created"] is False
    assert payload["completion_claimed"] is False
    assert payload["obstacle_avoidance_completion_claimed"] is False
    assert payload["physical_execution_invoked"] is False
    assert payload["mission_delivery_completion_claimed"] is False
    assert payload["progress_counted"] is False


def test_nvblox_perception_evidence_available_still_does_not_claim_avoidance() -> None:
    evidence = build_nvblox_perception_evidence(
        {
            "perception_source": "isaac_ros_nvblox",
            "depth_input_observed": True,
            "pose_input_observed": True,
            "scene_reconstruction_observed": True,
            "nav2_costmap_updated_from_perception": True,
            "dynamic_obstacle_observed": True,
            "perception_artifact_refs": ["output/nvblox/costmap.json"],
        }
    )

    payload = evidence.model_dump(mode="json")
    assert payload["evidence_status"] == "available"
    assert payload["perception_evidence_available"] is True
    assert payload["nav2_costmap_updated_from_perception"] is True
    assert (
        payload["supports_obstacle_aware_claim_when_paired_with_trajectory_evidence"]
        is True
    )
    assert payload["obstacle_avoidance_completion_claimed"] is False
    assert payload["completion_claimed"] is False
    assert "cannot claim obstacle avoidance" in payload["claim_boundary"]


def test_nvblox_perception_evidence_from_env_json(tmp_path) -> None:
    evidence_path = tmp_path / "nvblox.json"
    evidence_path.write_text(
        json.dumps(
            {
                "perception_source": "isaac_ros_nvblox",
                "depth_input_observed": True,
                "pose_input_observed": True,
                "scene_reconstruction_observed": True,
                "nav2_costmap_updated_from_perception": True,
                "dynamic_obstacle_observed": False,
                "perception_artifact_refs": ["output/nvblox/mesh.usd"],
            }
        ),
        encoding="utf-8",
    )

    evidence = build_nvblox_perception_evidence_from_env_or_responses(
        (),
        environ={
            NVBLOX_PERCEPTION_EVIDENCE_REQUIRED_ENV: "1",
            NVBLOX_PERCEPTION_EVIDENCE_JSON_ENV: str(evidence_path),
        },
    )

    payload = evidence.model_dump(mode="json")
    assert payload["evidence_status"] == "available"
    assert payload["evidence_source_ref"].endswith(str(evidence_path))
    assert payload["perception_artifact_refs"] == ["output/nvblox/mesh.usd"]


def test_extract_nvblox_perception_payload_from_bridge_response() -> None:
    payload = extract_nvblox_perception_payload_from_responses(
        (
            {
                "state_result": {
                    "nvblox_perception_evidence": {
                        "perception_source": "isaac_ros_nvblox",
                        "depth_input_observed": True,
                        "pose_input_observed": True,
                        "scene_reconstruction_observed": True,
                        "nav2_costmap_updated_from_perception": True,
                    }
                }
            },
        )
    )

    assert payload is not None
    assert payload["perception_source"] == "isaac_ros_nvblox"
    assert payload["nav2_costmap_updated_from_perception"] is True


def test_public_nvblox_available_fixture_is_evidence_only() -> None:
    fixture = json.loads(
        (FIXTURE_DIR / "available-perception-evidence.json").read_text(
            encoding="utf-8"
        )
    )

    evidence = build_nvblox_perception_evidence(fixture)
    payload = evidence.model_dump(mode="json")

    assert payload["evidence_status"] == "available"
    assert payload["perception_evidence_available"] is True
    assert payload["depth_input_observed"] is True
    assert payload["pose_input_observed"] is True
    assert payload["scene_reconstruction_observed"] is True
    assert payload["nav2_costmap_updated_from_perception"] is True
    assert payload["dynamic_obstacle_observed"] is True
    assert payload["perception_artifact_refs"] == [
        "fixture://nvblox/perception_evidence.json",
        "fixture://nvblox/nav2_costmap.json",
    ]
    assert payload["approval_authority_created"] is False
    assert payload["dispatch_authority_created"] is False
    assert payload["completion_claimed"] is False
    assert payload["obstacle_avoidance_completion_claimed"] is False
    assert payload["physical_execution_invoked"] is False
    assert payload["mission_delivery_completion_claimed"] is False
    assert payload["progress_counted"] is False


def test_public_nvblox_unavailable_fixture_fails_closed() -> None:
    fixture = json.loads(
        (FIXTURE_DIR / "unavailable-missing-pose-evidence.json").read_text(
            encoding="utf-8"
        )
    )

    evidence = build_nvblox_perception_evidence(fixture)
    payload = evidence.model_dump(mode="json")

    assert payload["evidence_status"] == "unavailable"
    assert payload["perception_evidence_available"] is False
    assert payload["depth_input_observed"] is True
    assert payload["pose_input_observed"] is False
    assert payload["scene_reconstruction_observed"] is True
    assert payload["nav2_costmap_updated_from_perception"] is True
    assert "nvblox_pose_input_not_observed" in payload["blocking_reasons"]
    assert payload["completion_claimed"] is False
    assert payload["obstacle_avoidance_completion_claimed"] is False
    assert payload["physical_execution_invoked"] is False
    assert payload["mission_delivery_completion_claimed"] is False
