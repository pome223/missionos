"""Isolated Gazebo RGB capture at staged actor positions; no aircraft or policy.

The simulator clock advances between poses. Images are actual rendered sensor
messages, but this is a stepped perception experiment, not live flight video.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import subprocess
import threading
import time
import zlib


def png_rgb(width, height, data):
    def chunk(kind, payload):
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    rows = b"".join(b"\0" + data[y * width * 3 : (y + 1) * width * 3] for y in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def main():
    from gz.msgs10.boolean_pb2 import Boolean
    from gz.msgs10.image_pb2 import Image
    from gz.msgs10.pose_pb2 import Pose
    from gz.msgs10.world_control_pb2 import WorldControl
    from gz.transport13 import Node

    root = Path("/mission")
    config = json.loads((root / "capture-config.json").read_text())
    node = Node()
    condition = threading.Condition()
    latest = None

    def receive(message):
        nonlocal latest
        with condition:
            latest = message
            condition.notify_all()

    if not node.subscribe(Image, "/ship/urban/rgb", receive):
        raise RuntimeError("Camera subscription failed")
    log = (root / "simulator.log").open("w")
    simulator = subprocess.Popen(
        ["gz", "sim", "-s", "--headless-rendering", "/mission/world.sdf"],
        stdout=log,
        stderr=subprocess.STDOUT,
    )

    def request(service, message, cls):
        ok, response = node.request(service, message, cls, Boolean, 3000)
        if not ok or not response.data:
            raise RuntimeError(f"Gazebo service failed: {service}")

    def stamp(message):
        return message.header.stamp.sec + message.header.stamp.nsec / 1e9

    def advance(steps, target):
        request("/world/default/control", WorldControl(pause=True, multi_step=steps), WorldControl)
        deadline = time.monotonic() + 30
        with condition:
            while latest is None or stamp(latest) < target - 0.11:
                if simulator.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Fresh rendered camera image not received")
                condition.wait(timeout=0.1)
            return latest

    try:
        deadline = time.monotonic() + 30
        while "/world/default/control" not in node.service_list():
            if simulator.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Gazebo services did not start")
            time.sleep(0.1)
        advance(1000, 1.0)
        simulation_target = 1.0
        with (root / "frames.jsonl").open("w", buffering=1) as frames:
            for case in config["cases"]:
                previous_t = None
                epoch = None
                for index, item in enumerate(case["samples"]):
                    position = Pose(name="urban_obstacle")
                    position.position.x, position.position.y, position.position.z = (
                        item["x_m"],
                        170,
                        30,
                    )
                    position.orientation.w = 1
                    request("/world/default/set_pose", position, Pose)
                    delta = 0.5 if previous_t is None else item["scripted_at_s"] - previous_t
                    simulation_target += delta
                    message = advance(round(delta * 1000), simulation_target)
                    if (
                        message.pixel_format_type != 3
                        or message.width != 640
                        or message.height != 360
                    ):
                        raise RuntimeError("Unexpected camera format")
                    if (
                        message.step != message.width * 3
                        or len(message.data) != message.step * message.height
                    ):
                        raise RuntimeError("Invalid RGB stride or payload")
                    observed = stamp(message)
                    if epoch is None:
                        epoch = observed
                    name = f"{case['case']}-{index:02}.png"
                    data = png_rgb(message.width, message.height, message.data)
                    (root / name).write_bytes(data)
                    row = {
                        "run_id": config["run_id"],
                        "case": case["case"],
                        "index": index,
                        "file": name,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "sensor_stamp_s": observed,
                        "observed_at_s": observed - epoch,
                        "scripted_at_s": item["scripted_at_s"],
                        "clock": "gazebo_simulation",
                        "source": "gz.msgs.Image",
                        "frame_role": "decision" if index < 5 else "validation_only",
                    }
                    frames.write(json.dumps(row) + "\n")
                    print(
                        json.dumps({k: row[k] for k in ("case", "index", "observed_at_s")}),
                        flush=True,
                    )
                    previous_t = item["scripted_at_s"]
    finally:
        simulator.terminate()
        try:
            simulator.wait(timeout=10)
        except subprocess.TimeoutExpired:
            simulator.kill()
            simulator.wait(timeout=5)
        log.close()


if __name__ == "__main__":
    main()
