"""Container-local, timestamp-joined RGBD capture for an offline native-WAM audit.

Twenty real frames: the first sixteen are historical input; the last four are
held-out outcomes. This collector never moves the aircraft or invokes a model.
"""

import hashlib
import json
import threading
import time
import subprocess
from pathlib import Path

STARTUP_WARMUP_BUNDLES = 8


class NativeHistoryBridge:
    """Keep high-frequency transport callbacks separate from PX4 log parsing."""

    def __init__(self, root, config):
        self.root, self.config, self.process = root, config, None

    def start(self):
        self.log = (self.root / "native-capture.log").open("w")
        self.process = subprocess.Popen(
            ["python3", str(self.root / "ship_anwm_capture.py"), str(self.root)],
            stdout=self.log,
            stderr=self.log,
        )

    def finish(self, sample, event):
        deadline = time.monotonic() + 95
        try:
            while time.monotonic() < deadline:
                state = sample()
                if state.get("nav_state") != 4 or state.get("arming_state") != 2:
                    raise RuntimeError("Native history requires armed PX4 auto-loiter")
                if self.process.poll() is not None:
                    if self.process.returncode != 0:
                        raise RuntimeError("Native capture failed; see native-capture.log")
                    event("native_history_captured", frame_count=20, model_invoked=False)
                    return
                time.sleep(0.1)
            raise TimeoutError("Native capture subprocess timed out")
        finally:
            if self.process.poll() is None:
                self.process.terminate()
                self.process.wait(timeout=5)
            self.log.close()


