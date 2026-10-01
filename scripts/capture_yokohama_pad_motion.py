#!/usr/bin/env python3
"""Opt-in CPU dynamic-pad camera recording; no PX4 flight or learned inference."""

from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4
import xml.etree.ElementTree as ET
import zlib

REPO = Path(__file__).resolve().parents[1]


def cases():
    # Times and splits are frozen before acquisition. Actor schedule and poses
    # are ground truth only, never input to an image reader or native WAM.
    return [
        dict(
            id="development-depart",
            split="development",
            knots=[[0, "start"], [8, "start"], [14, "up"], [26, "end"], [48, "end"]],
            cutoffs=[10, 18, 30],
        ),
        dict(
            id="evaluation-depart",
            split="evaluation",
            knots=[[0, "start"], [6, "start"], [14, "up"], [30, "end"], [50, "end"]],
            cutoffs=[16, 32],
        ),
        dict(
            id="evaluation-stall",
            split="evaluation",
            knots=[[0, "start"], [8, "start"], [14, "up"], [50, "up"], [62, "end"], [66, "end"]],
            cutoffs=[24],
        ),
        dict(
            id="evaluation-reenter",
            split="evaluation",
            knots=[[0, "end"], [8, "end"], [20, "up"], [42, "up"], [54, "end"], [58, "end"]],
            cutoffs=[14],
        ),
    ]


def actor_xyz(knots, t, points):
    for (ta, a), (tb, b) in zip(knots, knots[1:]):
        if t <= tb:
            f = max(0, min(1, (t - ta) / (tb - ta)))
            return [x + f * (y - x) for x, y in zip(points[a], points[b])]
    return points[knots[-1][1]]


def learning_cases():
    """Fresh motion timings, frozen before native post-training/evaluation."""
    return [
        dict(
            id="train-depart",
            split="train",
            knots=[[0, "start"], [10, "start"], [18, "up"], [32, "end"], [56, "end"]],
            cutoffs=[5, 12, 20, 26, 36],
        ),
        dict(
            id="train-stall",
            split="train",
            knots=[[0, "start"], [5, "start"], [12, "up"], [46, "up"], [58, "end"], [64, "end"]],
            cutoffs=[6, 16, 24, 32, 46],
        ),
        dict(
            id="train-reenter",
            split="train",
            knots=[[0, "end"], [6, "end"], [16, "up"], [40, "up"], [54, "end"], [60, "end"]],
            cutoffs=[6, 10, 18, 28, 42],
        ),
        dict(
            id="heldout-depart",
            split="test",
            knots=[[0, "start"], [7, "start"], [15, "up"], [31, "end"], [56, "end"]],
            cutoffs=[18, 36],
        ),
        dict(
            id="heldout-stall",
            split="test",
            knots=[[0, "start"], [9, "start"], [17, "up"], [49, "up"], [61, "end"], [68, "end"]],
            cutoffs=[28],
        ),
        dict(
            id="heldout-reenter",
            split="test",
            knots=[[0, "end"], [9, "end"], [23, "up"], [45, "up"], [57, "end"], [64, "end"]],
            cutoffs=[17],
        ),
    ]


def state_evaluation_cases():
    """Unused motion timings for the frozen direct-state learner; no training."""
    return [
        dict(
            id="state-depart",
            split="unseen_test",
            knots=[[0, "start"], [9, "start"], [17, "up"], [34, "end"], [52, "end"]],
            cutoffs=[5, 20, 27, 34, 43],
        ),
        dict(
            id="state-stall",
            split="unseen_test",
            knots=[[0, "start"], [6, "start"], [14, "up"], [37, "up"], [51, "end"], [60, "end"]],
            cutoffs=[19, 31, 40, 46, 55],
        ),
        dict(
            id="state-reenter",
            split="unseen_test",
            knots=[[0, "end"], [11, "end"], [27, "up"], [41, "up"], [56, "end"], [64, "end"]],
            cutoffs=[13, 20, 25, 32, 47, 58],
        ),
    ]


