"""Container-only stationary sensor recorder; never starts PX4 or models."""

from collections import deque
import hashlib
import json
import math
from pathlib import Path
import subprocess
import threading
import time

import numpy as np
from gz.msgs10.image_pb2 import Image
from gz.msgs10.pose_v_pb2 import Pose_V
from gz.transport13 import Node
from ship_urban_camera_worker import png_rgb

ROOT = Path("/mission")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stamp(m):
    return m.header.stamp.sec + m.header.stamp.nsec / 1e9


def pose_matrix(p):
    w, x, y, z = p["quat"]
    r = np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )
    ned = np.array([[0, 1, 0], [1, 0, 0], [0, 0, -1]])
    optical = np.array([[0, 0, 1], [-1, 0, 0], [0, -1, 0]])
    out = np.eye(4)
    out[:3, :3] = ned @ r @ optical
    out[:3, 3] = ned @ p["xyz"]
    return out


class Recorder:
    def __init__(self):
        self.node = Node()
        self.lock = threading.Lock()
        self.images = {"rgb": {}, "depth": {}}
        self.poses = deque(maxlen=8000)
        self.callbacks = []
        for kind, topic in [("rgb", "/adapt/image"), ("depth", "/adapt/depth_image")]:

            def callback(m, k=kind):
                self.image(m, k)

            self.callbacks.append(callback)
            assert self.node.subscribe(Image, topic, callback)
        self.callbacks.append(self.pose)
        # SceneBroadcaster timestamps the Pose_V envelope. PosePublisher in this
        # pinned build leaves that envelope at zero, so it cannot qualify joins.
        assert self.node.subscribe(Pose_V, "/world/default/pose/info", self.pose)

    def image(self, m, kind):
        if (m.width, m.height, m.pixel_format_type) != (640, 360, 3 if kind == "rgb" else 13):
            return
        with self.lock:
            self.images[kind][stamp(m)] = bytes(m.data)
            while len(self.images[kind]) > 100:
                del self.images[kind][min(self.images[kind])]

    def pose(self, m):
        with self.lock:
            for p in m.pose:
                if p.name == "adapt_camera":
                    self.poses.append(
                        dict(
                            t=stamp(m),
                            xyz=[p.position.x, p.position.y, p.position.z],
                            quat=[
                                p.orientation.w,
                                p.orientation.x,
                                p.orientation.y,
                                p.orientation.z,
                            ],
                        )
                    )

    def move(self, xyz, yaw):
        request = f'name: "adapt_camera", position: {{x: {xyz[0]}, y: {xyz[1]}, z: {xyz[2]}}}, orientation: {{w: {math.cos(yaw / 2)}, z: {math.sin(yaw / 2)}}}'
        p = subprocess.run(
            [
                "gz",
                "service",
                "-s",
                "/world/default/set_pose",
                "--reqtype",
                "gz.msgs.Pose",
                "--reptype",
                "gz.msgs.Boolean",
                "--timeout",
                "10000",
                "--req",
                request,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        if "data: true" not in p.stdout:
            raise ValueError("Pose command was not acknowledged")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with self.lock:
                latest = self.poses[-1] if self.poses else None
            if latest and np.linalg.norm(np.array(latest["xyz"]) - xyz) < 0.001:
                observed = 2 * math.atan2(latest["quat"][3], latest["quat"][0])
                if abs(math.remainder(observed - yaw, 2 * math.pi)) < 0.001:
                    return latest["t"] + 0.5
            time.sleep(0.05)
        raise TimeoutError("Camera pose not observed")

    def capture(self, after, count):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            with self.lock:
                stamps = sorted(set(self.images["rgb"]) & set(self.images["depth"]))
                stamps = [t for t in stamps if t > after][:count]
                if len(stamps) == count and self.poses and self.poses[-1]["t"] >= stamps[-1]:
                    rows = []
                    for t in stamps:
                        p = min(self.poses, key=lambda p: abs(p["t"] - t))
                        if abs(p["t"] - t) > 0.012:
                            raise ValueError("Camera/pose join exceeds 12 ms")
                        rows.append(
                            dict(
                                t=t,
                                pose=p,
                                rgb=self.images["rgb"][t],
                                depth=self.images["depth"][t],
                            )
                        )
                    return rows
            time.sleep(0.05)
        with self.lock:
            diagnostic = dict(
                after=after,
                count=count,
                image_stamps={k: list(v)[-5:] for k, v in self.images.items()},
                poses=list(self.poses)[-2:],
            )
        (ROOT / "capture-timeout.json").write_text(json.dumps(diagnostic, indent=2) + "\n")
        raise TimeoutError("RGBD pair capture incomplete")


def main():
    plan = json.loads((ROOT / "plan.json").read_text())
    (ROOT / "inputs").mkdir()
    (ROOT / "targets").mkdir()
    recorder = Recorder()
    time.sleep(2)
    samples = []
    for site in plan["sites"]:
        after = recorder.move(site["xyz"], site["yaw_enu_rad"])
        frames = recorder.capture(after, 20)
        if np.max(np.abs(np.diff([f["t"] for f in frames]) - 0.25)) > 0.004:
            raise ValueError("Context timing is not 4 Hz")
        history = frames[:16]
        poses = np.array([pose_matrix(f["pose"]) for f in history])
        fx = 640 / (2 * math.tan(math.pi / 6))
        path = ROOT / "inputs" / (site["id"] + ".npz")
        np.savez_compressed(
            path,
            rgb=np.array([np.frombuffer(f["rgb"], np.uint8).reshape(360, 640, 3) for f in history]),
            depth=np.array([np.frombuffer(f["depth"], "<f4").reshape(360, 640) for f in history]),
            poses=poses,
            intrinsics=np.array([[fx, 0, 320], [0, fx, 180], [0, 0, 1]]),
            stamps_s=np.array([f["t"] for f in history]),
        )
        endpoint_after = recorder.move(site["endpoint_xyz"], site["yaw_enu_rad"])
        endpoint = recorder.capture(endpoint_after, 1)[0]
        for action, f, delta in [
            ("hold", frames[19], [0, 0, 0, 0]),
            ("forward", endpoint, [2.8, 0, 0, 0]),
        ]:
            sample_id = site["id"] + "-" + action
            target = ROOT / "targets" / (sample_id + ".png")
            target.write_bytes(png_rgb(640, 360, f["rgb"]))
            expected = poses[-1].copy()
            expected[:3, 3] += expected[:3, 2] * delta[0]
            actual = pose_matrix(f["pose"])
            position_error = float(np.linalg.norm(actual[:3, 3] - expected[:3, 3]))
            rotation_error = float(
                np.arccos(np.clip((np.trace(actual[:3, :3].T @ expected[:3, :3]) - 1) / 2, -1, 1))
            )
            samples.append(
                dict(
                    id=sample_id,
                    site=site["id"],
                    split=site["split"],
                    action=action,
                    delta=delta,
                    time_index=1,
                    input=dict(file=str(path.relative_to(ROOT)), sha256=sha(path)),
                    target=dict(file=str(target.relative_to(ROOT)), sha256=sha(target)),
                    input_cutoff_sim_s=history[-1]["t"],
                    target_sim_s=f["t"],
                    position_error_m=position_error,
                    rotation_error_rad=rotation_error,
                )
            )
        (ROOT / "manifest.json").write_text(
            json.dumps({**plan, "samples": samples}, indent=2) + "\n"
        )
        print(json.dumps(dict(site=site["id"], count=len(samples))), flush=True)


if __name__ == "__main__":
    main()
