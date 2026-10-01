"""Container-local PX4/Gazebo worker. No host network or hardware endpoints.

Only the host runtime installs this worker in a dedicated container. Return is
gated on a host-verifier receipt, never a timeout or the detach command alone.
"""

from __future__ import annotations

import gzip
import json
import math
import re
import subprocess
import threading
import time
from pathlib import Path


ROOT = Path("/mission")
BIN = "/opt/px4-gazebo/bin/px4-"


def run(argv, timeout=10):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"command failed: {argv}: {result.stderr[-300:]} {result.stdout[-300:]}")
    return result.stdout


def field(text, name):
    match = re.search(r"\b" + re.escape(name) + r":\s*([-+0-9.eE]+|True|False)\b", text)
    if not match:
        return None
    value = match.group(1)
    return value == "True" if value in ("True", "False") else float(value)


def pose(text, name):
    for block in re.split(r"\npose \{", "\n" + text)[1:]:
        found = re.search(r'name:\s*"([^"]+)"', block)
        if not found or found.group(1) != name:
            continue
        position = re.search(r"position\s*\{(.*?)\}", block, re.S)
        identity = re.search(r"\bid:\s*(\d+)", block)
        if position and identity:
            return {
                "id": int(identity.group(1)),
                "xyz": [field(position.group(1), k) or 0 for k in "xyz"],
            }
    return None


def apply_wind(command, event, wind_mps, *, sleep=time.sleep):
    """Publish is not an ACK: retry only until actual wind state is observed."""
    for attempt in range(1, 6):
        command(
            [
                "gz",
                "topic",
                "-t",
                "/world/default/wind",
                "-m",
                "gz.msgs.Wind",
                "-p",
                f"enable_wind: true linear_velocity {{ x: 0 y: {wind_mps} z: 0 }}",
            ]
        )
        event("wind_requested", wind_mps=wind_mps, attempt=attempt)
        readback = command(
            [
                "gz",
                "service",
                "-s",
                "/world/default/wind_info",
                "--reqtype",
                "gz.msgs.Empty",
                "--reptype",
                "gz.msgs.Wind",
                "--timeout",
                "3000",
                "--req",
                "",
            ]
        )
        event("wind_readback", attempt=attempt, readback=readback)
        if abs((field(readback, "y") or 0) - wind_mps) <= 0.01 and "enable_wind: true" in readback:
            event("wind_observed", readback=readback, wind_mps=wind_mps, attempt=attempt)
            return
        sleep(0.5)
    raise RuntimeError("wind readback mismatch after five bounded requests")


