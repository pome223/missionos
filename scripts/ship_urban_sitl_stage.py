"""Container-local urban experiment; only the scripted obstacle is repositioned."""

import math
import hashlib
import json
import shutil
import threading
import time
import subprocess
from concurrent.futures import ThreadPoolExecutor

from urban_policy import choose_urban_action


class UrbanStage:
    def __init__(self, config, root, topic_class, pose, run, binary):
        self.config, self.root, self.pose, self.run, self.binary = config, root, pose, run, binary
        self.urban = config["urban"]
        self.stopped = threading.Event()
        self.actor_error = None
        self.thread = None
        self.vision = None
        self.anwm_capture = None
        if self.urban.get("onboard_camera"):
            from ship_onboard_camera import VisionBridge

            self.vision = VisionBridge(root, config)
            if self.urban.get("capture_anwm"):
                from ship_anwm_capture import NativeHistoryBridge

                self.anwm_capture = NativeHistoryBridge(root, config)
        self.topics = [
            topic_class(
                f"/world/default/model/{name}/link/link/sensor/contact/contact",
                root / f"{name}-contacts.txt",
            )
            for name in self.urban["collision_entities"]
        ]

        def publisher_info(name):
            return self.run(
                [
                    "gz",
                    "topic",
                    "-i",
                    "-t",
                    f"/world/default/model/{name}/link/link/sensor/contact/contact",
                ]
            )

        with ThreadPoolExecutor(max_workers=7) as pool:
            receipts = dict(
                zip(
                    self.urban["collision_entities"],
                    pool.map(publisher_info, self.urban["collision_entities"]),
                )
            )
        (root / "urban-monitor-publishers.json").write_text(json.dumps(receipts, indent=2))
        self.monitors_connected = all(
            "gz.msgs.Contacts" in text.split("Subscribers")[0] for text in receipts.values()
        )

    def observe(self, poses):
        if self.actor_error:
            raise RuntimeError(self.actor_error)
        return {
            "urban_obstacle": self.pose(poses, "urban_obstacle"),
            "urban_buildings": [self.pose(poses, b["name"]) for b in self.urban["buildings"]],
            "urban_contact_monitors_connected": self.monitors_connected
            and all(t.process.poll() is None for t in self.topics),
            "urban_contact_observed": any(t.contact_seen for t in self.topics),
        }

    def move_actor(self):
        if "stationary_obstacle_east_m" in self.urban:
            return  # The collision-enabled world owns this fixed obstacle.
        started = time.monotonic()
        if self.vision:
            with (self.root / "urban-actor.log").open("w") as log:
                process = subprocess.Popen(
                    ["python3", str(self.root / "ship_onboard_camera.py"), "--actor", str(started)],
                    stdout=log,
                    stderr=log,
                )
                try:
                    while process.poll() is None and not self.stopped.wait(0.2):
                        pass
                    if process.poll() not in (None, 0):
                        self.actor_error = "Isolated obstacle actor failed; see urban-actor.log"
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=3)
            return
        failures = 0
        while not self.stopped.is_set():
            elapsed = time.monotonic() - started
            if self.urban["case"] == "brake_stop":
                x = 4 * elapsed - 0.5 * elapsed**2 if elapsed <= 4 else 8 + 2 * max(0, elapsed - 40)
                x = min(16.0, x)
            else:
                x = min(16.0, elapsed * self.urban["scripted_obstacle_speed_mps"])
            try:
                response = self.run(
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
                        "2000",
                        "--req",
                        f'name: "urban_obstacle" position {{ x: {x} y: {self.urban["obstacle_north_m"]} z: 30 }} orientation {{ w: 1 }}',
                    ],
                    3,
                )
                if "data: true" not in response:
                    raise RuntimeError("Obstacle pose request rejected")
                failures = 0
                if x >= 16:
                    return
            except Exception as exc:
                failures += 1
                with (self.root / "urban-actor-retries.jsonl").open("a") as log:
                    log.write(
                        json.dumps(
                            {
                                "elapsed_s": time.monotonic() - started,
                                "consecutive_failures": failures,
                                "reason": str(exc),
                            }
                        )
                        + "\n"
                    )
                if failures >= 3:
                    self.actor_error = f"Scripted obstacle failed: {exc}"
                    return
            self.stopped.wait(0.1 if self.vision else 0.2)

    def execute(self, sample, wait_for, event, upload, activate, clock):
        def at_entry(v):
            if self.vision:
                if not v["position_valid"] or any(x is None for x in v["local_ned"]):
                    return False
                north, east, down = v["local_ned"]
                point = [east, north, -down]
            else:
                point = v["vehicle"]["xyz"] if v["vehicle"] else None
            return (
                point
                and math.hypot(point[0], point[1] - self.urban["entry_north_m"]) < 5
                and point[2] > 20
                and all(x is not None for x in v["velocity_ned"])
                and math.sqrt(sum(x * x for x in v["velocity_ned"])) < 0.5
                and v["urban_contact_monitors_connected"]
            )

        wait_for(at_entry, self.config["timeout_s"])
        if self.vision:
            self.run([self.binary + "commander", "mode", "auto:loiter"])
            wait_for(lambda v: v["nav_state"] == 4, 10)
        event("urban_entry_observed")
        if self.urban.get("vla_guard_limits"):
            from ship_vla_adapter import inspect_live_smoke

            receipt = inspect_live_smoke(sample(), self.config)
            path = self.root / "vla-guard-smoke.json"
            path.write_text(json.dumps(receipt, indent=2, allow_nan=False))
            event(
                "vla_guard_inspected", receipt_sha256=hashlib.sha256(path.read_bytes()).hexdigest()
            )
            if not receipt["all_expected"]:
                raise RuntimeError("VLA guard smoke failed; no urban dispatch")
        if self.urban.get("vla_executor_contract"):
            from ship_vla_executor import execute_fixture

            provider = None
            if self.urban.get("aerovla_live"):
                from ship_aerovla_live import LiveBridge

                provider = LiveBridge(self.root, self.config)
            execute_fixture(
                self.root,
                self.config,
                sample,
                event,
                self.run,
                self.binary,
                clock,
                provider=provider,
            )
        if self.anwm_capture:
            anchor = sample()
            event("native_history_started", source_sample_elapsed_s=anchor["elapsed_s"])
            self.anwm_capture.start()
        self.thread = threading.Thread(target=self.move_actor, daemon=True)
        self.thread.start()
        event(
            "urban_actor_started",
            motion="stationary_collision_box"
            if "stationary_obstacle_east_m" in self.urban
            else "scripted_collision_box",
            observation_source=self.urban["policy_observation_source"],
        )
        if self.vision:
            if self.anwm_capture:
                self.anwm_capture.finish(sample, event)
            history, decision = self.vision.decide(sample, event)
        else:
            history = []
            while (
                len(history) < 3 or history[-1]["observed_at_s"] - history[0]["observed_at_s"] < 2
            ):
                v = sample()
                if not v["poses_fresh"] or not v["urban_obstacle"]:
                    raise RuntimeError("Urban perception stale")
                history.append(
                    {"observed_at_s": v["elapsed_s"], "obstacle_x_m": v["urban_obstacle"]["xyz"][0]}
                )
                time.sleep(0.5)
            decision = choose_urban_action(
                history, self.urban["policy"], airspeed_mps=self.urban["airspeed_mps"]
            )
        event("urban_decision", history=history, decision=decision)
        if decision["action"] == "wait":
            if self.vision:
                self.vision.wait_clear(sample, event)
            else:
                wait_for(lambda v: v["urban_obstacle"] and v["urban_obstacle"]["xyz"][0] >= 13, 120)
        self.run([self.binary + "commander", "mode", "auto:loiter"])
        wait_for(lambda v: v["nav_state"] == 4, 10)
        shutil.copyfile(
            self.root / f"urban-{decision['action']}-upload.py", self.root / "urban-upload.py"
        )
        upload("urban")
        if self.urban.get("anwm_static"):
            row = sample()
            response = self.vision.exchange([row], "clearance", sample=sample)
            observation = response["history"][0]
            if (
                clock() - row["elapsed_s"] > 2
                or row["nav_state"] != 4
                or abs(observation["obstacle_x_m"] - decision["anwm"]["revalidated_obstacle_x_m"])
                > 1.5
            ):
                raise RuntimeError("WAM stationary scene changed before dispatch")
            event(
                "anwm_dispatch_revalidated",
                sample_elapsed_s=row["elapsed_s"],
                response_sha256=decision["anwm"]["response_sha256"],
            )
        activate("urban", action=decision["action"])
        if self.urban.get("vla_executor_contract"):
            start = sample()["local_ned"]
            resumed = wait_for(
                lambda row: (
                    row["nav_state"] == 3
                    and row["position_valid"]
                    and math.dist(start, row["local_ned"]) >= 3
                    and math.hypot(*row["velocity_ned"]) > 0.5
                ),
                15,
            )
            event("vla_mission_resumed", source_sample_elapsed_s=resumed["elapsed_s"])

    def close(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=4)
        for topic in self.topics:
            topic.close()
