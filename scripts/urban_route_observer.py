#!/usr/bin/env python3
"""RGB/pose observer inside the isolated urban container; sends no flight command."""

import hashlib
import json
from pathlib import Path
import struct
import threading
import time
import zlib


def main():
    from gz.transport13 import Node
    from gz.msgs10.image_pb2 import Image
    from gz.msgs10.pose_v_pb2 import Pose_V

    root = Path("/session/route-images")
    root.mkdir()
    buckets = {"rgb": {}, "pose": {}}
    lock = threading.Lock()

    def receive(key, message):
        stamp = message.header.stamp.sec * 1000000000 + message.header.stamp.nsec
        with lock:
            buckets[key][stamp] = message
            while len(buckets[key]) > (1024 if key == "pose" else 32):
                del buckets[key][min(buckets[key])]

    node = Node()
    node.subscribe(
        Image,
        "/world/default/model/x500_depth_0/link/camera_link/sensor/IMX214/image",
        lambda m: receive("rgb", m),
    )
    node.subscribe(Pose_V, "/world/default/pose/info", lambda m: receive("pose", m))
    (root / "ready").write_text("observer subscribed\n")
    frames = []
    previous = -1
    deadline = time.monotonic() + 1200

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    while time.monotonic() < deadline:
        with lock:
            common = [
                s for s in set(buckets["rgb"]) & set(buckets["pose"]) if s > previous
            ]
            stamp = min(common) if common else None
            bundle = (
                {k: v[stamp] for k, v in buckets.items()} if stamp is not None else None
            )
        if bundle:
            rgb = bundle["rgb"]
            vehicle = [p for p in bundle["pose"].pose if p.name == "x500_depth_0"]
            if len(vehicle) != 1 or (rgb.width, rgb.height, rgb.step) != (
                640,
                360,
                1920,
            ):
                raise ValueError("unexpected camera/pose source")
            rows = b"".join(
                b"\0" + rgb.data[y * rgb.step : (y + 1) * rgb.step]
                for y in range(rgb.height)
            )
            png = (
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", 640, 360, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(rows))
                + chunk(b"IEND", b"")
            )
            name = "frame-%04d.png" % len(frames)
            (root / name).write_bytes(png)
            p = vehicle[0]
            frames.append(
                {
                    "file": name,
                    "simulation_time_ns": stamp,
                    "image_sha256": hashlib.sha256(png).hexdigest(),
                    "pose_enu_m": [p.position.x, p.position.y, p.position.z],
                    "orientation_xyzw": [
                        p.orientation.x,
                        p.orientation.y,
                        p.orientation.z,
                        p.orientation.w,
                    ],
                }
            )
            previous = stamp
        if Path("/session/flight-result.json").exists():
            break
        time.sleep(0.02)
    (root / "frames.json").write_text(
        json.dumps(
            {
                "schema_version": "missionos_urban_route_rgb.v1",
                "exact_rgb_pose_timestamps": True,
                "frames": frames,
            },
            indent=2,
        )
        + "\n"
    )
    print(json.dumps({"frames": len(frames)}))


if __name__ == "__main__":
    main()
