"""CLI audit of generated sensor fixtures; no inference or dispatch."""

import json
from pathlib import Path

from click.testing import CliRunner
from PIL import Image
import pytest

from missionos_cli.cli import missionos
from scripts import ship_aerovla
from scripts.ship_anwm import digest


@pytest.fixture
def audit_files(tmp_path):
    capture = tmp_path / "run/native-capture"
    capture.mkdir(parents=True)
    (capture / "image.bin").write_bytes(bytes([255, 0, 0]) * (640 * 360))
    frame = {
        "vehicle_position_enu_m": [0, 70, 30],
        "vehicle_quaternion_wxyz": [1, 0, 0, 0],
        "simulation_time_ns": 50_000_000_000,
        "received_at_unix_s": {"rgb": 1000.0, "down": 1000.0, "pose": 1000.0},
        "assets": {
            k: {"file": "image.bin", "sha256": digest(capture / "image.bin")}
            for k in ("rgb", "down")
        },
    }
    config = {
        "run_id": "fixture",
        "world_sha256": "world",
        "plan_sha256": "plan",
        "goal_north_m": 300,
        "timeout_s": 900,
        "urban": {"case": "fixture", "entry_north_m": 70, "cruise_altitude_m": 30},
    }
    (capture.parent / "config.json").write_text(json.dumps(config))
    capture_file = capture / "capture.json"
    capture_file.write_text(
        json.dumps(
            {
                "schema_version": "ship_anwm_capture.v1",
                **{k: config[k] for k in ("run_id", "world_sha256", "plan_sha256")},
                "history_indices": list(range(16)),
                "frames": [frame] * 16,
            }
        )
    )
    inputs, outputs = tmp_path / "inputs", tmp_path / "outputs"
    ship_aerovla.prepare(capture_file, inputs)
    outputs.mkdir()
    Image.new("RGB", (224, 448), (255, 0, 0)).save(outputs / "actual-mosaic.png")
    (outputs / "result.json").write_text(
        json.dumps(
            {
                "schema_version": "ship_aerovla_invocation.v1",
                "request_sha256": digest(inputs / "request.json"),
                "runtime_sha256": digest(Path(ship_aerovla.__file__)),
                "base_revision": ship_aerovla.BASE_REVISION,
                "adapter_revision": ship_aerovla.ADAPTER_REVISION,
                "adapter_sha256": ship_aerovla.ADAPTER_SHA256,
                "upstream_revision": ship_aerovla.UPSTREAM_REVISION,
                "vla_inference_invoked": True,
                "dispatch_invoked": False,
                "physical_execution_invoked": False,
                "mosaic_sha256": digest(outputs / "actual-mosaic.png"),
                "generated_text": "55 85 49</s>",
                "proposal": {"down_m": 0, "dispatch_allowed": True},
            }
        )
    )
    return capture.parent, inputs, outputs


def invoke(files):
    run, inputs, outputs = files
    return CliRunner().invoke(
        missionos,
        [
            "ship-delivery",
            "vla-audit",
            "--run-dir",
            str(run),
            "--model-input",
            str(inputs),
            "--model-output",
            str(outputs),
        ],
    )


def test_cli_recomputes_text_and_never_promotes_archive(audit_files):
    result = invoke(audit_files)
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["input_pixel_binding_verified"] is True
    assert report["assessment"]["candidate"]["target_ned_m"][2] == pytest.approx(-26.3265306122449)
    assert "altitude_envelope_violation" in report["assessment"]["reasons"]
    assert "archived_observation_not_live_authority" in report["assessment"]["reasons"]
    assert report["assessment"]["candidate_eligible"] is False
    assert report["assessment"]["dispatch_allowed"] is False
    assert report["model_inference_rerun"] is False


@pytest.mark.parametrize(
    "fault", ["request_binding", "pixels", "pose", "mosaic", "runtime", "dispatch"]
)
def test_cli_rejects_broken_evidence_chain(audit_files, fault):
    run, inputs, outputs = audit_files
    path = outputs / "result.json"
    result = json.loads(path.read_text())
    if fault == "request_binding":
        result["request_sha256"] = "bad"
    elif fault == "runtime":
        result["runtime_sha256"] = "bad"
    elif fault == "dispatch":
        result["dispatch_invoked"] = True
    elif fault == "pixels":
        (inputs / "rgb.png").write_bytes(b"bad")
    elif fault == "pose":
        capture = run / "native-capture/capture.json"
        value = json.loads(capture.read_text())
        value["frames"][15]["vehicle_position_enu_m"][2] = 25
        capture.write_text(json.dumps(value))
    elif fault == "mosaic":
        Image.new("RGB", (224, 448), (0, 255, 0)).save(outputs / "actual-mosaic.png")
        result["mosaic_sha256"] = digest(outputs / "actual-mosaic.png")
    path.write_text(json.dumps(result))
    value = invoke(audit_files)
    assert value.exit_code == 1
    assert "Error:" in value.output
