"""Training payloads must not smuggle future observations into native requests."""

import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest

from scripts.yokohama_pad_focus_data import CROP, crop, sha, validate, write
from scripts.capture_yokohama_pad_focus import native_focus_cases


def dataset(tmp_path):
    (tmp_path / "histories").mkdir()
    stamps = np.arange(16, dtype=np.int64) * 250000000
    np.savez_compressed(
        tmp_path / "histories/test.npz", rgb=np.zeros((16, 224, 224, 3), np.uint8), stamps_ns=stamps
    )
    d = dict(
        schema="pad_native_focus_dataset.v1",
        crop_xyxy=list(CROP),
        test_targets_uploaded=False,
        samples=[
            dict(
                id="test",
                split="test",
                history="histories/test.npz",
                offset=16,
                cutoff_stamp_ns=int(stamps[-1]),
            )
        ],
        assets={"histories/test.npz": sha(tmp_path / "histories/test.npz")},
    )
    write(tmp_path / "dataset.json", d)
    return d


def test_past_only_payload_is_cpu_readable(tmp_path):
    dataset(tmp_path)
    assert validate(tmp_path)["samples"][0]["split"] == "test"


@pytest.mark.parametrize(
    "field,value",
    [("target", "future.png"), ("truth", "occupied"), ("offset", 64), ("cutoff_stamp_ns", 0)],
)
def test_reject_future_supervision_or_wrong_clock(tmp_path, field, value):
    d = dataset(tmp_path)
    d["samples"][0][field] = value
    write(tmp_path / "dataset.json", d)
    with pytest.raises(ValueError):
        validate(tmp_path)


def test_reject_unlisted_future_file(tmp_path):
    dataset(tmp_path)
    (tmp_path / "future.png").write_bytes(b"not an observation")
    with pytest.raises(ValueError, match="payload"):
        validate(tmp_path)


def test_reject_actor_pose_in_history(tmp_path):
    d = dataset(tmp_path)
    np.savez_compressed(
        tmp_path / "histories/test.npz",
        rgb=np.zeros((16, 224, 224, 3), np.uint8),
        stamps_ns=np.arange(16) * 250000000,
        lead_pose=np.zeros((16, 3)),
    )
    d["assets"]["histories/test.npz"] = sha(tmp_path / "histories/test.npz")
    write(tmp_path / "dataset.json", d)
    with pytest.raises(ValueError, match="observation"):
        validate(tmp_path)


def test_new_captures_have_complete_future_windows():
    cases = native_focus_cases()
    assert len({c["id"] for c in cases}) == 3
    assert sum(len(c["cutoffs"]) for c in cases) == 14
    assert all(4 <= t <= c["knots"][-1][0] - 4 for c in cases for t in c["cutoffs"])


def test_fixed_crop_uses_recorded_pixels():
    a = np.full((360, 640, 3), 73, np.uint8)
    result = crop(a)
    assert result.shape == (224, 224, 3) and np.all(result == 73)


def test_capture_cli_stages_the_worker_invoked_by_docker(tmp_path):
    """Exercise the renamed CLI through a Docker fixture; no simulator starts."""
    rig, output, binaries = (tmp_path / p for p in ("rig", "output", "bin"))
    (rig / "assets").mkdir(parents=True)
    (rig / "models/worlds").mkdir(parents=True)
    (rig / "models/worlds/default.sdf").write_text(
        '<sdf><world><model name="x500_0"><pose>0 0 0 0 0 0</pose></model></world></sdf>'
    )
    (rig / "capture-result.json").write_text('{"status":"passed"}')
    (rig / "config.json").write_text(
        json.dumps(
            {
                "world": {"pad_queue": {"pad_xyz_m": [0, 0, 0]}},
                "diagnostic_cases": [{}, {"camera_xyz": [0, 0, 0], "camera_yaw": 0}],
            }
        )
    )
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, pathlib, sys
root = pathlib.Path(os.environ["PAD_CAPTURE_FIXTURE_OUTPUT"])
args = sys.argv[1:]
if args[0] == "exec":
    worker = root / pathlib.Path(args[-2]).name
    assert args[-1] == "--worker" and worker.is_file(), args
    assert worker.name == "capture_yokohama_pad_focus.py", args
    (root / "fixture-worker-invocation.json").write_text(json.dumps(args))
elif args[0] == "inspect":
    sys.exit(1)
"""
    )
    docker.chmod(0o755)
    command = [
        sys.executable,
        str(Path(__file__).resolve().parents[1] / "scripts/capture_yokohama_pad_focus.py"),
        "--source-rig",
        str(rig),
        "--output-dir",
        str(output),
        "--case-set",
        "native-focus-v1",
    ]
    env = {
        **os.environ,
        "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
        "PAD_CAPTURE_FIXTURE_OUTPUT": str(output),
    }
    rejected = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
    assert rejected.returncode == 2 and not output.exists()
    subprocess.run(
        [*command, "--approve-sitl"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert (output / "fixture-worker-invocation.json").is_file()
    assert json.loads((output / "config.json").read_text())["motion_cases"] == native_focus_cases()
    assert json.loads((output / "cleanup.json").read_text())["removed"] is True
