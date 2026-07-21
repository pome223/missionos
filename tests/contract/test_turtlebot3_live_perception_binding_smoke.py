"""Regression contract for the opt-in live perception smoke."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import shlex
import sys

from scripts.smoke_turtlebot3_live_perception_binding import run_smoke
from src.intelligence.turtlebot3_perception_sidecar import (
    TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED_ENV,
    TURTLEBOT3_PERCEPTION_SIDECAR_ALLOW_OVERRIDE_ENV,
    TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND_ENV,
)


def test_command_override_cannot_pass_live_vlm_smoke(
    tmp_path: Path,
    monkeypatch,
) -> None:
    image_path = tmp_path / "frame.png"
    image_path.write_bytes(b"fixture-image")
    image_sha256 = sha256(image_path.read_bytes()).hexdigest()
    observed_at = datetime.now(timezone.utc).isoformat()
    receipt_path = tmp_path / "capture.json"
    receipt_path.write_text(
        json.dumps(
            {
                "camera_frame_sha256": image_sha256,
                "camera_lidar_observation": {
                    "camera_observed_at": observed_at,
                    "camera_received_at": observed_at,
                    "camera_width": 640,
                    "camera_fx": 554.25,
                    "camera_cx": 320.0,
                    "lidar_observed_at": observed_at,
                    "lidar_obstacle_observed": True,
                    "lidar_horizontal_sector": "center",
                    "lidar_candidate_bearing_rad": 0.0,
                    "target_candidate_id": "lidar_candidate:fixture",
                    "lidar_evidence_ref": "laser_scan:fixture",
                },
            }
        ),
        encoding="utf-8",
    )
    sidecar_path = tmp_path / "sidecar.py"
    sidecar_path.write_text(
        "import json\n"
        "print(json.dumps({"
        "'claim_kind':'corridor_blocked_by_object',"
        "'confidence':0.9,"
        "'horizontal_sector':'center',"
        "'target_center_x_normalized':0.5}))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND_ENV,
        f"{shlex.quote(sys.executable)} {shlex.quote(str(sidecar_path))}",
    )
    monkeypatch.setenv(TURTLEBOT3_PERCEPTION_SIDECAR_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED_ENV, raising=False)

    result, passed = run_smoke(
        capture_receipt_path=receipt_path,
        image_path=image_path,
        decision_epoch_ref="fixture:epoch:1",
    )

    binding = result["perception_claim"]["corroboration_binding"]
    assert passed is False
    assert result["smoke_passed"] is False
    assert result["image_hash_matches_receipt"] is True
    assert binding["temporal_status"] == "bound"
    assert binding["spatial_status"] == "bound"
    assert binding["live_vlm_invocation_observed"] is False
    assert binding["progressive_action_supported"] is False
    assert "perception_binding_live_vlm_invocation_required" in (
        binding["blocking_reasons"]
    )
