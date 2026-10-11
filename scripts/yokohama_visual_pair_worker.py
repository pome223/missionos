"""Container CPU rendering only: fixed camera/goal and staged static barrier."""

import json
from pathlib import Path
import subprocess
import threading
import time

from gz.msgs10.boolean_pb2 import Boolean
from gz.msgs10.image_pb2 import Image
from gz.msgs10.pose_pb2 import Pose
from gz.msgs10.world_control_pb2 import WorldControl
from gz.transport13 import Node
from ship_urban_camera_worker import png_rgb


def main():
    root = Path("/mission")
    config = json.loads((root / "pair-config.json").read_text())
    node, latest, condition = Node(), {}, threading.Condition()

    def receive(key, msg):
        with condition:
            latest[key] = msg
            condition.notify_all()

    for key in ("rgb", "down"):
        if not node.subscribe(Image, "/yokohama/pair/" + key, lambda msg, k=key: receive(k, msg)):
            raise RuntimeError("Image subscription failed")

    def request(service, msg, cls):
        ok, response = node.request(service, msg, cls, Boolean, 3000)
        if not ok or not response.data:
            raise RuntimeError("Unconfirmed scene transport")

    def stamp(msg):
        return msg.header.stamp.sec + msg.header.stamp.nsec / 1e9

    def advance(target):
        request("/world/default/control", WorldControl(pause=True, multi_step=500), WorldControl)
        with condition:
            end = time.monotonic() + 20
            while any(k not in latest or stamp(latest[k]) < target - 0.11 for k in ("rgb", "down")):
                if time.monotonic() >= end:
                    raise TimeoutError("Fresh paired camera images unavailable")
                condition.wait(timeout=0.1)
            frames = dict(latest)
        if abs(stamp(frames["rgb"]) - stamp(frames["down"])) > 0.15:
            raise ValueError("Camera stamps differ")
        return frames

    with (root / "simulator.log").open("w") as log:
        sim = subprocess.Popen(
            ["gz", "sim", "-s", "--headless-rendering", "/mission/models/worlds/default.sdf"],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            end = time.monotonic() + 30
            while "/world/default/control" not in node.service_list():
                if sim.poll() is not None or time.monotonic() > end:
                    raise RuntimeError("Gazebo did not start")
                time.sleep(0.1)
            target, records = 0.0, []
            for label, xyz in (("open", [1000, 1000, 15]), ("blocked", config["barrier_xyz_m"])):
                pose = Pose(name="pair_barrier")
                pose.position.x, pose.position.y, pose.position.z = xyz
                pose.orientation.w = config["barrier_quat_wz"][0]
                pose.orientation.z = config["barrier_quat_wz"][1]
                request("/world/default/set_pose", pose, Pose)
                for index in range(3):
                    target += 0.5
                    frames = advance(target)
                    files, stamps = {}, {}
                    for key, msg in frames.items():
                        if msg.pixel_format_type != 3 or (msg.width, msg.height) != (640, 360):
                            raise ValueError("Unexpected camera format")
                        name = f"{label}-{index}-{key}.png"
                        (root / name).write_bytes(png_rgb(msg.width, msg.height, msg.data))
                        files[key], stamps[key] = name, stamp(msg)
                    records.append(
                        dict(
                            evaluation_label=label,
                            index=index,
                            images=files,
                            stamps_s=stamps,
                            requested_sim_s=target,
                            camera_pose=config["camera_pose"],
                            goal=config["goal_world_xyz_m"],
                        )
                    )
            (root / "frames.json").write_text(json.dumps(records, indent=2) + "\n")
        finally:
            sim.terminate()
            try:
                sim.wait(timeout=10)
            except subprocess.TimeoutExpired:
                sim.kill()
                sim.wait(timeout=10)


if __name__ == "__main__":
    main()
