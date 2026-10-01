"""Scripted lead actor and local aircraft Rules for the CPU pad-queue trial."""

import json
import math
import time

from yokohama_pad_queue import (
    approved_hold,
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
        self.sequence = 0
        self.hold = None
        self.fault_started_sim_s = None
        self.camera = None
        if config["world"].get("pad_state_advisory"):
            from yokohama_pad_advisory_worker import PadCameraRecorder

            self.camera = PadCameraRecorder(root, config, obs)

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
        if self.fault_started_sim_s is not None:
            # CPU fault injection only: the lead flies back over the pad.
            f = min(1, (now - self.fault_started_sim_s) / p["departure_duration_sim_s"])
            xyz = [a + f * (b - a) for a, b in zip(end, up)]
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
        if self.camera:
            self.camera.start()
        try:
            return self._await_clearance(sample)
        finally:
            if self.camera:
                self.camera.close(self.obs.snapshot())

    def _await_clearance(self, sample):
        first = sample()
        require_wait(self.config, first)
        initial = clearance(self.config, first)
        if initial["pad_clear"]:
            raise ValueError("Queue trial must begin with an observed occupied pad")
        self.epoch = first["sim_s"]
        deadline = time.monotonic() + self.policy["maximum_wait_wall_s"]
        self.event("pad_occupied_reported", observation=first)
        tail = []
        waiting_acknowledged = False
        anchor_resets = first["reset_counters"]
        last_sim_s = first["sim_s"]
        last_request_sim_s = -1
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
            periodic = self.camera and row["sim_s"] - last_request_sim_s >= 2
            if waiting_acknowledged and not ready and not periodic:
                time.sleep(0.2)
                continue
            if self.camera and row["sim_s"] - last_request_sim_s < 2:
                time.sleep(0.2)
                continue
            evidence = tail if ready else [row]
            last_request_sim_s = row["sim_s"]
            request, response, current, action = self._ask(sample, evidence, row)
            if action == "wait_at_current_hold":
                self.event(
                    "pad_wait_reaffirmed" if waiting_acknowledged else "pad_wait_executed",
                    observation=current,
                )
                waiting_acknowledged = True
                continue
            if not waiting_acknowledged:
                raise ValueError("No observed wait before pad continuation")
            self._permit(request, response, current)
            self.entered = True
            self.event("pad_entry_authorized", permit=self.permission)
            return
        raise TimeoutError("Pad supervision overall deadline")

    def _ask(self, sample, evidence, row):
        """One aircraft-to-MissionOS exchange at the current approved hold."""
        folder = self.root / "pad-decisions" / f"{self.sequence:03d}"
        folder.mkdir(parents=True)
        history = self.camera.capture(folder, row["sim_s"]) if self.camera else None
        request = make_request(self.config, self.sequence, evidence, history, self.hold)
        atomic_json(folder / "request.json", request)
        self.event(
            "missionos_advice_requested",
            request_id=request["request_id"],
            sequence=self.sequence,
            observation=row,
        )
        response_path = folder / "response.json"
        response_deadline = time.monotonic() + 2
        while not response_path.exists():
            current = sample()
            require_wait(self.config, current, self.hold)
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
        self.sequence += 1
        return request, response, current, action

    def _permit(self, request, response, current):
        self.permission = dict(
            request_id=request["request_id"],
            response_sha256=digest(response),
            rules_checked_at=current,
            operator_approval=self.config["operator_approval"],
        )
        if self.hold is not None:
            self.permission["hold_xyz_m"] = self.hold
        atomic_json(self.root / "pad-entry-permit.json", self.permission)

    def reconfirm(self, sample, reason):
        """Fresh stable-clear evidence and a new MissionOS response at the current hold.

        Used only after the first entry permission. Any reoccupation during the
        window is fatal through ``guard``; this never waits for a second clearance.
        """
        if not self.entered or self.camera:
            raise ValueError("Reconfirmation requires a prior pose-only entry permission")
        deadline = time.monotonic() + self.policy["reconfirm_timeout_wall_s"]
        tail = []
        while time.monotonic() < deadline:
            row = sample()
            require_wait(self.config, row, self.hold)
            c = clearance(self.config, row)
            if not (c["pad_clear"] and c["approach_clear"]):
                raise ValueError("Pad reoccupied during reconfirmation; no model authority")
            if tail and not 0 < row["sim_s"] - tail[-1]["sim_s"] <= 2:
                if row["sim_s"] == tail[-1]["sim_s"]:
                    time.sleep(0.2)
                    continue
                raise ValueError("Reconfirmation clock continuity lost")
            tail.append(row)
            if clear_window(self.config, tail, self.hold):
                request, response, current, action = self._ask(sample, tail, row)
                if action != "enter_delivery_approach":
                    raise ValueError("MissionOS did not reconfirm the clear approach")
                self._permit(request, response, current)
                self.event("pad_entry_reconfirmed", permit=self.permission, reason=reason)
                return self.permission
            time.sleep(0.2)
        raise TimeoutError("Pad reconfirmation deadline")

    def trigger_fault(self, operation, cycle):
        """Start the injected lead return at the D3 WAM request itself."""
        if (
            self.policy.get("fault_lead_return") != "on_d3_wam_request"
            or (operation, cycle) != ("wam", 3)
            or self.fault_started_sim_s is not None
        ):
            return
        self.fault_started_sim_s = self.obs.snapshot()["sim_s"]
        self.last_move = -1
        self.event("fault_lead_return_triggered", sim_s=self.fault_started_sim_s)

    def move_hold(self, xyz, permit_id):
        """Adopt one reached, authorized model endpoint as the pad hold."""
        self.hold = approved_hold(self.config, [float(v) for v in xyz])
        self.event("pad_hold_moved", hold_xyz_m=self.hold, permit_id=permit_id)

    def require_dispatch(self, sample):
        row = sample()
        self.guard(row)
        if (
            self.permission is None
            or row["wall_s"] - self.permission["rules_checked_at"]["wall_s"] > 30
        ):
            raise ValueError("Missing or expired pad entry permission")
        require_wait(self.config, row, self.hold)
        if self.permission.get("hold_xyz_m") != self.hold:
            raise ValueError("Pad entry permission was issued at a different hold")
        self.event(
            "pad_entry_dispatch_checked", observation=row, request_id=self.permission["request_id"]
        )
