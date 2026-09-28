#!/usr/bin/env python3
"""Opt-in CPU Gazebo camera diagnostic; no AP flight or learned inference.

Reuses a qualified occupied-pad world's geometry. A static camera rig has the
same front RGBD/down-camera transforms as the aircraft. Its pose is authored,
not an observed aircraft arrival. Actor poses are observed before capture.
"""

from __future__ import annotations

import argparse
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

REPO = Path(__file__).resolve().parents[1]


def worker():
    from gz.msgs10.boolean_pb2 import Boolean
    from gz.msgs10.pose_pb2 import Pose
    from yokohama_sitl_worker import Observer

    root = Path("/mission")
    config = json.loads((root / "config.json").read_text())
    obs = Observer(config)
    receipt = {"status": "failed", "captures": [], "ap_flight": False}

    def pose(name, xyz, yaw=0):
        if name not in {"x500_0", "queue_lead", "queue_parcel"}:
            raise ValueError("Unowned camera diagnostic entity")
        msg = Pose()
        msg.name = name
        msg.position.x, msg.position.y, msg.position.z = xyz
        msg.orientation.w, msg.orientation.z = math.cos(yaw / 2), math.sin(yaw / 2)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            obs.node.request("/world/default/set_pose", msg, Pose, Boolean, 100)
            time.sleep(0.1)
            actual = obs.snapshot()["poses"].get(name)
            if actual and actual["age_s"] < 0.5 and math.dist(actual["xyz"], xyz) < 0.001:
                return actual
        raise TimeoutError("Pose not observed: " + name)

    try:
        deadline = time.monotonic() + 30
        while obs.snapshot()["sim_s"] is None or "x500_0" not in obs.snapshot()["poses"]:
            if time.monotonic() > deadline:
                raise TimeoutError("Camera rig unavailable")
            time.sleep(0.1)
        for case in config["diagnostic_cases"]:
            rig = pose("x500_0", case["camera_xyz"], case["camera_yaw"])
            lead = pose("queue_lead", case["lead_xyz"])
            pose("queue_parcel", config["world"]["pad_queue"]["pad_xyz_m"])
            cutoff = obs.snapshot()["sim_s"] + 1
            deadline = time.monotonic() + 40
            capture = None
            while capture is None:
                if time.monotonic() > deadline:
                    raise TimeoutError("RGBD history incomplete: " + case["id"])
                time.sleep(0.1)
                capture = obs.capture_history(case["id"], cutoff)
            receipt["captures"].append(
                dict(case_id=case["id"], capture=capture, rig_pose=rig, lead_pose=lead)
            )
            (root / "capture-result.json").write_text(json.dumps(receipt, indent=2) + "\n")
        receipt["status"] = "passed"
    finally:
        obs.close()
        (root / "capture-result.json").write_text(json.dumps(receipt, indent=2) + "\n")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--approve-sitl", action="store_true")
    p.add_argument("--source-run", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    a = p.parse_args()
    if not a.approve_sitl:
        p.error("Explicit --approve-sitl required")
    source, root = a.source_run.resolve(), a.output_dir.resolve()
    if root.exists():
        p.error("Preserve attempts: output directory must not exist")
    for name in ("verification.json", "pad-verification.json", "payload-verification.json"):
        if json.loads((source / name).read_text())["status"] != "passed":
            raise ValueError("Source full-flight evidence failed: " + name)
    sys.path.insert(0, str(REPO))
    from src.runtime.yokohama_scene import camera, to_source
    from shapely.geometry import LineString, shape

    root.mkdir(parents=True)
    for name in ("assets", "models"):
        shutil.copytree(source / name, root / name, symlinks=True)
    for name in ("yokohama_sitl_worker.py", "ship_urban_camera_worker.py"):
        shutil.copy2(REPO / "scripts" / name, root / name)
    shutil.copy2(__file__, root / "capture_yokohama_pad_views.py")
    config = json.loads((source / "config.json").read_text())
    config["source_run_id"] = config["run_id"]
    config["run_id"] = "pad-views-" + uuid4().hex[:12]
    config["decisions"] = {"backend": "capture-only; no inference"}
    policy = config["world"]["pad_queue"]
    pad, far = policy["pad_xyz_m"], policy["wait_xyz_m"]
    d = math.dist(pad[:2], far[:2])
    near = [pad[i] + (far[i] - pad[i]) * 20 / d for i in (0, 1)] + [pad[2] + 5]
    yaw = math.atan2(pad[1] - near[1], pad[0] - near[0])
    features = json.loads(
        (REPO / "docs/examples/yokohama-urban-scene/collision-footprints.geojson").read_text()
    )["features"]
    line = LineString(to_source([far, near, [*near[:2], far[2]]], config["world"]["frame"])[:, :2])
    clearance = min(line.distance(shape(f["geometry"])) for f in features)
    if clearance <= 3 or math.dist(near, policy["lead_start_xyz_m"]) < 10:
        raise ValueError("Camera inspection location violates the authored clearance screen")
    config["diagnostic_cases"] = [
        dict(id="far-busy", camera_xyz=far, camera_yaw=yaw, lead_xyz=policy["lead_start_xyz_m"]),
        dict(id="near-busy", camera_xyz=near, camera_yaw=yaw, lead_xyz=policy["lead_start_xyz_m"]),
        dict(id="near-clear", camera_xyz=near, camera_yaw=yaw, lead_xyz=policy["lead_end_xyz_m"]),
        dict(id="near-reoccupied", camera_xyz=near, camera_yaw=yaw, lead_xyz=policy["lead_start_xyz_m"]),
    ]
    config["diagnostic_boundary"] = dict(
        camera_rig=True, aircraft_flown=False, native_models=False,
        minimum_mapped_camera_connector_clearance_m=clearance,
        purpose="Fixed-weight perception/action probe before native flight integration",
    )
    world_path = root / "models/worlds/default.sdf"
    tree = ET.parse(world_path)
    world = tree.getroot().find("world")
    for m in list(world.findall("model")):
        if m.attrib.get("name") == "x500_0":
            world.remove(m)
    rig = ET.SubElement(world, "model", name="x500_0")
    ET.SubElement(rig, "static").text = "true"
    ET.SubElement(rig, "pose").text = " ".join(map(str, [*far, 0, 0, yaw]))
    link = ET.SubElement(rig, "link", name="base_link")
    camera(link, "urban_rgbd", [.25, 0, .1], [0, 0, 0], "/yokohama/onboard", rgbd=True, rate_hz=4)
    camera(link, "urban_down", [.25, 0, .1], [0, math.pi / 2, 0], "/yokohama/down", rate_hz=4)
    plugin = ET.SubElement(rig, "plugin", filename="gz-sim-pose-publisher-system", name="gz::sim::systems::PosePublisher")
    for key, value in [("publish_model_pose", "true"), ("publish_link_pose", "false"), ("use_pose_vector_msg", "true"), ("update_frequency", "250"), ("topic", "/world/default/pose/info")]:
        ET.SubElement(plugin, key).text = value
    tree.write(world_path, encoding="utf-8", xml_declaration=True)
    config["world"]["world_sha256"] = hashlib.sha256(world_path.read_bytes()).hexdigest()
    (root / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    name = "missionos-" + config["run_id"]
    try:
        subprocess.run([
            "docker", "run", "-d", "--name", name, "--network", "none", "--cpus", "4",
            "--memory", "4g", "--entrypoint", "sh", "-v", str(root) + ":/mission",
            "-e", "LIBGL_ALWAYS_SOFTWARE=1", "-e",
            "GZ_SIM_RESOURCE_PATH=/mission/models:/opt/px4-gazebo/share/gz/models",
            "px4io/px4-sitl-gazebo:latest", "-c",
            "gz sim -r -s --headless-rendering /mission/models/worlds/default.sdf",
        ], check=True, capture_output=True, timeout=30)
        time.sleep(5)
        with (root / "worker.stdout").open("w") as out, (root / "worker.stderr").open("w") as err:
            subprocess.run(["docker", "exec", name, "python3", "-u", "/mission/capture_yokohama_pad_views.py", "--worker"], stdout=out, stderr=err, check=True, timeout=210)
    finally:
        log = subprocess.run(["docker", "logs", name], capture_output=True, text=True, timeout=15)
        (root / "gazebo.log").write_text(log.stdout + log.stderr)
        subprocess.run(["docker", "rm", "-f", name], check=True, capture_output=True, timeout=30)
        probe = subprocess.run(["docker", "inspect", name], capture_output=True, timeout=15)
        (root / "cleanup.json").write_text(json.dumps({"owned_container": name, "removed": probe.returncode != 0}) + "\n")


if __name__ == "__main__":
    if sys.argv[1:] == ["--worker"]:
        worker()
    else:
        main()