def worker():
    from gz.msgs10.boolean_pb2 import Boolean
    from gz.msgs10.pose_pb2 import Pose
    from yokohama_sitl_worker import Observer, stamp
    from ship_urban_camera_worker import png_rgb

    root = Path("/mission")
    config = json.loads((root / "config.json").read_text())

    class MotionObserver(Observer):
        def __init__(self, c):
            self.lead_history = deque(maxlen=10000)
            super().__init__(c)

        def receive_poses(self, m):
            super().receive_poses(m)
            with self.lock:
                for p in m.pose:
                    if p.name == "queue_lead":
                        self.lead_history.append(
                            dict(
                                stamp_s=stamp(m),
                                xyz=[p.position.x, p.position.y, p.position.z],
                                entity_id=p.id,
                            )
                        )

    obs = MotionObserver(config)
    policy = config["world"]["pad_queue"]
    points = {k: policy[f"lead_{k}_xyz_m"] for k in ("start", "up", "end")}
    receipt = dict(status="failed", aircraft_flown=False, native_inference=False, cases=[])

    def set_pose(name, xyz):
        if name not in {"queue_lead", "queue_parcel"}:
            raise ValueError("Unowned actor")
        m = Pose()
        m.name = name
        m.position.x, m.position.y, m.position.z = xyz
        m.orientation.w = 1
        obs.node.request("/world/default/set_pose", m, Pose, Boolean, 100)

    try:
        deadline = time.monotonic() + 40
        while obs.snapshot()["sim_s"] is None or not obs.lead_history or not obs.pose_history:
            if time.monotonic() > deadline:
                raise TimeoutError("Dynamic sensors unavailable")
            time.sleep(0.05)
        for case in config["motion_cases"]:
            xyz = points[case["knots"][0][1]]
            deadline = time.monotonic() + 10
            while True:
                set_pose("queue_lead", xyz)
                actual = obs.snapshot()["poses"].get("queue_lead")
                if actual and actual["age_s"] < 0.5 and math.dist(actual["xyz"], xyz) < 0.001:
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError("Actor reset not observed")
            set_pose("queue_parcel", [*policy["pad_xyz_m"][:2], policy["pad_xyz_m"][2] + 0.15])
            time.sleep(0.5)
            epoch = obs.snapshot()["sim_s"]
            folder = root / case["id"]
            folder.mkdir()
            last_stamp = int(epoch * 1e9)
            last_command = -1
            records = []
            deadline = time.monotonic() + 400
            while obs.snapshot()["sim_s"] - epoch <= case["knots"][-1][0] + 0.5:
                if time.monotonic() > deadline:
                    raise TimeoutError("Dynamic recording deadline")
                t = obs.snapshot()["sim_s"] - epoch
                if t - last_command >= 0.1:
                    set_pose("queue_lead", actor_xyz(case["knots"], t, points))
                    last_command = t
                with obs.lock:
                    stamps = sorted(
                        set(obs.image_history["onboard_rgb"])
                        & set(obs.image_history["onboard_depth"])
                    )
                    frames = [
                        (
                            s,
                            obs.image_history["onboard_rgb"][s][0],
                            obs.image_history["onboard_depth"][s][0],
                        )
                        for s in stamps
                        if s > last_stamp
                    ]
                    rig = list(obs.pose_history)
                    lead = list(obs.lead_history)
                for s, rgb, depth in frames:
                    if s / 1e9 - epoch > case["knots"][-1][0]:
                        continue
                    if (rgb.width, rgb.height, rgb.pixel_format_type, len(rgb.data)) != (
                        640,
                        360,
                        3,
                        640 * 360 * 3,
                    ):
                        raise ValueError("RGB encoding")
                    if (depth.width, depth.height, depth.pixel_format_type, len(depth.data)) != (
                        640,
                        360,
                        13,
                        640 * 360 * 4,
                    ):
                        raise ValueError("Depth encoding")
                    rp = min(rig, key=lambda p: abs(p["sensor_sim_s"] - s / 1e9))
                    lp = min(lead, key=lambda p: abs(p["stamp_s"] - s / 1e9))
                    if max(abs(rp["sensor_sim_s"] - s / 1e9), abs(lp["stamp_s"] - s / 1e9)) > 0.012:
                        continue
                    i = len(records)
                    assets = {}
                    for kind, data in [
                        ("rgb", png_rgb(640, 360, rgb.data)),
                        ("depth", zlib.compress(bytes(depth.data))),
                    ]:
                        name = f"{i:04d}-{kind}." + ("png" if kind == "rgb" else "z")
                        (folder / name).write_bytes(data)
                        assets[kind] = dict(file=name, sha256=hashlib.sha256(data).hexdigest())
                    records.append(
                        dict(
                            index=i,
                            stamp_ns=s,
                            elapsed_sim_s=s / 1e9 - epoch,
                            rig_pose={
                                k: v
                                for k, v in rp.items()
                                if k not in {"raw_pose", "received_monotonic_s"}
                            },
                            lead_pose=lp,
                            assets=assets,
                        )
                    )
                    last_stamp = s
                time.sleep(0.01)
            if len(records) < 100:
                raise ValueError("Incomplete dynamic recording")
            gaps = [(b["stamp_ns"] - a["stamp_ns"]) / 1e9 for a, b in zip(records, records[1:])]
            if max(abs(g - 0.25) for g in gaps) > 0.004000001:
                raise ValueError("Noncontiguous dynamic frames")
            value = dict(
                schema="yokohama_pad_motion_capture.v1",
                case=case,
                epoch_sim_s=epoch,
                frames=records,
                run_id=config["run_id"],
                world_sha256=config["world"]["world_sha256"],
            )
            path = folder / "capture.json"
            path.write_text(json.dumps(value, indent=2) + "\n")
            receipt["cases"].append(
                dict(
                    id=case["id"],
                    frames=len(records),
                    capture_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            )
            print(json.dumps(receipt["cases"][-1]), flush=True)
        receipt["status"] = "passed"
    finally:
        obs.close()
        (root / "capture-result.json").write_text(json.dumps(receipt, indent=2) + "\n")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--approve-sitl", action="store_true")
    p.add_argument("--source-rig", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument(
        "--case-set", choices=["temporal-v1", "learning-v1", "state-eval-v1"], default="temporal-v1"
    )
    a = p.parse_args()
    if not a.approve_sitl:
        p.error("Explicit --approve-sitl required")
    root = a.output_dir.resolve()
    source = a.source_rig.resolve()
    if root.exists():
        p.error("Preserve previous attempts")
    if json.loads((source / "capture-result.json").read_text())["status"] != "passed":
        raise ValueError("Unqualified source rig")
    root.mkdir(parents=True)
    for name in ("assets", "models"):
        shutil.copytree(source / name, root / name, symlinks=True)
    for name in ("yokohama_sitl_worker.py", "ship_urban_camera_worker.py"):
        shutil.copy2(REPO / "scripts" / name, root / name)
    shutil.copy2(__file__, root / Path(__file__).name)
    config = json.loads((source / "config.json").read_text())
    config["run_id"] = "pad-motion-" + uuid4().hex[:12]
    config["motion_cases"] = {
        "temporal-v1": cases,
        "learning-v1": learning_cases,
        "state-eval-v1": state_evaluation_cases,
    }[a.case_set]()
    config["motion_case_set"] = a.case_set
    config["decisions"] = {"backend": "camera-recording-only"}
    near = config["diagnostic_cases"][1]
    # Tilt the static rig, preserving its exact sensor extrinsics. This is an
    # authored observation viewpoint, not a qualified tilted-aircraft AP hold.
    near["camera_xyz"][2] = config["world"]["pad_queue"]["pad_xyz_m"][2] + 10
    pitch = math.atan2(6, 20)  # aim 4 m above the pad, keeping ascent in view
    world_path = root / "models/worlds/default.sdf"
    tree = ET.parse(world_path)
    rig = next(
        m for m in tree.getroot().find("world").findall("model") if m.get("name") == "x500_0"
    )
    rig.find("pose").text = " ".join(map(str, [*near["camera_xyz"], 0, pitch, near["camera_yaw"]]))
    tree.write(world_path, encoding="utf-8", xml_declaration=True)
    config["world"]["world_sha256"] = hashlib.sha256(world_path.read_bytes()).hexdigest()
    config["diagnostic_boundary"] = dict(
        camera_rig=True,
        aircraft_flown=False,
        camera_pitch_rad=pitch,
        source="scripted lead with measured poses; no lead AP",
    )
    (root / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    name = "missionos-" + config["run_id"]
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--network",
                "none",
                "--cpus",
                "4",
                "--memory",
                "4g",
                "--entrypoint",
                "sh",
                "-v",
                str(root) + ":/mission",
                "-e",
                "LIBGL_ALWAYS_SOFTWARE=1",
                "-e",
                "GZ_SIM_RESOURCE_PATH=/mission/models:/opt/px4-gazebo/share/gz/models",
                "px4io/px4-sitl-gazebo:latest",
                "-c",
                "gz sim -r -s --headless-rendering /mission/models/worlds/default.sdf",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        time.sleep(5)
        with (root / "worker.stdout").open("w") as out, (root / "worker.stderr").open("w") as err:
            subprocess.run(
                [
                    "docker",
                    "exec",
                    name,
                    "python3",
                    "-u",
                    "/mission/capture_yokohama_pad_motion.py",
                    "--worker",
                ],
                stdout=out,
                stderr=err,
                check=True,
                timeout=1700,
            )
    finally:
        logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True, timeout=15)
        (root / "gazebo.log").write_text(logs.stdout + logs.stderr)
        subprocess.run(["docker", "rm", "-f", name], check=True, capture_output=True, timeout=30)
        absent = subprocess.run(["docker", "inspect", name], capture_output=True, timeout=15)
        (root / "cleanup.json").write_text(
            json.dumps(dict(owned_container=name, removed=absent.returncode != 0)) + "\n"
        )


if __name__ == "__main__":
    worker() if sys.argv[1:] == ["--worker"] else main()
