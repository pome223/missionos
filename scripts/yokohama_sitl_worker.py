"""Container-local observations for the opt-in Yokohama simulator boundary.

No inference or hardware endpoints. Receipts describe physics and AP observations.
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from gz.msgs10.contacts_pb2 import Contacts
from gz.msgs10.camera_info_pb2 import CameraInfo
from gz.msgs10.image_pb2 import Image
from gz.msgs10.pose_v_pb2 import Pose_V
from gz.msgs10.world_stats_pb2 import WorldStatistics
from gz.transport13 import Node
from ship_urban_camera_worker import png_rgb

ROOT = Path("/mission")
BIN = "/opt/px4-gazebo/bin/px4-"


def run(args, timeout=12):
    p = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(
            "Command failed: " + str(args) + ": " + p.stderr[-500:] + p.stdout[-500:]
        )
    return p.stdout


def field(text, name):
    m = re.search(r"\b" + re.escape(name) + r":\s*([-+0-9.eE]+|True|False)\b", text)
    if not m:
        return None
    v = m.group(1)
    return v == "True" if v in ["True", "False"] else float(v)


def stamp(message):
    return message.header.stamp.sec + message.header.stamp.nsec / 1e9


class Observer:
    def __init__(self, config):
        self.node = Node()
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.sim_s = None
        self.poses = {}
        self.images = {}
        self.contacts = []
        self.camera_info = {}
        self.callbacks = []
        self.topics = []
        self.sensor_file = (ROOT / "sensor-events.jsonl").open("w", buffering=1)
        self.subscribe(Pose_V, "/world/default/pose/info", self.receive_poses)
        self.subscribe(WorldStatistics, "/world/default/stats", self.receive_stats)
        for name in ["city", "launch_pad", "delivery_pad", *config["world"]["probes"]]:
            model = "yokohama_city" if name == "city" else name
            self.subscribe(
                Contacts,
                f"/world/default/model/{model}/link/link/sensor/{name}/contact",
                lambda m, n=name: self.receive_contacts(m, n),
            )
        self.subscribe(CameraInfo, "/yokohama/scene/camera_info", self.receive_camera_info)
        for key, topic in [
            ("scene_rgb", "/yokohama/scene/image"),
            ("scene_depth", "/yokohama/scene/depth_image"),
            ("onboard_rgb", "/yokohama/onboard/image"),
            ("onboard_depth", "/yokohama/onboard/depth_image"),
            ("down_rgb", "/yokohama/down"),
        ]:
            self.subscribe(Image, topic, lambda m, k=key: self.receive_image(m, k))

    def subscribe(self, cls, topic, callback):
        self.callbacks.append(callback)
        self.topics.append(topic)
        if not self.node.subscribe(cls, topic, callback):
            raise RuntimeError("Subscription rejected: " + topic)

    def close(self):
        for topic in self.topics:
            self.node.unsubscribe(topic)
        self.node = None
        time.sleep(0.1)
        self.sensor_file.close()

    def receive_stats(self, m):
        with self.lock:
            self.sim_s = m.sim_time.sec + m.sim_time.nsec / 1e9

    def receive_poses(self, m):
        now = time.monotonic()
        with self.lock:
            for p in m.pose:
                self.poses[p.name] = {
                    "id": p.id,
                    "xyz": [p.position.x, p.position.y, p.position.z],
                    "quat_wxyz": [
                        p.orientation.w,
                        p.orientation.x,
                        p.orientation.y,
                        p.orientation.z,
                    ],
                    "sensor_sim_s": stamp(m),
                    "received_monotonic_s": now,
                }

    def receive_camera_info(self, m):
        with self.lock:
            self.camera_info = {
                "width": m.width,
                "height": m.height,
                "k": list(m.intrinsics.k),
                "sensor_sim_s": stamp(m),
            }

    def receive_image(self, m, key):
        with self.lock:
            own = Image()
            own.CopyFrom(m)
            self.images[key] = (own, time.monotonic())

    def receive_contacts(self, m, name):
        if not m.contact:
            return
        with self.lock:
            for c in m.contact:
                row = {
                    "topic": name,
                    "sensor_sim_s": stamp(m),
                    "wall_elapsed_s": time.monotonic() - self.started,
                    "collision1": c.collision1.name,
                    "collision2": c.collision2.name,
                    "depth": list(c.depth),
                }
                self.contacts.append(row)
                self.sensor_file.write(json.dumps(row) + "\n")

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            poses = {
                k: dict(v, age_s=now - v["received_monotonic_s"]) for k, v in self.poses.items()
            }
            return {
                "sim_s": self.sim_s,
                "wall_elapsed_s": now - self.started,
                "poses": poses,
                "contacts_seen": len(self.contacts),
            }

    def images_to_disk(self, label, required):
        result = {}
        folder = ROOT / "images"
        folder.mkdir(exist_ok=True)
        with self.lock:
            images = dict(self.images)
        for key in required:
            if key not in images:
                raise RuntimeError("Image not received: " + key)
            m, received = images[key]
            if time.monotonic() - received > 3:
                raise RuntimeError("Stale image: " + key)
            if (m.width, m.height) != (640, 360):
                raise RuntimeError("Wrong image dimensions")
            if key.endswith("rgb"):
                if m.pixel_format_type != 3 or len(m.data) != m.width * m.height * 3:
                    raise RuntimeError("Invalid RGB image")
                data = png_rgb(m.width, m.height, m.data)
                name = label + "-" + key + ".png"
                extra = {}
            else:
                if len(m.data) != m.width * m.height * 4:
                    raise RuntimeError("Invalid depth payload")
                data = bytes(m.data)
                name = label + "-" + key + ".f32"
                values = array.array("f", data)
                finite = [x for x in values if math.isfinite(x) and 0.1 < x < 800]
                extra = {
                    "finite_fraction": len(finite) / len(values),
                    "finite_min_m": min(finite) if finite else None,
                    "finite_max_m": max(finite) if finite else None,
                    "encoding": "little-endian float32, axial camera depth",
                }
                if len(finite) < 1000:
                    raise RuntimeError("Depth scene is empty")
            (folder / name).write_bytes(data)
            result[key] = {
                "file": "images/" + name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "width": m.width,
                "height": m.height,
                "pixel_format": m.pixel_format_type,
                "sensor_sim_s": stamp(m),
                **extra,
            }
        (ROOT / (label + "-images.json")).write_text(json.dumps(result, indent=2) + "\n")
        return result


def contact_trial(config, obs):
    time.sleep(1)
    run(
        [
            "gz",
            "service",
            "-s",
            "/world/default/control",
            "--reqtype",
            "gz.msgs.WorldControl",
            "--reptype",
            "gz.msgs.Boolean",
            "--timeout",
            "10000",
            "--req",
            "pause: false",
        ]
    )
    deadline = time.monotonic() + 90
    rows = []
    start_sim = None
    with (ROOT / "probe-trajectory.jsonl").open("w", buffering=1) as log:
        while time.monotonic() < deadline:
            row = obs.snapshot()
            if row["sim_s"] is not None and all(
                n in row["poses"] for n in config["world"]["probes"]
            ):
                if start_sim is None:
                    start_sim = row["sim_s"]
                rows.append(row)
                log.write(json.dumps(row) + "\n")
                if row["sim_s"] - start_sim >= 10:
                    break
            time.sleep(0.15)
        else:
            raise TimeoutError("Contact trial did not receive 10 seconds of simulator observations")
    images = obs.images_to_disk("scene", ["scene_rgb", "scene_depth"])
    (ROOT / "camera-info.json").write_text(json.dumps(obs.camera_info, indent=2) + "\n")
    results = {}
    for name, p in config["world"]["probes"].items():
        contacts = [c for c in obs.contacts if name in c["collision1"] or name in c["collision2"]]
        expected = p["expected_collision"]
        matches = [
            c
            for c in contacts
            if expected and (expected in c["collision1"] or expected in c["collision2"])
        ]
        final = rows[-1]["poses"][name]
        check = {
            "expected_collision": expected,
            "contact_count": len(contacts),
            "matched_contact_count": len(matches),
            "initial": rows[0]["poses"][name],
            "final": final,
            "passed": bool(matches) if expected else not contacts,
        }
        if name == "ground_probe":
            check["rest_z_error_m"] = abs(final["xyz"][2] - p["expected_rest_z_m"])
            tail = [r["poses"][name]["xyz"][2] for r in rows if r["sim_s"] >= rows[-1]["sim_s"] - 2]
            check["tail_z_range_m"] = max(tail) - min(tail)
            check["passed"] &= check["rest_z_error_m"] < 0.08 and check["tail_z_range_m"] < 0.02
        if not expected:
            check["position_drift_m"] = math.dist(final["xyz"], p["initial_world_xyz_m"])
            check["passed"] &= check["position_drift_m"] < 0.02
        results[name] = check
    return {
        "status": "passed" if all(x["passed"] for x in results.values()) else "failed",
        "cases": results,
        "simulation_duration_s": rows[-1]["sim_s"] - rows[0]["sim_s"],
        "images": images,
        "gazebo_runtime_invoked": True,
        "px4_runtime_invoked": False,
        "vla_invoked": False,
        "wam_invoked": False,
        "physical_execution_invoked": False,
    }


def main():
    config = json.loads((ROOT / "config.json").read_text())
    obs = Observer(config)
    try:
        if config["phase"] == "contacts":
            result = contact_trial(config, obs)
        else:
            from yokohama_flight_worker import flight_trial

            result = flight_trial(config, obs, run, field)
    except Exception as exc:
        result = {"status": "failed", "reason": type(exc).__name__ + ": " + str(exc)}
    finally:
        obs.close()
    result.update(run_id=config["run_id"], world_sha256=config["world"]["world_sha256"])
    (ROOT / "worker-result.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps(result), flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
