#!/usr/bin/env python3
"""Capture an explicitly declared static urban goal camera; no vehicle commands."""

from datetime import datetime, timezone
import hashlib
import json
import struct
import threading
import time
import zlib
import xml.etree.ElementTree as ET
from pathlib import Path
from gz.transport13 import Node
from gz.msgs10.image_pb2 import Image
from gz.msgs10.camera_info_pb2 import CameraInfo
from gz.msgs10.pose_v_pb2 import Pose_V
from google.protobuf.json_format import MessageToDict

root = Path("/session/goal")
root.mkdir(exist_ok=True)
sdf_tree = ET.parse("/session/goal-camera.sdf")
expected = list(map(float, sdf_tree.findtext("model/pose").split()))
if expected[3:] != [0, 0, 0]:
    raise ValueError("urban reference requires ENU east heading")
prefix = (
    "/world/default/model/aerial_goal_reference/link/goal_camera_link/sensor/goal_rgb"
)
buckets = {k: {} for k in ("rgb", "info", "pose")}
lock = threading.Lock()


def callback(key, msg):
    stamp = msg.header.stamp.sec * 1000000000 + msg.header.stamp.nsec
    with lock:
        buckets[key][stamp] = msg
        while len(buckets[key]) > 512:
            del buckets[key][min(buckets[key])]


node = Node()
node.subscribe(Image, prefix + "/image", lambda msg: callback("rgb", msg))
node.subscribe(CameraInfo, prefix + "/camera_info", lambda msg: callback("info", msg))
node.subscribe(Pose_V, "/world/default/pose/info", lambda msg: callback("pose", msg))
deadline = time.monotonic() + 45
bundle = None
while time.monotonic() < deadline:
    with lock:
        common = set.intersection(*(set(v) for v in buckets.values()))
        if common:
            stamp = max(common)
            bundle = {k: v[stamp] for k, v in buckets.items()}
            break
    time.sleep(0.02)
if bundle is None:
    raise RuntimeError("synchronized reference camera unavailable")
rgb, info = bundle["rgb"], bundle["info"]
if (rgb.width, rgb.height, rgb.step) != (640, 360, 1920) or (
    info.width,
    info.height,
) != (640, 360):
    raise ValueError("unexpected goal camera geometry")
poses = [p for p in bundle["pose"].pose if p.name == "aerial_goal_reference"]
if len(poses) != 1:
    raise ValueError("reference camera pose unavailable")
p = poses[0]
if (
    max(
        abs(a - b)
        for a, b in zip((p.position.x, p.position.y, p.position.z), tuple(expected[:3]))
    )
    > 1e-6
):
    raise ValueError("reference camera moved")
if abs(p.orientation.w - 1) > 1e-6:
    raise ValueError("reference camera rotated")


def chunk(kind, data):
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


rows = b"".join(
    b"\0" + rgb.data[y * rgb.step : (y + 1) * rgb.step] for y in range(rgb.height)
)
png = (
    b"\x89PNG\r\n\x1a\n"
    + chunk(b"IHDR", struct.pack(">IIBBBBB", 640, 360, 8, 2, 0, 0, 0))
    + chunk(b"IDAT", zlib.compress(rows))
    + chunk(b"IEND", b"")
)
(root / "goal.png").write_bytes(png)
(root / "goal-rgb.bin").write_bytes(rgb.data)
(root / "goal-rgb.pb").write_bytes(rgb.SerializeToString())
(root / "goal-camera-info.pb").write_bytes(info.SerializeToString())
(root / "goal-pose.pb").write_bytes(bundle["pose"].SerializeToString())
scene = json.loads(Path("/session/scene.json").read_text())
sdf = Path("/session/goal-camera.sdf").read_bytes()
(root / "goal-camera.sdf").write_bytes(sdf)
K = list(info.intrinsics.k)
result = {
    "schema_version": "missionos_aerial_goal_reference.v1",
    "role": "declared_goal_reference",
    "source_kind": "actual_gazebo_static_camera",
    "rgb_file": "goal.png",
    "rgb_sha256": hashlib.sha256(png).hexdigest(),
    "width": 640,
    "height": 360,
    "intrinsics": [K[0:3], K[3:6], K[6:9]],
    "optical_to_local_ned": [
        [-1, 0, 0, expected[1]],
        [0, 0, 1, expected[0]],
        [0, 1, 0, -expected[2]],
        [0, 0, 0, 1],
    ],
    "simulation_time_ns": stamp,
    "received_at": datetime.now(timezone.utc).isoformat(),
    "scene_geometry_sha256": scene["scene_sha256"],
    "source_message_sha256": hashlib.sha256(rgb.SerializeToString()).hexdigest(),
    "source_message_file": "goal-rgb.pb",
    "camera_info": MessageToDict(info, preserving_proto_field_name=True),
    "observed_reference_pose": MessageToDict(p, preserving_proto_field_name=True),
    "camera_sdf_file": "goal-camera.sdf",
    "camera_sdf_sha256": hashlib.sha256(sdf).hexdigest(),
    "future_outcome_image": False,
    "candidate_outcome_observed": False,
    "flight_command_sent": False,
}
(root / "reference.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps({"goal_reference_captured": True, "simulation_time_ns": stamp}))
