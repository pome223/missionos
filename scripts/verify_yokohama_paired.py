"""Verify CPU flight pair separation; export model inputs and withheld truth separately."""

from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import sys
import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts import ship_anwm  # noqa: E402
from src.runtime.yokohama_native import digest, load_capture, read_asset  # noqa: E402
from src.runtime.yokohama_paired import evaluation_frames  # noqa: E402


def read(path):
    return json.loads(path.read_text())


def bound(root, entry):
    path = root / entry["file"]
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Paired capture escaped run")
    if ship_anwm.digest(path) != entry["sha256"]:
        raise ValueError("Paired capture hash mismatch")
    return path


def crop(path):
    return (
        Image.open(path)
        .convert("RGB")
        .crop((80, 0, 560, 360))
        .resize((224, 224), Image.Resampling.BILINEAR)
    )


def verify(root, export, capture_only=False):
    config = read(root / "config.json")
    result = read(root / "result.json") if (root / "result.json").exists() else None
    if not capture_only:
        assert result and result["status"] == "passed" and result["cleanup"]
        assert not any(result[k] for k in ["vla_invoked", "wam_invoked", "gpu_requested"])
        assert result["observed"]["city_decision_updates"] == 2
    assert config["decisions"]["backend"] == "fixture"
    assert config["decisions"]["capture_paired_views"]
    rows = []
    events = [json.loads(line) for line in (root / "flight-events.jsonl").read_text().splitlines()]
    assert sum(e["event"] == "city_segment_arrived" for e in events) == 2
    assert any(e["event"] == "city_late_response_rejected" for e in events)
    export.mkdir(exist_ok=False, parents=True)
    mailboxes = [read(p) for p in (root / "decisions").glob("*-request.json")]
    for cycle in (1, 2):
        paired = read(root / f"city-{cycle:02d}-paired-views.json")
        event = next(
            e
            for e in events
            if e["event"] == "city_paired_views_recorded"
            and e["file"] == f"city-{cycle:02d}-paired-views.json"
        )
        assert ship_anwm.digest(root / event["file"]) == event["sha256"]
        activated = next(
            e["permit"]
            for e in events
            if e["event"] == "city_permit_consumed" and e["permit"]["cycle"] == cycle
        )
        assert paired["permit_sha256"] == digest(activated)
        assert paired["prepared_candidate"] == activated["candidate"]
        assert paired["run_id"] == config["run_id"] and paired["cycle"] == cycle
        assert paired["evaluation_only"] and not paired["future_observations_sent_to_model"]
        request = next(r for r in mailboxes if r["cycle"] == cycle and r["operation"] == "wam")
        assert paired["input_capture"] == request["capture"]
        input_path = bound(root, paired["input_capture"])
        record, arrays = load_capture(input_path)
        cutoff = record["frames"][-1]["stamp_ns"] / 1e9
        assert cutoff == paired["input_cutoff_sim_s"]
        folder = export / "inputs" / f"D{cycle}"
        truth = export / "heldout" / f"D{cycle}"
        folder.mkdir(parents=True)
        truth.mkdir(parents=True)
        original = root / "decisions" / f"{request['sequence']:03d}" / "input"
        req, archived = ship_anwm.validate(original / "request.json")
        for key in arrays:
            np.testing.assert_array_equal(archived[key], arrays[key])
        shutil.copy2(original / "history.npz", folder / "history.npz")
        shutil.copy2(original / "request.json", folder / "request.json")
        raw = read_asset(input_path.parent, record["frames"][-1]["assets"]["onboard_depth_raw"])
        np.save(folder / "last-raw-depth.npy", np.frombuffer(raw, "<f4").reshape(360, 640))
        crop(input_path.parent / record["frames"][-1]["assets"]["onboard_rgb_png"]["file"]).save(
            truth / "before.png"
        )
        item = dict(
            cycle=cycle,
            input_cutoff_sim_s=cutoff,
            input_capture_sha256=paired["input_capture"]["sha256"],
            request_sha256=ship_anwm.digest(folder / "request.json"),
            history_sha256=req["history_sha256"],
            outcomes={},
        )
        for kind, after, index in [
            ("hold", cutoff, 3),
            ("endpoint", paired["arrival_observation"]["sim_s"], 0),
        ]:
            path = bound(root, paired[kind + "_outcome"])
            evaluation = evaluation_frames(path, after)
            frame = evaluation["frames"][index]
            assert frame["stamp_ns"] > arrays["stamps_ns"].max()
            if kind == "hold":
                assert abs(frame["stamp_ns"] / 1e9 - cutoff - 1) <= 0.004000001
            asset = frame["assets"]["onboard_rgb_png"]
            read_asset(path.parent, asset)
            crop(path.parent / asset["file"]).save(truth / (kind + ".png"))
            pose = ship_anwm.camera_pose(
                dict(
                    vehicle_position_enu_m=frame["pose"]["xyz"],
                    vehicle_quaternion_wxyz=frame["pose"]["quat_wxyz"],
                )
            )
            candidate = req["candidates"][0 if kind == "hold" else 1]
            target = ship_anwm.action_pose(arrays["poses"][-1], candidate["delta"])
            distance = float(np.linalg.norm(pose[:3, 3] - target[:3, 3]))
            rotation = float(
                np.arccos(np.clip((np.trace(pose[:3, :3].T @ target[:3, :3]) - 1) / 2, -1, 1))
            )
            assert distance <= 0.3 and rotation <= 0.05
            item["outcomes"][kind] = dict(
                stamp_ns=frame["stamp_ns"],
                elapsed_after_input_s=frame["stamp_ns"] / 1e9 - cutoff,
                target_camera_error_m=distance,
                target_camera_rotation_error_rad=rotation,
                source_capture_sha256=paired[kind + "_outcome"]["sha256"],
                actual_rgb_sha256=asset["sha256"],
                model_time_alignment_verified=False,
            )
        rows.append(item)
    assert not any("evaluation" in json.dumps(r.get("capture", {})) for r in mailboxes)
    return dict(
        status="passed",
        qualification_scope="paired_capture_only" if capture_only else "paired_capture_and_flight",
        flight_terminal_status=result["status"] if result else "pending",
        run_id=config["run_id"],
        pairs=rows,
        real_model_flight=False,
        future_images_in_model_payload=False,
        outcome_use="offline evaluation only; not flight authorization",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--export-dir", type=Path, required=True)
    parser.add_argument("--capture-only", action="store_true",
                        help="Qualify completed pairs independently of the remaining AP route")
    args = parser.parse_args()
    value = verify(args.root.resolve(), args.export_dir.resolve(), args.capture_only)
    (args.export_dir / "verification.json").write_text(json.dumps(value, indent=2) + "\n")
    print(json.dumps(value, indent=2))
