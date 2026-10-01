"""Scripted lead actor and local aircraft Rules for the CPU pad-queue trial."""

import json
import math
import time

from yokohama_pad_queue import (
    atomic_json,
    clearance,
    clear_window,
    digest,
    make_request,
    require_response,
    require_wait,
)


class PadQueue:
    def __init__(self, root, config, obs, event):
        self.root, self.config, self.obs, self.event = root, config, obs, event
        self.policy = config["world"]["pad_queue"]
        self.epoch = None
        self.last_move = -1
        self.entered = False
        self.permission = None
        self.missing_ack = {}

    def update_actor(self):
        if self.epoch is None:
            return
        from gz.msgs10.pose_pb2 import Pose
        from gz.msgs10.boolean_pb2 import Boolean

        now = self.obs.snapshot()["sim_s"]
        if now - self.last_move < 0.2:
            return
        p = self.policy
        elapsed = now - self.epoch
        start, up, end = (p[k] for k in ("lead_start_xyz_m", "lead_up_xyz_m", "lead_end_xyz_m"))
        t = elapsed - p["occupied_duration_sim_s"]
        if t <= 0:
            xyz = start
        elif t <= p["ascent_duration_sim_s"]:
            f = t / p["ascent_duration_sim_s"]
            xyz = [a + f * (b - a) for a, b in zip(start, up)]
        else:
            f = min(1, (t - p["ascent_duration_sim_s"]) / p["departure_duration_sim_s"])
            xyz = [a + f * (b - a) for a, b in zip(up, end)]
        # Only the authored lead and its visual parcel can be pose-driven.
        # The delivery vehicle is always moved by PX4.
        f = min(1, elapsed / p["unload_duration_sim_s"])
        parcel = [*start[:2], (start[2] - 0.5) * (1 - f) + (p["pad_xyz_m"][2] + 0.15) * f]
        for name, point in ((p["lead_entity"], xyz), (p["parcel_entity"], parcel)):
            observed = self.obs.snapshot()["poses"].get(name)
            if observed and observed["age_s"] <= 1 and math.dist(observed["xyz"], point) <= 0.005:
                continue
            msg = Pose()
            msg.name = name
            msg.position.x, msg.position.y, msg.position.z = point
            msg.orientation.w = 1
            # Transport acknowledgement can be lost even when Gazebo applies
            # the idempotent pose request. Only observed entity poses can clear
            # the pad. The existing 90 sim / 180 wall second occupancy deadline
            # bounds this retry loop; an acknowledgement is not mission progress.
            okay, reply = self.obs.node.request("/world/default/set_pose", msg, Pose, Boolean, 250)
            self.missing_ack[name] = 0 if okay and reply.data else self.missing_ack.get(name, 0) + 1
            if self.missing_ack[name] == 1:
                self.event("lead_pose_ack_missing", entity=name, consecutive=self.missing_ack[name])
        self.last_move = now

    def guard(self, row):
        if not self.entered:
            return
        c = clearance(self.config, row)
        if not c["pad_clear"] or not c["approach_clear"]:
            raise ValueError("Occupied approach; continuation revoked")
        if math.dist(row["vehicle"]["xyz"], row["queue_lead"]["xyz"]) <= 3:
            raise ValueError("Lead-aircraft separation lost")

    def await_clearance(self, sample):
        first = sample()
        require_wait(self.config, first)
        initial = clearance(self.config, first)
        if initial["pad_clear"]:
            raise ValueError("Queue trial must begin with an observed occupied pad")
        self.epoch = first["sim_s"]
        deadline = time.monotonic() + self.policy["maximum_wait_wall_s"]
        self.event("pad_occupied_reported", observation=first)
        tail, sequence = [], 0
        waiting_acknowledged = False
        anchor_resets = first["reset_counters"]
        last_sim_s = first["sim_s"]
        while time.monotonic() < deadline:
            row = sample()
            require_wait(self.config, row)
            if row["reset_counters"] != anchor_resets or not 0 <= row["sim_s"] - last_sim_s <= 2:
                raise ValueError("Queue wait estimator or clock continuity lost")
            last_sim_s = row["sim_s"]
            if row["sim_s"] - self.epoch > self.policy["maximum_wait_sim_s"]:
                raise TimeoutError("Pad occupancy wait deadline")
            c = clearance(self.config, row)
            if c["pad_clear"] and c["approach_clear"]:
                if not tail or row["sim_s"] > tail[-1]["sim_s"]:
                    tail.append(row)
            else:
                tail = []
            ready = clear_window(self.config, tail)
            if waiting_acknowledged and not ready:
                time.sleep(0.2)
                continue
            evidence = tail if ready else [row]
            request = make_request(self.config, sequence, evidence)
            folder = self.root / "pad-decisions" / f"{sequence:03d}"
            folder.mkdir(parents=True)
            atomic_json(folder / "request.json", request)
            self.event(
                "missionos_advice_requested",
                request_id=request["request_id"],
                sequence=sequence,
                observation=row,
            )
            response_path = folder / "response.json"
            response_deadline = time.monotonic() + 2
            while not response_path.exists():
                current = sample()
                require_wait(self.config, current)
                if time.monotonic() > response_deadline:
                    self.event(
                        "missionos_response_timeout",
                        local_action="retain AP hold; bounded SITL termination",
                    )
                    raise TimeoutError("MissionOS advice unavailable; local hold retained")
                time.sleep(0.02)
            response = json.loads(response_path.read_text())
            current = sample()
            action = require_response(self.config, request, response, current)
            self.event("missionos_advice_received", response=response, observation=current)
            if action == "wait_at_current_hold":
                waiting_acknowledged = True
                self.event("pad_wait_executed", observation=current)
                sequence += 1
                continue
            if not waiting_acknowledged:
                raise ValueError("No observed wait before pad continuation")
            self.permission = dict(
                request_id=request["request_id"],
                response_sha256=digest(response),
                rules_checked_at=current,
                operator_approval=self.config["operator_approval"],
            )
            atomic_json(self.root / "pad-entry-permit.json", self.permission)
            self.entered = True
            self.event("pad_entry_authorized", permit=self.permission)
            return
        raise TimeoutError("Pad supervision overall deadline")

    def require_dispatch(self, sample):
        row = sample()
        self.guard(row)
        if (
            self.permission is None
            or row["wall_s"] - self.permission["rules_checked_at"]["wall_s"] > 30
        ):
            raise ValueError("Missing or expired pad entry permission")
        require_wait(self.config, row)
        self.event(
            "pad_entry_dispatch_checked", observation=row, request_id=self.permission["request_id"]
        )
