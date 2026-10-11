"""Container-local observations for the opt-in Yokohama simulator boundary.

No inference or hardware endpoints. Receipts describe physics and AP observations.
"""

from __future__ import annotations

import array
from collections import deque
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
from gz.msgs10.stringmsg_pb2 import StringMsg
from gz.msgs10.wind_pb2 import Wind
from gz.msgs10.empty_pb2 import Empty
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
        self.config = config
        self.node = Node()
        self.wind_publisher = (
            self.node.advertise("/world/default/wind", Wind)
            if config["world"].get("wind", {}).get("after_takeoff")
            else None
        )
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.sim_s = None
        self.poses = {}
        self.images = {}
        self.decision_capture = bool(config.get("decisions") or config.get("motion_capture"))
        self.motion = None
        self.onboard_camera_info = {}
        if config.get("motion_capture"):
            from yokohama_motion_recorder import MotionRecorder

            self.motion = MotionRecorder(ROOT, config["motion_capture"])
        self.image_history = {k: {} for k in ("onboard_rgb", "onboard_depth", "down_rgb")}
        self.pose_history = deque(maxlen=5000)
        self.contacts = []
        self.payload_joint = None
        self.joint_file = None
        self.camera_info = {}
        self.callbacks = []
        self.topics = []
        self.sensor_file = (ROOT / "sensor-events.jsonl").open("w", buffering=1)
        self.recovery_pose_file = (
            (ROOT / "recovery-poses.jsonl").open("w", buffering=1)
            if config.get("candidate_recovery") or config.get("delivery_trial") else None)
        self.last_recovery_pose_sim_s = -1
        self.subscribe(Pose_V, "/world/default/pose/info", self.receive_poses)
        self.subscribe(WorldStatistics, "/world/default/stats", self.receive_stats)
        if config["world"].get("payload_delivery"):
            self.joint_file = (ROOT / "payload-joint-events.jsonl").open("w", buffering=1)
            self.subscribe(
                StringMsg, config["world"]["payload_delivery"]["joint_topic"], self.receive_joint
            )
        for name in ["city", "launch_pad", "delivery_pad", *config["world"]["probes"]]:
            model = "yokohama_city" if name == "city" else name
            self.subscribe(
                Contacts,
                f"/world/default/model/{model}/link/link/sensor/{name}/contact",
                lambda m, n=name: self.receive_contacts(m, n),
            )
        self.subscribe(CameraInfo, "/yokohama/scene/camera_info", self.receive_camera_info)
        if self.motion:
            self.subscribe(CameraInfo, "/yokohama/onboard/camera_info", self.receive_onboard_info)
        for key, topic in [
            ("scene_rgb", "/yokohama/scene/image"),
            ("scene_depth", "/yokohama/scene/depth_image"),
            ("onboard_rgb", "/yokohama/onboard/image"),
            ("onboard_depth", "/yokohama/onboard/depth_image"),
            ("down_rgb", "/yokohama/down"),
            *([("delivery_rgb", "/yokohama/delivery")] if self.joint_file else []),
            *([("queue_rgb", "/yokohama/queue")] if config["world"].get("pad_queue") else []),
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
        if self.recovery_pose_file:
            self.recovery_pose_file.close()
        if self.joint_file:
            self.joint_file.close()
        if self.motion:
            self.motion.close(self.onboard_camera_info)

    def activate_wind(self, velocity):
        if self.wind_publisher is None:
            raise RuntimeError("Wind activation not configured")
        msg = Wind()
        msg.enable_wind = True
        msg.linear_velocity.x, msg.linear_velocity.y, msg.linear_velocity.z = velocity
        start = self.snapshot()["sim_s"]
        deadline = time.monotonic() + 3
        confirmed = False
        attempts = 0
        while time.monotonic() < deadline:
            # Repeating the same seed is idempotent. A publish return value or
            # elapsed time never substitutes for observed service confirmation.
            self.wind_publisher.publish(msg)
            attempts += 1
            time.sleep(0.2)
            okay, response = self.node.request(
                "/world/default/wind_info", Empty(), Empty, Wind, 500
            )
            confirmed = bool(
                okay
                and response.enable_wind
                and [
                    response.linear_velocity.x,
                    response.linear_velocity.y,
                    response.linear_velocity.z,
                ]
                == velocity
            )
            if confirmed:
                break
        return dict(
            start_sim_s=start,
            end_sim_s=self.snapshot()["sim_s"],
            confirmed=confirmed,
            requested_enu_mps=velocity,
            confirmation_attempts=attempts,
        )

    def receive_stats(self, m):
        with self.lock:
            self.sim_s = m.sim_time.sec + m.sim_time.nsec / 1e9

    def receive_joint(self, m):
        with self.lock:
            self.payload_joint = dict(
                state=m.data,
                observed_sim_s=self.sim_s,
                wall_elapsed_s=time.monotonic() - self.started,
            )
            self.joint_file.write(json.dumps(self.payload_joint) + "\n")

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
                if p.name == "x500_0" and self.decision_capture:
                    self.pose_history.append(
                        dict(self.poses[p.name], raw_pose=p.SerializeToString())
                    )
                if (p.name == "x500_0" and self.recovery_pose_file
                        and stamp(m) - self.last_recovery_pose_sim_s >= 0.1):
                    self.recovery_pose_file.write(json.dumps({
                        "run_id": self.config["run_id"], "sensor_sim_s": stamp(m),
                        "xyz": [p.position.x, p.position.y, p.position.z],
                        "quat_wxyz": [p.orientation.w, p.orientation.x,
                                      p.orientation.y, p.orientation.z],
                        "source_topic": "/world/default/pose/info",
                    }) + "\n")
                    self.last_recovery_pose_sim_s = stamp(m)

    def receive_camera_info(self, m):
        with self.lock:
            self.camera_info = {
                "width": m.width,
                "height": m.height,
                "k": list(m.intrinsics.k),
                "sensor_sim_s": stamp(m),
            }

    def receive_onboard_info(self, m):
        with self.lock:
            self.onboard_camera_info = dict(
                width=m.width, height=m.height, k=list(m.intrinsics.k), sensor_sim_s=stamp(m)
            )

    def record_motion(self, row):
        if self.motion:
            with self.lock:
                histories = {k: dict(v) for k, v in self.image_history.items()}
                poses = list(self.pose_history)
            self.motion.append(histories, poses, row)

    def receive_image(self, m, key):
        with self.lock:
            own = Image()
            own.CopyFrom(m)
            self.images[key] = (own, time.monotonic())
            if self.decision_capture and key in self.image_history:
                history = self.image_history[key]
                history[m.header.stamp.sec * 1_000_000_000 + m.header.stamp.nsec] = (
                    own,
                    time.monotonic(),
                )
                while len(history) > 100:
                    del history[min(history)]

    def capture_history(self, label, after_sim_s, *, evaluation_only=False):
        """Exact RGB/depth/down joins; nearest measured pose, never interpolation."""
        with self.lock:
            stamps = sorted(set.intersection(*(set(h) for h in self.image_history.values())))
            stamps = [s for s in stamps if s / 1e9 > after_sim_s][:24]
            if len(stamps) < 24:
                return None
            if evaluation_only and stamps[0] / 1e9 - after_sim_s > 0.254000001:
                raise ValueError("Evaluation capture lost the first post-cutoff frame")
            if any(abs((b - a) / 1e9 - 0.25) > 0.004000001 for a, b in zip(stamps, stamps[1:])):
                raise ValueError("Urban history is not uninterrupted 4 Hz")
            frames = [{k: h[s] for k, h in self.image_history.items()} for s in stamps]
            poses = list(self.pose_history)
        folder = ROOT / label
        folder.mkdir()
        result = []
        for index, (s, frameset) in enumerate(zip(stamps, frames)):
            pose = min(poses, key=lambda p: abs(p["sensor_sim_s"] - s / 1e9))
            if abs(pose["sensor_sim_s"] - s / 1e9) > 0.012:
                raise ValueError("RGBD/Gazebo pose join exceeds 12 ms")
            assets = {}
            for key, (message, received) in frameset.items():
                rgb = key != "onboard_depth"
                if (
                    message.width,
                    message.height,
                    message.pixel_format_type,
                    len(message.data),
                ) != (640, 360, 3 if rgb else 13, 640 * 360 * (3 if rgb else 4)):
                    raise ValueError("Urban sensor encoding mismatch")
                for suffix, data in [
                    ("raw", bytes(message.data)),
                    ("pb", message.SerializeToString()),
                ]:
                    name = f"{index:02d}-{key}.{suffix}"
                    (folder / name).write_bytes(data)
                    assets[key + "_" + suffix] = {
                        "file": name,
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                if rgb:
                    name = f"{index:02d}-{key}.png"
                    data = png_rgb(640, 360, message.data)
                    (folder / name).write_bytes(data)
                    assets[key + "_png"] = {
                        "file": name,
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
            pose_file = f"{index:02d}-vehicle.pb"
            (folder / pose_file).write_bytes(pose["raw_pose"])
            assets["pose_pb"] = {
                "file": pose_file,
                "sha256": hashlib.sha256(pose["raw_pose"]).hexdigest(),
            }
            result.append(
                dict(
                    stamp_ns=s,
                    pose={k: v for k, v in pose.items() if k != "raw_pose"},
                    assets=assets,
                )
            )
        capture = {
            "schema_version": (
                "yokohama_rgbd_evaluation.v1" if evaluation_only else "yokohama_rgbd_history.v1"
            ),
            "frames": result,
            "startup_indices": [] if evaluation_only else list(range(8)),
            "history_indices": [] if evaluation_only else list(range(8, 24)),
            "future_frames_included": evaluation_only,
            "evaluation_only": evaluation_only,
        }
        (folder / "capture.json").write_text(json.dumps(capture, indent=2) + "\n")
        return {
            "file": label + "/capture.json",
            "sha256": hashlib.sha256((folder / "capture.json").read_bytes()).hexdigest(),
        }

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
                "payload_joint": self.payload_joint,
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