class NativeHistory:
    def __init__(self, root, config):
        from gz.msgs10.camera_info_pb2 import CameraInfo
        from gz.msgs10.image_pb2 import Image
        from gz.msgs10.pose_v_pb2 import Pose_V
        from gz.transport13 import Node

        self.root = root / "native-capture"
        self.root.mkdir()
        self.config = config
        self.lock = threading.Lock()
        self.active = False
        self.bundles = {}
        self.frames = []
        self.warmup_frames = []
        self.last_complete_stamp_ns = -1
        self.error = None
        self.node = Node()
        for kind, message, topic in (
            ("rgb", Image, "/ship/anwm/image"),
            ("depth", Image, "/ship/anwm/depth_image"),
            ("info", CameraInfo, "/ship/anwm/camera_info"),
            ("down", Image, "/ship/aerovla/down"),
            ("pose", Pose_V, "/model/x500_0/pose"),
        ):
            if not self.node.subscribe(message, topic, lambda m, k=kind: self.receive(k, m)):
                raise RuntimeError("Native camera subscription failed: " + topic)

    def start(self):
        with self.lock:
            self.bundles.clear()
            self.active = True

    def receive(self, kind, message):
        with self.lock:
            if not self.active or len(self.frames) == 20 or self.error:
                return
            try:
                header = (
                    next(p.header for p in message.pose if p.name == "x500_0")
                    if kind == "pose"
                    else message.header
                )
                stamp = header.stamp.sec * 10**9 + header.stamp.nsec
                # Gazebo may emit an immediate off-grid image when a subscriber
                # connects. Admit only the established 4 Hz sensor schedule.
                if kind != "pose" and stamp % 250_000_000 > 4_000_000:
                    return
                if stamp <= self.last_complete_stamp_ns:
                    return
                bundle = self.bundles.setdefault(stamp, {})
                bundle[kind] = (message, time.time())
                # Pose messages are frequent. Keep a bounded join buffer.
                for old in sorted(self.bundles)[:-600]:
                    del self.bundles[old]
                if set(bundle) != {"rgb", "depth", "info", "pose", "down"}:
                    return
                rgb, depth, info, poses = [bundle[k][0] for k in ("rgb", "depth", "info", "pose")]
                if any(m.width != 640 or m.height != 360 for m in (rgb, depth, info)):
                    raise ValueError("Unexpected native camera dimensions")
                if (
                    rgb.pixel_format_type != 3
                    or rgb.step != 640 * 3
                    or len(rgb.data) != 640 * 360 * 3
                    or depth.pixel_format_type != 13
                    or depth.step != 640 * 4
                    or len(depth.data) != 640 * 360 * 4
                ):
                    raise ValueError("Unexpected RGB/depth pixel contract")
                vehicle = next(p for p in poses.pose if p.name == "x500_0")
                down = bundle["down"][0]
                if (down.width, down.height, down.step, down.pixel_format_type, len(down.data)) != (
                    640,
                    360,
                    1920,
                    3,
                    691200,
                ):
                    raise ValueError("Unexpected downward camera layout")
                row = {
                    "simulation_time_ns": stamp,
                    "received_at_unix_s": {k: v[1] for k, v in bundle.items()},
                    "intrinsics": list(info.intrinsics.k),
                    "vehicle_position_enu_m": [
                        vehicle.position.x,
                        vehicle.position.y,
                        vehicle.position.z,
                    ],
                    "vehicle_quaternion_wxyz": [
                        vehicle.orientation.w,
                        vehicle.orientation.x,
                        vehicle.orientation.y,
                        vehicle.orientation.z,
                    ],
                    "assets": {},
                }
                warming = len(self.warmup_frames) < STARTUP_WARMUP_BUNDLES
                index = len(self.warmup_frames) if warming else len(self.frames)
                for asset, data in {
                    "rgb": bytes(rgb.data),
                    "depth": bytes(depth.data),
                    "down": bytes(down.data),
                    **{k + "_message": m.SerializeToString() for k, (m, _) in bundle.items()},
                }.items():
                    name = f"{'warmup-' if warming else ''}{index:02}-{asset}.bin"
                    (self.root / name).write_bytes(data)
                    row["assets"][asset] = {
                        "file": name,
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                # New Gazebo RGBD subscriptions can initially have geometry but
                # uninitialized materials. Exclude a fixed two-second startup
                # interval without inspecting image content or model outcomes.
                # Retain every excluded bundle for an audit; do not fill a gap
                # inside the later twenty-frame sequence.
                (self.warmup_frames if warming else self.frames).append(row)
                self.last_complete_stamp_ns = stamp
                del self.bundles[stamp]
            except Exception as exc:
                self.error = str(exc)

    def finish(self, sample, event):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if sample is not None:
                state = sample()
                if state.get("nav_state") != 4 or state.get("arming_state") != 2:
                    raise RuntimeError("Native history requires armed PX4 auto-loiter")
            with self.lock:
                if self.error:
                    raise RuntimeError("Native capture failed: " + self.error)
                if len(self.frames) == 20:
                    self.active = False
                    manifest = {
                        "schema_version": "ship_anwm_capture.v1",
                        "run_id": self.config["run_id"],
                        "world_sha256": self.config["world_sha256"],
                        "plan_sha256": self.config["plan_sha256"],
                        "frames": self.frames,
                        "startup_warmup": {
                            "selection": "first_eight_complete_4hz_bundles_content_independent",
                            "bundle_count": STARTUP_WARMUP_BUNDLES,
                            "frames": self.warmup_frames,
                        },
                        "history_indices": list(range(16)),
                        "outcome_indices": list(range(16, 20)),
                        "ego_source": "Gazebo_model_pose_simulator_ground_truth",
                        "camera_pose_flu": [0.25, 0, 0.1, 0, 0, 0],
                        "single_rgbd_sensor": True,
                        "model_invoked": False,
                        "control_hold": "px4_auto_loiter",
                    }
                    (self.root / "capture.json").write_text(json.dumps(manifest, indent=2))
                    event("native_history_captured", frame_count=20, model_invoked=False)
                    return
            time.sleep(0.1)
        with self.lock:
            self.active = False
            (self.root / "failure.json").write_text(
                json.dumps(
                    {
                        "frames": len(self.frames),
                        "buffer_keys": [list(v) for v in self.bundles.values()][-12:],
                        "error": self.error,
                    }
                )
            )
        raise TimeoutError("Native RGBD capture timed out")


if __name__ == "__main__":
    import sys

    root = Path(sys.argv[1])
    recorder = NativeHistory(root, json.loads((root / "config.json").read_text()))
    recorder.start()
    recorder.finish(None, lambda *args, **kwargs: None)
    # Gazebo 8.11 Python transport can tear down callbacks after the Python GIL
    # has shut down. The isolated collector has closed all artifact files here;
    # bypass only interpreter destruction, never capture/validation failures.
    import os

    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
