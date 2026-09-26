"""RGB evidence, input leakage, matched baselines and bounded capture contracts."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

from click.testing import CliRunner
from PIL import Image, ImageDraw
import pytest

from missionos_cli.cli import missionos
from scripts.ship_urban_camera_worker import png_rgb
from src.runtime.ship_urban_camera import FOCAL_PX, POLICIES, choose_camera_action, obstacle_pixels
from src.runtime.ship_urban_camera_scene import (
    CASES,
    SAMPLE_TIMES,
    actor_x,
    capture_config,
    camera_world,
)
from src.runtime.ship_urban_camera_screen import (
    ANALYSIS_SOURCES,
    digest,
    inspect_camera_records,
    run_camera_screen,
    verify_camera_screen,
)


def history(case):
    return [
        {"observed_at_s": t, "center_x_px": 320 + FOCAL_PX * actor_x(case, t) / 96}
        for t in SAMPLE_TIMES[:5]
    ]


@pytest.mark.parametrize(
    "case,velocity,stopping",
    [
        ("short_clear", "wait", "wait"),
        ("long_block", "detour", "detour"),
        ("brake_stop", "wait", "detour"),
    ],
)
def test_braking_baseline_changes_proposal_from_same_observations(case, velocity, stopping):
    rows = history(case)
    untouched = deepcopy(rows)
    assert choose_camera_action(rows, "constant_velocity")["action_proposal"] == velocity
    assert choose_camera_action(rows, "stopping_aware")["action_proposal"] == stopping
    assert rows == untouched
    assert not choose_camera_action(rows, "stopping_aware")["dispatch_authorized"]


@pytest.mark.parametrize(
    "mutation", ["future", "case", "nan", "duplicate", "gap", "few", "clock", "outside", "oracle"]
)
def test_invalid_or_privileged_inputs_fail_closed(mutation):
    rows, policy = history("brake_stop"), "stopping_aware"
    if mutation == "future":
        rows[-1]["future_clear_at_s"] = 42.5
    if mutation == "case":
        rows[-1]["case"] = "brake_stop"
    if mutation == "nan":
        rows[-1]["center_x_px"] = float("nan")
    if mutation == "duplicate":
        rows[-1]["observed_at_s"] = rows[-2]["observed_at_s"]
    if mutation == "gap":
        rows[-1]["observed_at_s"] += 2
    if mutation == "few":
        rows.pop()
    if mutation == "clock":
        rows[0]["observed_at_s"] = -1
    if mutation == "outside":
        rows[-1]["center_x_px"] = 641
    if mutation == "oracle":
        policy = "oracle"
    with pytest.raises(ValueError):
        choose_camera_action(rows, policy)


def frame(path, x=320):
    image = Image.new("RGB", (640, 360), (90, 100, 90))
    ImageDraw.Draw(image).rectangle((round(x - 40), 60, round(x + 40), 300), fill=(210, 50, 25))
    image.save(path)


def test_pixel_tracking_and_missing_clipped_ambiguous_rejection(tmp_path):
    path = tmp_path / "image.png"
    frame(path, 340)
    assert obstacle_pixels(path)["center_x_px"] == 340
    frame(path, 10)
    with pytest.raises(ValueError, match="Clipped"):
        obstacle_pixels(path)
    image = Image.new("RGB", (640, 360))
    image.save(path)
    with pytest.raises(ValueError, match="not visible"):
        obstacle_pixels(path)
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 100, 140, 140), fill=(210, 50, 25))
    draw.rectangle((300, 100, 340, 140), fill=(210, 50, 25))
    image.save(path)
    with pytest.raises(ValueError, match="Ambiguous"):
        obstacle_pixels(path)


def test_stdlib_png_capture_preserves_rgb(tmp_path):
    data = bytes([255, 0, 4, 10, 20, 30])
    path = tmp_path / "rgb.png"
    path.write_bytes(png_rgb(2, 1, data))
    with Image.open(path) as image:
        assert image.mode == "RGB" and image.tobytes() == data


def saved_fixture(root):
    (root / "capture-config.json").write_text(json.dumps(capture_config("fixture")))
    rows = []
    for ci, case in enumerate(CASES):
        for i, t in enumerate(SAMPLE_TIMES):
            path = root / f"{case}-{i:02}.png"
            frame(path, 320 + FOCAL_PX * actor_x(case, t) / 96)
            rows.append(
                {
                    "case": case,
                    "index": i,
                    "file": path.name,
                    "run_id": "fixture",
                    "scripted_at_s": t,
                    "observed_at_s": t,
                    "sensor_stamp_s": 1 + ci * 10 + t,
                    "source": "gz.msgs.Image",
                    "clock": "gazebo_simulation",
                    "frame_role": "decision" if i < 5 else "validation_only",
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    (root / "frames.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    return rows


def test_screen_retains_stronger_baseline_and_future_exclusion(tmp_path):
    saved_fixture(tmp_path)
    report = inspect_camera_records(tmp_path)
    assert report["mean_analytic_regret_s"]["constant_velocity"] > 5
    assert report["oracle_headroom_over_best_simple_s"] == 0
    assert not report["learned_model_comparison_admitted"]
    assert not report["px4_runtime_invoked"]
    assert all(len(c["history"]) == 5 and len(c["validation_frames"]) == 2 for c in report["cases"])
    assert all(set(c["proposals"]) == set(POLICIES) for c in report["cases"])


@pytest.mark.parametrize("change", ["hash", "clock", "order", "missing", "role", "path"])
def test_frame_evidence_mutations_rejected(tmp_path, change):
    rows = saved_fixture(tmp_path)
    if change == "hash":
        rows[0]["sha256"] = "0" * 64
    if change == "clock":
        rows[0]["sensor_stamp_s"] += 1
    if change == "order":
        rows[0], rows[1] = rows[1], rows[0]
    if change == "missing":
        rows.pop()
    if change == "role":
        rows[0]["frame_role"] = "validation_only"
    if change == "path":
        rows[0]["file"] = "../elsewhere.png"
    (tmp_path / "frames.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(ValueError):
        inspect_camera_records(tmp_path)


def test_no_approval_creates_no_output_or_process(tmp_path):
    output = tmp_path / "denied"
    with pytest.raises(PermissionError):
        run_camera_screen(output_dir=output)
    result = CliRunner().invoke(
        missionos, ["ship-delivery", "urban-camera-screen", "--output-dir", str(output)]
    )
    assert result.exit_code == 1 and "--approve-gazebo" in result.output
    assert not output.exists()


def test_camera_cli_invokes_runtime_boundary(monkeypatch, tmp_path):
    from src.runtime import ship_urban_camera_screen as runtime

    def run(**kwargs):
        assert kwargs == {"output_dir": tmp_path, "operator_approved": True, "timeout_s": 180}
        return {"status": "screened"}

    monkeypatch.setattr(runtime, "run_camera_screen", run)
    result = CliRunner().invoke(
        missionos,
        ["ship-delivery", "urban-camera-screen", "--approve-gazebo", "--output-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output


def archived_fixture(root):
    saved_fixture(root)
    (root / "world.sdf").write_text(camera_world())
    (root / "worker.py").write_text("# Unit-test invocation fixture, not a Gazebo run\n")
    (root / "worker.stdout").write_text("unit fixture\n")
    (root / "worker.stderr").write_text("")
    from src.runtime import ship_urban_camera_screen as runtime

    for name in ANALYSIS_SOURCES:
        shutil.copyfile(Path(runtime.__file__).with_name(name), root / name)
    report = inspect_camera_records(root)
    stamp = datetime.now(timezone.utc).isoformat()
    report["runtime_invocation_evidence"] = {
        "schema_version": "runtime_invocation_evidence.v1",
        "invocation_kind": "subprocess",
        "invocation_target": "unit-fixture",
        "run_id": "fixture",
        "execution_scope": "sim",
        "invocation_started_at": stamp,
        "invocation_completed_at": stamp,
        "invocation_exit_code": 0,
        "worker_sha256": digest(root / "worker.py"),
        **{f"{s}_artifact_path": str(root / f"worker.{s}") for s in ("stdout", "stderr")},
        **{f"invocation_{s}_sha256": digest(root / f"worker.{s}") for s in ("stdout", "stderr")},
    }
    names = ["world.sdf", "capture-config.json", "worker.py", "frames.jsonl", *ANALYSIS_SOURCES]
    names.extend(f"{case}-{i:02}.png" for case in CASES for i in range(7))
    report["artifact_sha256"] = {name: digest(root / name) for name in names}
    (root / "result.json").write_text(json.dumps(report))
    return report


def test_archive_cli_recomputes_same_pixels(tmp_path):
    archived_fixture(tmp_path)
    result = CliRunner().invoke(
        missionos, ["ship-delivery", "urban-camera-verify", "--run-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["archived_evidence_reverified"]


@pytest.mark.parametrize(
    "mutation",
    [
        "proposal",
        "manifest",
        "world",
        "script",
        "source",
        "failure",
        "scope",
        "stdout",
        "stale_frame",
    ],
)
def test_archive_rejects_tampered_evidence(tmp_path, mutation):
    report = archived_fixture(tmp_path)
    if mutation == "proposal":
        report["cases"][0]["proposals"]["always_wait"]["action_proposal"] = "detour"
    if mutation == "manifest":
        report["artifact_sha256"].pop("frames.jsonl")
    if mutation in ("world", "source", "script", "stale_frame"):
        name = {
            "world": "world.sdf",
            "source": "ship_urban_camera.py",
            "script": "capture-config.json",
            "stale_frame": "brake_stop-04.png",
        }[mutation]
        p = tmp_path / name
        if mutation == "script":
            config = json.loads(p.read_text())
            config["cases"][0]["samples"][1]["x_m"] = 99
            p.write_text(json.dumps(config))
        elif mutation == "stale_frame":
            shutil.copyfile(tmp_path / "brake_stop-00.png", p)
            rows = [
                json.loads(line) for line in (tmp_path / "frames.jsonl").read_text().splitlines()
            ]
            rows[18]["sha256"] = digest(p)
            (tmp_path / "frames.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
            report["artifact_sha256"]["frames.jsonl"] = digest(tmp_path / "frames.jsonl")
        else:
            p.write_text(p.read_text() + "\n")
        # Even updating the local manifest does not bypass calibration/source checks.
        report["artifact_sha256"][name] = digest(p)
    if mutation == "failure":
        report["runtime_invocation_evidence"]["invocation_exit_code"] = 1
    if mutation == "scope":
        report["runtime_invocation_evidence"]["execution_scope"] = "hardware"
    if mutation == "stdout":
        (tmp_path / "worker.stdout").write_text("modified")
    (tmp_path / "result.json").write_text(json.dumps(report))
    with pytest.raises(ValueError):
        verify_camera_screen(tmp_path)


def test_stopping_rule_never_detours_from_already_clear_image_history():
    rows = history("brake_stop")
    for r in rows:
        r["center_x_px"] += 100
    assert choose_camera_action(rows, "stopping_aware")["action_proposal"] == "wait"