class Topic:
    def __init__(self, topic, path):
        self.latest, self.received_at = "", 0.0
        self.contact_seen = False
        self.log = gzip.open(str(path) + ".gz", "wt", compresslevel=3)
        self.process = subprocess.Popen(
            ["stdbuf", "-oL", "gz", "topic", "-e", "-t", topic],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        lines = []
        for line in self.process.stdout:
            self.log.write(line)
            if not line.strip() and lines:
                self.latest, self.received_at = "".join(lines), time.monotonic()
                if "collision1" in self.latest and "x500" in self.latest:
                    self.contact_seen = True
                lines = []
                self.log.flush()
            else:
                lines.append(line)

    def fresh(self):
        return self.latest if time.monotonic() - self.received_at < 3 else ""

    def close(self):
        self.process.terminate()
        self.process.wait(timeout=5)
        self.log.close()


def main():
    config = json.loads((ROOT / "config.json").read_text())
    onboard = (config.get("urban") or {}).get("onboard_camera")
    if onboard:
        from ship_onboard_camera import OnboardPoseTopic

        pose_topic = OnboardPoseTopic(ROOT)
    else:
        pose_topic = Topic("/world/default/pose/info", ROOT / "poses.txt")
    topics = {
        "poses": pose_topic,
        "deck": Topic(
            "/world/default/model/stationary_ship/link/link/sensor/contact/contact",
            ROOT / "deck-contacts.txt",
        ),
        "payload": Topic(
            "/world/default/model/delivery_pad/link/link/sensor/contact/contact",
            ROOT / "payload-contacts.txt",
        ),
        "joint": Topic("/model/x500_0/delivery_payload/state", ROOT / "joint-state.txt"),
    }
    started = time.monotonic()
    urban = None
    if config.get("urban"):
        from ship_urban_sitl_stage import UrbanStage

        urban = UrbanStage(config, ROOT, Topic, pose, run, BIN)
    camera = None
    if (config.get("urban") or {}).get("onboard_camera"):
        from ship_onboard_camera import OnboardCamera

        camera = OnboardCamera(ROOT, dual_view=bool(config["urban"].get("aerovla_live")))
    phase = "preflight"
    record = (ROOT / "telemetry.jsonl").open("w", buffering=1)
    events = (ROOT / "events.jsonl").open("w", buffering=1)

    def event(kind, **kwargs):
        value = {
            "event": kind,
            "elapsed_s": time.monotonic() - started,
            "run_id": config["run_id"],
            **kwargs,
        }
        events.write(json.dumps(value) + "\n")
        print(json.dumps(value), flush=True)

    def sample():
        query_started = time.monotonic()
        raw = {
            key: run([BIN + "listener", key, "-n", "1"], 5)
            for key in (
                "vehicle_local_position",
                "vehicle_status",
                "vehicle_land_detected",
                "battery_status",
                "mission_result",
            )
        }
        if camera:
            raw["vehicle_attitude"] = run([BIN + "listener", "vehicle_attitude", "-n", "1"], 5)
        poses = topics["poses"].fresh()
        value = {
            "run_id": config["run_id"],
            "world_sha256": config["world_sha256"],
            "plan_sha256": config["plan_sha256"],
            "elapsed_s": time.monotonic() - started,
            "px4_query_duration_s": time.monotonic() - query_started,
            "phase": phase,
            "vehicle": pose(poses, "x500_0"),
            "payload": pose(poses, "delivery_payload"),
            "ship": pose(poses, "stationary_ship"),
            "poses_fresh": bool(poses),
            "poses_sensor_stamp_s": (field(poses, "sec") or 0) + (field(poses, "nsec") or 0) / 1e9,
            "joint_state": topics["joint"].latest.strip(),
            "deck_contact": "x500" in topics["deck"].fresh(),
            "payload_contact": "delivery_payload" in topics["payload"].fresh(),
            "local_ned": [field(raw["vehicle_local_position"], k) for k in "xyz"],
            "velocity_ned": [field(raw["vehicle_local_position"], "v" + k) for k in "xyz"],
            "position_valid": field(raw["vehicle_local_position"], "xy_valid"),
            "arming_state": field(raw["vehicle_status"], "arming_state"),
            "nav_state": field(raw["vehicle_status"], "nav_state"),
            "landed": field(raw["vehicle_land_detected"], "landed"),
            "battery_remaining": field(raw["battery_status"], "remaining"),
            "raw_px4": raw,
            "preflight_pass": field(raw["vehicle_status"], "pre_flight_checks_pass"),
            "mission_id": field(raw["mission_result"], "mission_id"),
            "mission_valid": field(raw["mission_result"], "valid"),
            "mission_count": field(raw["mission_result"], "seq_total"),
        }
        if camera:
            match = re.search(r"\bq:\s*\[([^]]+)\]", raw["vehicle_attitude"])
            value["attitude_q"] = [float(x) for x in match.group(1).split(",")] if match else None
            value["onboard_frame"] = camera.snapshot()
        if urban:
            value.update(urban.observe(poses))
        record.write(json.dumps(value) + "\n")
        return value

    def wait_for(predicate, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = sample()
            if predicate(value):
                return value
            if time.monotonic() - started > config["timeout_s"]:
                raise TimeoutError("mission timeout")
            time.sleep(0.5)
        raise TimeoutError(f"phase timeout: {phase}")

    def upload(name):
        previous_id = field(run([BIN + "listener", "mission_result", "-n", "1"]), "mission_id")
        value = json.loads(run(["python3", str(ROOT / (name + "-upload.py"))], 40).splitlines()[-1])
        event(name + "_upload", receipt=value)
        if value.get("mission_ack_type") != 0:
            raise RuntimeError("mission upload not accepted")
        # MAVLink ACK precedes Navigator's asynchronous mission validation.
        # Requesting AUTO immediately can be denied while validation is pending.
        ready = wait_for(
            lambda v: (
                v["mission_valid"] is True
                and v["mission_id"] is not None
                and v["mission_id"] != previous_id
                and v["mission_count"] == len(value["mission_items"])
            ),
            15,
        )
        event(name + "_ready", mission_id=ready["mission_id"], previous_mission_id=previous_id)

    def activate(name, **details):
        # Commander may still be processing Navigator's mission-valid update.
        # Its CLI exit status is not a mode-transition ACK; observe nav_state.
        for attempt in range(1, 6):
            run([BIN + "commander", "mode", "auto:mission"])
            event(name + "_mode_request", attempt=attempt)
            try:
                wait_for(lambda v: v["nav_state"] == 3, 1.5)
            except TimeoutError:
                if time.monotonic() - started > config["timeout_s"]:
                    raise
                continue
            event(name + "_requested", mode_observed=3, **details)
            return
        raise RuntimeError(f"PX4 did not enter AUTO MISSION for {name}")

    try:
        # These are explicitly simulator-only limits; retain PX4 battery telemetry.
        for name, value in {
            "COM_RC_IN_MODE": 4,
            "NAV_DLL_ACT": 0,
            "MPC_XY_CRUISE": config["airspeed_mps"],
            "SIM_BAT_DRAIN": 1800,
            "SIM_BAT_MIN_PCT": 0,
            "COM_DISARM_LAND": 2,
        }.items():
            run([BIN + "param", "set", name, str(value)])
        wait_for(
            lambda v: (
                v["vehicle"]
                and v["payload"]
                and v["ship"]
                and v["position_valid"]
                and v["preflight_pass"] is True
            ),
            60,
        )
        event("world_observed")
        upload("outbound")
        run([BIN + "commander", "arm"])
        wait_for(lambda v: v["arming_state"] == 2, 10)
        activate("outbound")
        phase = "outbound"
        wait_for(lambda v: v["vehicle"] and v["vehicle"]["xyz"][2] > 20, 60)
        apply_wind(run, event, config["wind_mps"])
        if urban:
            urban.execute(
                sample, wait_for, event, upload, activate, lambda: time.monotonic() - started
            )
        goal = config["goal_north_m"]

        def at_delivery(v):
            if not v["vehicle"]:
                return False
            x, y, z = v["vehicle"]["xyz"]
            return math.hypot(x, y - goal) < 5 and abs(z - 3) < 1 and v["landed"] is False

        wait_for(at_delivery, config["timeout_s"])
        phase = "delivery_hold"
        # Observe the low hover before detaching; cargo is never placed by code.
        time.sleep(2)
        event("delivery_hover_observed", observation=sample())

        def request_detach(attempt):
            run(
                [
                    "gz",
                    "topic",
                    "-t",
                    "/model/x500_0/delivery_payload/detach",
                    "-m",
                    "gz.msgs.Empty",
                    "-p",
                    "",
                ]
            )
            event("detach_published" if attempt == 1 else "detach_republished", attempt=attempt)

        request_detach(1)
        detach_attempts = 1
        next_detach = time.monotonic() + 5
        phase = "delivery_verify"
        # Host independently checks payload separation, pad contact and stability.
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            observation = sample()
            permit = ROOT / "return-authorized.json"
            if permit.exists():
                binding = json.loads(permit.read_text())
                if (
                    all(
                        binding.get(k) == config[k]
                        for k in ("run_id", "world_sha256", "plan_sha256")
                    )
                    and binding.get("delivery_verified") is True
                ):
                    event("return_authorized", binding=binding)
                    break
                raise RuntimeError("return permit binding mismatch")
            # A one-shot transport publish can exit before the subscriber has
            # received it. Retry only this idempotent request while the actual
            # observed parcel remains attached; the host still verifies delivery.
            if (
                detach_attempts < 3
                and time.monotonic() >= next_detach
                and observation["poses_fresh"]
                and observation["payload"]
                and observation["vehicle"]
                and math.dist(observation["payload"]["xyz"], observation["vehicle"]["xyz"]) < 1
            ):
                detach_attempts += 1
                request_detach(detach_attempts)
                next_detach = time.monotonic() + 5
            time.sleep(0.5)
        else:
            raise TimeoutError("payload delivery not verified; return remains unauthorized")
        phase = "return"
        # Leave the outbound unlimited loiter before replacing its mission.
        # Upload acceptance alone does not invalidate PX4's active setpoint.
        run([BIN + "commander", "mode", "auto:loiter"])
        wait_for(lambda v: v["nav_state"] == 4, 10)
        event("return_hold_observed")
        upload("return")
        activate("return")
        wait_for(
            lambda v: v["vehicle"] and v["vehicle"]["xyz"][2] > 20 and v["nav_state"] == 3,
            60,
        )
        event("return_climb_observed")
        stable_since = None

        def recovered(v):
            nonlocal stable_since
            valid = (
                v["vehicle"]
                and math.hypot(*v["vehicle"]["xyz"][:2]) < 3
                and v["landed"] is True
                and v["arming_state"] == 1
                and v["deck_contact"]
            )
            if not valid:
                stable_since = None
            elif stable_since is None:
                stable_since = v["elapsed_s"]
            return valid and v["elapsed_s"] - stable_since >= 3

        wait_for(recovered, config["timeout_s"])
        event("recovery_candidate_observed")
    except Exception as exc:
        event("worker_failed", reason=f"{type(exc).__name__}: {exc}")
        # Stop further mission progression in this disposable simulation.
        subprocess.run([BIN + "commander", "mode", "auto:loiter"], capture_output=True, timeout=5)
        raise
    finally:
        if urban:
            urban.close()
        record.close()
        events.close()
        for topic in topics.values():
            topic.close()


def run_worker():
    main()
    # main has closed every evidence file and child transport process. Gazebo's
    # native Python callbacks can segfault during interpreter finalization even
    # after a verified landing. Match the isolated RGBD collector's exit path;
    # a runtime or cleanup exception from main never reaches this success exit.
    import os
    import sys

    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    run_worker()
