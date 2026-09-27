"""Lossless, bounded RGBD recording during an explicitly approved AP flight.

This recorder never issues vehicle commands or invokes a model. Raw camera
payloads are compressed once; actual sensor and pose timestamps remain separate
from the nearest (not simultaneous) PX4 state sample.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import shutil
import zlib


def digest(data):
    return hashlib.sha256(data).hexdigest()


class MotionRecorder:
    def __init__(self, root, config):
        self.root = Path(root) / "motion"
        self.root.mkdir()
        self.config = config
        self.last_stamp = -1
        self.phase = None
        self.phase_start = None
        self.loiter_start = None
        self.count = 0
        self.bytes = 0
        self.rows = (self.root / "frames.jsonl").open("w", buffering=1)

    def append(self, history, poses, state):
        phase = state["phase"]
        if phase not in self.config["phases"]:
            return
        if phase != self.phase:
            self.phase, self.phase_start, self.loiter_start = phase, state["sim_s"], None
        if state["arming_state"] != 2 or state["landed"] is not False:
            return
        if state["nav_state"] == 3:
            self.loiter_start = None
        elif state["nav_state"] == 4:
            if self.loiter_start is None:
                self.loiter_start = state["sim_s"]
            if state["sim_s"] - self.loiter_start > self.config["hold_tail_s"]:
                return
        else:
            return
        stamps = sorted(set(history["onboard_rgb"]) & set(history["onboard_depth"]))
        for stamp in stamps:
            if stamp <= self.last_stamp or stamp / 1e9 < self.phase_start:
                continue
            # Wait until a later pose has arrived, so a callback-order race does
            # not make the nearest-pose join depend on when this method is called.
            if not poses or max(p["sensor_sim_s"] for p in poses) < stamp / 1e9 + 0.012:
                continue
            pose = min(poses, key=lambda p: abs(p["sensor_sim_s"] - stamp / 1e9))
            if abs(pose["sensor_sim_s"] - stamp / 1e9) > 0.012:
                raise ValueError("Motion RGBD/pose join exceeds 12 ms")
            if not all(math.isfinite(v) for v in [*pose["xyz"], *pose["quat_wxyz"]]):
                raise ValueError("Nonfinite motion pose")
            if self.count >= self.config["max_frames"]:
                raise ValueError("Motion capture frame bound exceeded")
            assets, pending = {}, []
            for key, size, encoding in [("onboard_rgb", 3, 3), ("onboard_depth", 4, 13)]:
                message, _ = history[key][stamp]
                if (
                    message.width,
                    message.height,
                    message.pixel_format_type,
                    len(message.data),
                ) != (640, 360, encoding, 640 * 360 * size):
                    raise ValueError("Invalid motion sensor encoding")
                data = bytes(message.data)
                packed = zlib.compress(data, level=3)
                name = f"{self.count:05d}-{key}.z"
                assets[key] = dict(
                    file=name,
                    sha256=digest(packed),
                    raw_sha256=digest(data),
                    raw_bytes=len(data),
                    compressed_bytes=len(packed),
                    codec="zlib",
                )
                pending.append((name, packed))
            extra = sum(len(p) for _, p in pending)
            if self.bytes + extra > self.config["max_compressed_bytes"]:
                raise ValueError("Motion capture byte bound exceeded")
            if shutil.disk_usage(self.root).free < extra + self.config["reserve_bytes"]:
                raise ValueError("Insufficient reserved disk space for motion capture")
            for name, packed in pending:
                (self.root / name).write_bytes(packed)
            row = dict(
                index=self.count,
                stamp_ns=stamp,
                phase=phase,
                pose={k: v for k, v in pose.items() if k != "raw_pose"},
                assets=assets,
                px4_sample=dict(
                    sim_s=state["sim_s"],
                    nav_state=state["nav_state"],
                    velocity_ned=state["velocity_ned"],
                    arming_state=state["arming_state"],
                    landed=state["landed"],
                ),
            )
            self.rows.write(json.dumps(row, allow_nan=False) + "\n")
            self.last_stamp = stamp
            self.count += 1
            self.bytes += extra

    def close(self, camera_info):
        self.rows.close()
        value = dict(
            schema_version="yokohama_ap_motion_capture.v1",
            frames=self.count,
            compressed_bytes=self.bytes,
            config=self.config,
            camera_info=camera_info,
            frames_sha256=digest((self.root / "frames.jsonl").read_bytes()),
            future_role="unpartitioned acquisition; not an inference payload",
            model_invoked=False,
            vehicle_commands_issued=False,
        )
        (self.root / "manifest.json").write_text(
            json.dumps(value, indent=2, allow_nan=False) + "\n"
        )
