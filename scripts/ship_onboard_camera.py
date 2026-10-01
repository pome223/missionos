"""Container-local continuous onboard RGB receiver; never reads obstacle poses."""

import hashlib
import gzip
import threading
import time

from ship_urban_camera_worker import png_rgb


class OnboardPoseTopic:
    """Direct validation stream, avoiding a queued CLI text pipe during rendering."""

    def __init__(self, root):
        from gz.msgs10.pose_v_pb2 import Pose_V
        from gz.transport13 import Node

        self.node = Node()
        self.latest, self.received_at = "", 0.0
        self.lock = threading.Lock()
        self.log = gzip.open(root / "poses.txt.gz", "wt", compresslevel=3)
        self.closed = False
        if not self.node.subscribe(Pose_V, "/world/default/pose/info", self.receive):
            raise RuntimeError("Validation pose subscription failed")

    def receive(self, message):
        with self.lock:
            now = time.monotonic()
            if self.closed or now - self.received_at < 0.05:
                return
            self.latest, self.received_at = str(message), now
            self.log.write(self.latest + "\n")
            self.log.flush()

    def fresh(self):
        return self.latest if time.monotonic() - self.received_at < 0.6 else ""

    def close(self):
        with self.lock:
            self.closed = True
            self.log.close()


class OnboardCamera:
    def __init__(self, root, *, dual_view=False):
        from gz.msgs10.image_pb2 import Image
        from gz.transport13 import Node

        self.root = root
        (root / "rgb").mkdir()
        self.node = Node()
        self.lock = threading.Lock()
        self.latest = None
        self.index = 0
        self.dual_view, self.pairs = dual_view, {}
        if not self.node.subscribe(Image, "/ship/onboard/rgb", self.receive):
            raise RuntimeError("Onboard camera subscription failed")
        if dual_view:
            (root / "down").mkdir()
            if not self.node.subscribe(
                Image, "/ship/aerovla/down", lambda m: self.receive(m, "down")
            ):
                raise RuntimeError("Downward camera subscription failed")

    def receive(self, message, kind="rgb"):
        with self.lock:
            now = time.monotonic()
            if not self.dual_view:
                self.latest = (message, now)
                return
            stamp = message.header.stamp.sec * 10**9 + message.header.stamp.nsec
            pair = self.pairs.setdefault(stamp, {})
            pair[kind] = (message, now)
            if set(pair) == {"rgb", "down"}:
                if self.latest is None or stamp > self.latest[2]:
                    self.latest = (*pair["rgb"], stamp, pair["down"])
            for old in sorted(self.pairs)[:-20]:
                del self.pairs[old]

    def snapshot(self):
        with self.lock:
            latest = self.latest
        if latest is None:
            return None
        message, received = latest[:2]
        age = time.monotonic() - received
        if (
            age > 0.6
            or message.pixel_format_type != 3
            or message.width != 640
            or message.height != 360
            or message.step != 640 * 3
            or len(message.data) != 640 * 360 * 3
        ):
            return None
        self.index += 1
        filename = f"rgb/{self.index:05}.png"
        data = png_rgb(640, 360, message.data)
        (self.root / filename).write_bytes(data)
        frame = {
            "file": filename,
            "sha256": hashlib.sha256(data).hexdigest(),
            "sensor_stamp_s": message.header.stamp.sec + message.header.stamp.nsec / 1e9,
            "received_age_s": age,
            "source": "gz.msgs.Image:onboard_base_link",
        }
        if self.dual_view:
            down, down_received = latest[3]
            down_age = time.monotonic() - down_received
            if down_age > 0.6 or (
                down.width,
                down.height,
                down.step,
                down.pixel_format_type,
                len(down.data),
            ) != (640, 360, 1920, 3, 691200):
                return None
            down_data = png_rgb(640, 360, down.data)
            down_file = f"down/{self.index:05}.png"
            (self.root / down_file).write_bytes(down_data)
            frame["paired_down"] = {
                "file": down_file,
                "sha256": hashlib.sha256(down_data).hexdigest(),
                "sensor_stamp_s": down.header.stamp.sec + down.header.stamp.nsec / 1e9,
                "received_age_s": down_age,
                "source": "gz.msgs.Image:downward_base_link",
            }
        return frame


