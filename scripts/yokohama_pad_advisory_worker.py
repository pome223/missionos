"""Container-side fixed pad-camera history; no model execution or actor truth."""

from collections import deque
import hashlib
import threading
import time

from ship_urban_camera_worker import png_rgb
from yokohama_pad_queue import atomic_json


class PadCameraRecorder:
    def __init__(self, root, config, observer):
        self.root, self.config, self.observer = root, config, observer
        self.policy = config["world"]["pad_state_advisory"]
        self.active = False
        self.lock = threading.Lock()
        self.history = deque(maxlen=64)
        self.poses = deque(maxlen=512)
        from gz.msgs10.image_pb2 import Image
        from gz.msgs10.pose_v_pb2 import Pose_V
        from gz.transport13 import Node

        # A separate node preserves the Observer's existing subscription. The
        # model-specific static publisher emits once; world poses are periodic.
        self.pose_node = Node()
        self.pose_callback = self.receive_pose
        if not self.pose_node.subscribe(Pose_V, "/world/default/pose/info", self.pose_callback):
            raise RuntimeError("Pad camera pose subscription rejected")
        observer.subscribe(Image, self.policy["topic"], self.receive)

    def start(self):
        with self.lock:
            self.history.clear()
            self.poses.clear()
            self.active = True

    def receive_pose(self, message):
        if not self.active:
            return
        stamp = message.header.stamp.sec + message.header.stamp.nsec / 1e9
        for pose in message.pose:
            if pose.name != self.policy["camera_entity"]:
                continue
            record = dict(
                id=pose.id,
                xyz=[pose.position.x, pose.position.y, pose.position.z],
                quat_wxyz=[
                    pose.orientation.w,
                    pose.orientation.x,
                    pose.orientation.y,
                    pose.orientation.z,
                ],
                sensor_sim_s=stamp,
                received_monotonic_s=time.monotonic(),
            )
            with self.lock:
                if self.active and (not self.poses or stamp > self.poses[-1]["sensor_sim_s"]):
                    self.poses.append(record)

    def receive(self, message):
        if not self.active:
            return
        stamp = message.header.stamp.sec * 1_000_000_000 + message.header.stamp.nsec
        # Rendering arrives later than pose telemetry. Join against measured
        # historical poses at/before the sensor time, never the latest pose.
        with self.lock:
            prior = [p for p in self.poses if p["sensor_sim_s"] <= stamp / 1e9]
            pose = prior[-1] if prior else None
        if (
            not pose
            or time.monotonic() - pose["received_monotonic_s"] > 2
            or abs(pose["sensor_sim_s"] - stamp / 1e9) > 0.04
            or (message.width, message.height, message.pixel_format_type, len(message.data))
            != (640, 360, 3, 640 * 360 * 3)
        ):
            return
        record = dict(
            stamp_ns=stamp,
            rgb=bytes(message.data),
            pose={k: pose[k] for k in ("id", "xyz", "quat_wxyz", "sensor_sim_s")},
        )
        with self.lock:
            if self.active and (not self.history or stamp > self.history[-1]["stamp_ns"]):
                self.history.append(record)

    def capture(self, folder, now):
        with self.lock:
            records = [r for r in self.history if r["stamp_ns"] / 1e9 <= now][-16:]
        if len(records) != 16 or now - records[-1]["stamp_ns"] / 1e9 > 1:
            return None
        if any(
            abs(b["stamp_ns"] - a["stamp_ns"] - 250_000_000) > 4_000_001
            for a, b in zip(records, records[1:])
        ):
            return None
        path = folder / "history"
        path.mkdir()
        frames = []
        for i, r in enumerate(records):
            name = f"{i:02d}.png"
            data = png_rgb(640, 360, r["rgb"])
            (path / name).write_bytes(data)
            frames.append(
                dict(
                    stamp_ns=r["stamp_ns"],
                    pose=r["pose"],
                    file=name,
                    sha256=hashlib.sha256(data).hexdigest(),
                )
            )
        record = dict(
            schema="missionos.pad-camera-history.v1",
            run_id=self.config["run_id"],
            world_sha256=self.config["world"]["world_sha256"],
            camera_entity=self.policy["camera_entity"],
            source="live fixed delivery-pad camera; not onboard",
            frames=frames,
        )
        atomic_json(path / "capture.json", record)
        return dict(
            file="history/capture.json",
            sha256=hashlib.sha256((path / "capture.json").read_bytes()).hexdigest(),
        )

    def close(self, observation):
        with self.lock:
            self.active = False
            self.history.clear()
            self.poses.clear()
        self.pose_node.unsubscribe("/world/default/pose/info")
        atomic_json(
            self.root / "pad-advisory-closed.json",
            dict(
                schema="missionos.pad-advisory-close.v1",
                run_id=self.config["run_id"],
                closed_sim_s=observation["sim_s"],
                reason="pad wait ended",
            ),
        )
