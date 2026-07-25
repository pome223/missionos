"""Contract tests for the VLM perception sidecar (issue #31).

Exercises the command-override backend, matching this codebase's testing
posture for every Gemini-backed capability: the live ADK/Gemini path itself
is opt-in and uncovered by the fast suite (see
turtlebot3_recovery_planner.py's identical pattern), verified instead by
opt-in live smokes with real credentials.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import shlex
import sys

import pytest

from src.intelligence.turtlebot3_perception_sidecar import (
    TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED_ENV,
    TURTLEBOT3_PERCEPTION_SIDECAR_ALLOW_OVERRIDE_ENV,
    TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND_ENV,
    build_turtlebot3_perception_sidecar_prompt,
    run_turtlebot3_perception_sidecar,
)
from src.runtime.perception_corroboration_binding import (
    build_perception_corroboration_binding,
)
from src.runtime.runtime_claim_evidence import validate_runtime_invocation_evidence

_FAKE_PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-frame-for-tests"


def _command(path: Path) -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(path))}"


def _write_frame(tmp_path: Path) -> Path:
    frame = tmp_path / "frame.png"
    frame.write_bytes(_FAKE_PNG_BYTES)
    return frame


def _write_classifying_sidecar(path: Path, *, claim_kind: str, confidence: float) -> None:
    path.write_text(
        "import json, sys\n"
        "prompt = json.loads(sys.stdin.read())\n"
        f"assert prompt['task'] == 'classify_camera_frame_for_recovery_perception_claim'\n"
        "assert 'image_base64' in prompt\n"
        "assert 'corroborated_by' not in prompt\n"
        "print(json.dumps({\n"
        f"    'claim_kind': {claim_kind!r},\n"
        f"    'confidence': {confidence},\n"
        "    'horizontal_sector': 'center',\n"
        "    'target_center_x_normalized': 0.5,\n"
        "}))\n",
        encoding="utf-8",
    )


def _write_self_corroborating_sidecar(path: Path) -> None:
    path.write_text(
        "import json, sys\n"
        "prompt = json.loads(sys.stdin.read())\n"
        "print(json.dumps({\n"
        "    'claim_kind': 'corridor_blocked_by_object',\n"
        "    'confidence': 0.9,\n"
        "    'horizontal_sector': 'center',\n"
        "    'target_center_x_normalized': 0.5,\n"
        "    'corroborated_by': ['lidar_costmap:fabricated_by_sidecar'],\n"
        "}))\n",
        encoding="utf-8",
    )


def _write_malformed_sidecar(path: Path) -> None:
    path.write_text(
        "import sys\n"
        "print('not json')\n",
        encoding="utf-8",
    )


def _write_out_of_range_confidence_sidecar(path: Path) -> None:
    path.write_text(
        "import json, sys\n"
        "print(json.dumps({'claim_kind': 'path_clear', 'confidence': 1.5}))\n",
        encoding="utf-8",
    )


def test_prompt_is_source_bound_and_excludes_authority_fields() -> None:
    prompt = build_turtlebot3_perception_sidecar_prompt(
        image_sha256="a" * 64,
        include_image_base64="ZmFrZQ==",
    )
    assert prompt["image_sha256"] == "a" * 64
    assert prompt["image_base64"] == "ZmFrZQ=="
    assert set(prompt["allowed_claim_kinds"]) == {
        "corridor_blocked_by_object",
        "path_clear",
        "landing_zone_obstructed",
        "unexpected_entity_detected",
        "floor_hazard_detected",
    }
    assert "corroborated_by" not in prompt
    contract_text = prompt["strict_output_contract"].lower()
    assert "corroborated_by" in contract_text
    assert "approval" in contract_text


def test_reports_not_configured_when_no_backend_enabled(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND_ENV, raising=False)
    monkeypatch.delenv(TURTLEBOT3_PERCEPTION_SIDECAR_ALLOW_OVERRIDE_ENV, raising=False)
    monkeypatch.delenv(TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED_ENV, raising=False)

    result = run_turtlebot3_perception_sidecar(image_path=_write_frame(tmp_path))

    assert result["sidecar_status"] == "not_configured"
    assert result["camera_observation"] == {}
    assert result["dispatch_authority_created"] is False


def test_command_override_requires_explicit_allow_flag(monkeypatch, tmp_path) -> None:
    script = tmp_path / "sidecar.py"
    _write_classifying_sidecar(
        script, claim_kind="corridor_blocked_by_object", confidence=0.7
    )
    monkeypatch.setenv(TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND_ENV, _command(script))
    monkeypatch.delenv(TURTLEBOT3_PERCEPTION_SIDECAR_ALLOW_OVERRIDE_ENV, raising=False)
    monkeypatch.delenv(TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED_ENV, raising=False)

    result = run_turtlebot3_perception_sidecar(image_path=_write_frame(tmp_path))

    assert result["sidecar_status"] == "blocked"
    assert result["blocking_reasons"] == [
        f"{TURTLEBOT3_PERCEPTION_SIDECAR_ALLOW_OVERRIDE_ENV}_required"
    ]


def test_command_override_classifies_frame_and_hashes_source(
    monkeypatch, tmp_path
) -> None:
    script = tmp_path / "sidecar.py"
    _write_classifying_sidecar(
        script, claim_kind="corridor_blocked_by_object", confidence=0.82
    )
    monkeypatch.setenv(TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND_ENV, _command(script))
    monkeypatch.setenv(TURTLEBOT3_PERCEPTION_SIDECAR_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED_ENV, raising=False)
    frame = _write_frame(tmp_path)

    result = run_turtlebot3_perception_sidecar(image_path=frame)

    assert result["sidecar_status"] == "classified"
    claim = result["camera_observation"]
    assert claim["claim_kind"] == "corridor_blocked_by_object"
    assert claim["confidence"] == 0.82
    assert claim["horizontal_sector"] == "center"
    assert claim["target_center_x_normalized"] == 0.5
    expected_ref = f"sha256:{sha256(_FAKE_PNG_BYTES).hexdigest()}"
    assert claim["source_frame_ref"] == expected_ref
    assert "corroborated_by" not in claim
    evidence = result["llm_invocation_evidence"]
    assert evidence["schema_version"] == "runtime_invocation_evidence.v1"
    assert evidence["provider"] == "command_override"
    assert evidence["invocation_exit_code"] == 0


@pytest.mark.parametrize(
    ("backend", "expected_provider"),
    [
        ("deepseek", "google_adk_litellm_deepseek"),
        ("gemini", "google_adk_gemini"),
    ],
)
def test_adk_sidecar_evidence_tracks_provider_and_binds_live_vlm(
    monkeypatch, tmp_path, backend, expected_provider
) -> None:
    from src.intelligence import turtlebot3_perception_sidecar as sidecar

    async def fake_adk_response(**_kwargs) -> str:
        return (
            '{"claim_kind":"corridor_blocked_by_object","confidence":0.75,'
            '"horizontal_sector":"center","target_center_x_normalized":0.5}'
        )

    monkeypatch.setenv("MISSIONOS_LLM_BACKEND", backend)
    monkeypatch.delenv(
        "MISSIONOS_AGENT_MISSIONOS_TURTLEBOT3_PERCEPTION_SIDECAR_AGENT_LLM_BACKEND",
        raising=False,
    )
    monkeypatch.delenv(TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND_ENV, raising=False)
    monkeypatch.delenv(TURTLEBOT3_PERCEPTION_SIDECAR_ALLOW_OVERRIDE_ENV, raising=False)
    monkeypatch.setenv(TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED_ENV, "1")
    monkeypatch.setattr(
        sidecar, "_invoke_adk_perception_response_async", fake_adk_response
    )

    frame = _write_frame(tmp_path)
    result = run_turtlebot3_perception_sidecar(image_path=frame)

    assert result["sidecar_status"] == "classified"
    evidence = result["llm_invocation_evidence"]
    assert evidence["provider"] == expected_provider
    assert evidence["invocation_kind"] == "llm_api"
    assert str(evidence["invocation_target"]).startswith("google_adk:")
    assert validate_runtime_invocation_evidence(evidence)["provider"] == expected_provider

    now = datetime.now(timezone.utc)
    claim = result["camera_observation"]
    frame_sha256 = sha256(frame.read_bytes()).hexdigest()
    binding = build_perception_corroboration_binding(
        source_frame_ref=str(claim["source_frame_ref"]),
        claim_kind=str(claim["claim_kind"]),
        camera_horizontal_sector=str(claim["horizontal_sector"]),
        target_center_x_normalized=claim["target_center_x_normalized"],
        runtime_context={
            "decision_epoch_ref": "proposal:test:perception",
            "capture": {
                "camera_frame_sha256": frame_sha256,
                "camera_lidar_observation": {
                    "camera_observed_at": now.isoformat(),
                    "camera_received_at": now.isoformat(),
                    "camera_width": 640,
                    "camera_fx": 554.25,
                    "camera_cx": 320.0,
                    "lidar_observed_at": now.isoformat(),
                    "lidar_obstacle_observed": True,
                    "lidar_horizontal_sector": "center",
                    "lidar_candidate_bearing_rad": 0.0,
                    "target_candidate_id": "lidar_candidate:fixture",
                    "lidar_evidence_ref": "laser_scan:fixture",
                },
            },
            "llm_invocation_evidence": evidence,
        },
    )

    assert binding.runtime_invocation_evidence_valid is True
    assert binding.live_vlm_invocation_observed is True


def test_self_reported_corroboration_is_stripped_not_trusted(
    monkeypatch, tmp_path
) -> None:
    script = tmp_path / "sidecar.py"
    _write_self_corroborating_sidecar(script)
    monkeypatch.setenv(TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND_ENV, _command(script))
    monkeypatch.setenv(TURTLEBOT3_PERCEPTION_SIDECAR_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED_ENV, raising=False)

    result = run_turtlebot3_perception_sidecar(image_path=_write_frame(tmp_path))

    assert result["sidecar_status"] == "classified"
    claim = result["camera_observation"]
    assert set(claim.keys()) == {
        "claim_kind",
        "source_frame_ref",
        "confidence",
        "horizontal_sector",
        "target_center_x_normalized",
    }
    assert "corroborated_by" not in claim


def test_disallowed_claim_kind_is_blocked(monkeypatch, tmp_path) -> None:
    script = tmp_path / "sidecar.py"
    _write_classifying_sidecar(script, claim_kind="totally_made_up_kind", confidence=0.5)
    monkeypatch.setenv(TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND_ENV, _command(script))
    monkeypatch.setenv(TURTLEBOT3_PERCEPTION_SIDECAR_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED_ENV, raising=False)

    result = run_turtlebot3_perception_sidecar(image_path=_write_frame(tmp_path))

    assert result["sidecar_status"] == "blocked"
    assert "claim_kind_not_allowed" in result["blocking_reasons"]


def test_out_of_range_confidence_is_blocked(monkeypatch, tmp_path) -> None:
    script = tmp_path / "sidecar.py"
    _write_out_of_range_confidence_sidecar(script)
    monkeypatch.setenv(TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND_ENV, _command(script))
    monkeypatch.setenv(TURTLEBOT3_PERCEPTION_SIDECAR_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED_ENV, raising=False)

    result = run_turtlebot3_perception_sidecar(image_path=_write_frame(tmp_path))

    assert result["sidecar_status"] == "blocked"
    assert "confidence_out_of_range" in result["blocking_reasons"]


def test_malformed_stdout_is_blocked_not_raised(monkeypatch, tmp_path) -> None:
    script = tmp_path / "sidecar.py"
    _write_malformed_sidecar(script)
    monkeypatch.setenv(TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND_ENV, _command(script))
    monkeypatch.setenv(TURTLEBOT3_PERCEPTION_SIDECAR_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED_ENV, raising=False)

    result = run_turtlebot3_perception_sidecar(image_path=_write_frame(tmp_path))

    assert result["sidecar_status"] == "blocked"
    assert "turtlebot3_perception_sidecar_stdout_not_json_object" in (
        result["blocking_reasons"]
    )


def test_claim_output_feeds_directly_into_perception_claim_builder(
    monkeypatch, tmp_path
) -> None:
    from src.runtime.perception_claim import build_perception_claim_from_camera_observation

    script = tmp_path / "sidecar.py"
    _write_classifying_sidecar(
        script, claim_kind="corridor_blocked_by_object", confidence=0.75
    )
    monkeypatch.setenv(TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND_ENV, _command(script))
    monkeypatch.setenv(TURTLEBOT3_PERCEPTION_SIDECAR_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED_ENV, raising=False)

    result = run_turtlebot3_perception_sidecar(image_path=_write_frame(tmp_path))
    claim = build_perception_claim_from_camera_observation(
        result["camera_observation"],
        costmap_obstacle_observed=True,
    )

    assert claim is not None
    assert claim.claim_kind == "corridor_blocked_by_object"
    assert claim.corroborated_by == ("lidar_costmap:nav2_costmap_obstacle_observed",)