class VisionBridge:
    """Bound file mailbox to the host image processor; no direct model authority."""

    def __init__(self, root, config):
        self.root, self.config, self.index = root, config, 0

    def exchange(self, rows, purpose, sample=None):
        import json

        self.index += 1
        request = {
            "run_id": self.config["run_id"],
            "world_sha256": self.config["world_sha256"],
            "plan_sha256": self.config["plan_sha256"],
            "request_id": self.index,
            "purpose": purpose,
            "frames": [
                {k: row[k] for k in ("elapsed_s", "local_ned", "attitude_q", "onboard_frame")}
                for row in rows
            ],
        }
        payload = json.dumps(request, sort_keys=True).encode()
        request_hash = hashlib.sha256(payload).hexdigest()
        temporary = self.root / "vision-request.tmp"
        temporary.write_bytes(payload)
        temporary.replace(self.root / "vision-request.json")
        path = self.root / f"vision-response-{self.index}.json"
        deadline = time.monotonic() + (90 if self.config["urban"].get("anwm_static") else 25)
        next_sample = time.monotonic()
        while time.monotonic() < deadline:
            if sample is not None and time.monotonic() >= next_sample:
                current = sample()
                next_sample = time.monotonic() + 0.5
                if not current.get("onboard_frame") or current.get("nav_state") != 4:
                    raise RuntimeError("Camera or entry hold lost while awaiting proposal")
            if path.exists():
                response = json.loads(path.read_text())
                if response.get("request_sha256") != request_hash:
                    raise RuntimeError("Vision response binding mismatch")
                if response.get("error"):
                    raise RuntimeError("Vision rejected observation: " + response["error"])
                return response
            time.sleep(0.05)
        raise TimeoutError("Onboard vision processing timed out; aircraft remains in hold")

    def decide(self, sample, event):
        rows = []
        while len(rows) < 5 or rows[-1]["elapsed_s"] - rows[0]["elapsed_s"] < 2:
            row = sample()
            if not row.get("onboard_frame") or not row.get("attitude_q"):
                raise RuntimeError("Fresh onboard RGB and PX4 attitude are required")
            rows.append(row)
            time.sleep(0.5)
        response = self.exchange(rows, "decision", sample=sample)
        event("onboard_decision_received", response=response)
        return response["history"], response["decision"]

    def wait_clear(self, sample, event):
        deadline = time.monotonic() + 120
        consecutive = 0
        previous_stamp = -1.0
        while time.monotonic() < deadline:
            response = self.exchange([sample()], "clearance")
            stamp = response["history"][0]["sensor_stamp_s"]
            if stamp <= previous_stamp:
                raise RuntimeError("Clearance requires a new camera image")
            previous_stamp = stamp
            # Two fresh images and an extra metre of margin before direct flight.
            consecutive = consecutive + 1 if response["history"][0]["obstacle_x_m"] >= 14 else 0
            if consecutive >= 2:
                event("onboard_clearance_observed", response=response)
                return
            time.sleep(0.5)
        raise TimeoutError("Image-observed clearance did not arrive; aircraft remains in hold")


def run_actor():
    """Isolated transport calls cannot starve Python sensor/telemetry callbacks."""
    import json
    import sys
    from pathlib import Path
    from gz.msgs10.boolean_pb2 import Boolean
    from gz.msgs10.pose_pb2 import Pose
    from gz.transport13 import Node

    root = Path("/mission")
    urban = json.loads((root / "config.json").read_text())["urban"]
    started = float(sys.argv[2])
    node = Node()
    while True:
        t = time.monotonic() - started
        if urban["case"] == "brake_stop":
            east = 4 * t - 0.5 * t * t if t <= 4 else 8 + 2 * max(0, t - 40)
        else:
            east = t * urban["scripted_obstacle_speed_mps"]
        east = min(16.0, east)
        message = Pose(name="urban_obstacle")
        message.position.x, message.position.y, message.position.z = (
            east,
            urban["obstacle_north_m"],
            30,
        )
        message.orientation.w = 1
        ok, response = node.request("/world/default/set_pose", message, Pose, Boolean, 2000)
        if not ok or not response.data:
            raise RuntimeError("Scripted obstacle update rejected")
        if east >= 16:
            return
        time.sleep(0.1)


if __name__ == "__main__":
    run_actor()
