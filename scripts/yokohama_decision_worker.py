"""Network-free simulator side of city stop/observe/decide/move.

Native inference has no PX4 connection. Every mailbox is single-use, bound to
this run/config, and closed before the AP completes the remaining route.
"""

from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
import time


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def physical_heading(row):
    """Independent Gazebo camera/vehicle heading, in world NED radians."""
    w, x, y, z = row["vehicle"]["quat_wxyz"]
    return math.atan2(1 - 2 * (y * y + z * z), 2 * (x * y + w * z))


class CityDecisions:
    def __init__(self, root, config, sample, event, clock):
        self.root, self.config, self.sample, self.event, self.clock = (
            Path(root),
            config,
            sample,
            event,
            clock,
        )
        self.sequence, self.cycle = 0, 0
        self.started, self.closed, self.active = False, False, False
        self.completed = []
        self.paired_view = None

    def held(self, anchor):
        row = self.sample()
        if (
            row["nav_state"] != 4
            or row["arming_state"] != 2
            or row["landed"] is not False
            or row["position_valid"] is not True
            or row["battery_fraction"] < 0.2
            or math.hypot(*row["velocity_ned"]) > 0.3
            or math.dist(row["vehicle"]["xyz"], anchor["vehicle"]["xyz"]) > 0.5
            or abs(math.remainder(row["heading_ned_rad"] - anchor["heading_ned_rad"], 2 * math.pi))
            > 0.03
            or row["reset_counters"] != anchor["reset_counters"]
            or abs(math.remainder(physical_heading(row) - physical_heading(anchor),
                                  2 * math.pi)) > 0.03
        ):
            raise ValueError("City decision hold, reserve, heading or estimator continuity lost")
        return row

    def exchange(self, operation, row, **fields):
        if self.closed or (operation not in {"start", "stop"} and not self.active):
            raise ValueError("City session inactive; late responses cannot restore it")
        self.sequence += 1
        message = dict(
            run_id=self.config["run_id"],
            config_sha256=digest(self.config),
            operation=operation,
            cycle=self.cycle,
            sequence=self.sequence,
            observation=row,
            **fields,
        )
        stem = self.root / "decisions" / f"{self.sequence:03d}"
        temp = stem.with_name(stem.name + "-request.tmp")
        temp.write_text(json.dumps(message, allow_nan=False))
        temp.replace(stem.with_name(stem.name + "-request.json"))
        response = stem.with_name(stem.name + "-response.json")
        end = (
            time.monotonic()
            + {"start": 180, "stop": 65, "vla": 75, "wam": 75, "authorize": 2, "activate": 2}[
                operation
            ]
        )
        self.event(
            "city_request", operation=operation, cycle=self.cycle, request_sha256=digest(message)
        )
        while time.monotonic() < end:
            self.held(row)
            if response.exists():
                value = json.loads(response.read_text())
                if (
                    value.get("request_sha256") != digest(message)
                    or value.get("operation") != operation
                    or value.get("run_id") != self.config["run_id"]
                    or self.closed
                ):
                    raise ValueError("Late or cross-cycle response")
                self.event(
                    "city_response",
                    operation=operation,
                    cycle=self.cycle,
                    response_sha256=digest(value),
                )
                if "error" in value:
                    raise ValueError(value["error"])
                return value["value"]
            time.sleep(0.05)
        raise TimeoutError("City model exchange deadline: " + operation)

    def capture(self, obs, label, anchor):
        after = anchor["sim_s"]
        end = time.monotonic() + 20
        while time.monotonic() < end:
            self.held(anchor)
            capture = obs.capture_history(label, after)
            if capture is not None:
                self.event("city_history_captured", cycle=self.cycle, capture=capture)
                return capture
            time.sleep(0.05)
        raise TimeoutError("City RGBD history missing")

    def decide(self, obs, next_target):
        self.cycle += 1
        anchor = self.sample()
        self.held(anchor)
        if not self.started:
            self.started = True
            self.exchange("start", anchor)
            self.active = True
        capture = self.capture(obs, f"city-{self.cycle:02d}-vla-capture", self.sample())
        vla = self.exchange(
            "vla", self.held(anchor), capture=capture, next_target_world_xyz_m=next_target
        )
        # A new uninterrupted history follows the VLA call; old imagery is not
        # reused to manufacture a second observation.
        capture = self.capture(obs, f"city-{self.cycle:02d}-wam-capture", self.sample())
        wam = self.exchange("wam", self.held(anchor), capture=capture, vla=vla)
        if wam.get("passed") is not True:
            raise ValueError("WAM visible-structure consistency rejected; no segment dispatched")
        if self.config["decisions"].get("capture_paired_views"):
            record = json.loads((self.root / capture["file"]).read_text())
            cutoff = record["frames"][-1]["stamp_ns"] / 1e9
            self.paired_view = dict(
                schema_version="yokohama_paired_views.v1",
                run_id=self.config["run_id"], cycle=self.cycle,
                input_capture=capture, input_cutoff_sim_s=cutoff,
                hold_outcome=self.capture_evaluation(obs, "hold", anchor, cutoff),
                evaluation_only=True, model_predictions_used_for_dispatch=False,
                decision_backend="fixture", future_observations_sent_to_model=False,
            )
        current = self.held(anchor)
        permit = self.exchange("authorize", current, next_target_world_xyz_m=next_target)
        if (
            permit.get("run_id") != self.config["run_id"]
            or permit.get("config_sha256") != digest(self.config)
            or permit.get("cycle") != self.cycle
            or permit.get("observation_sha256") != digest(current)
            or self.clock() > permit["expires_at_worker_wall_s"]
            or permit.get("upload_name") != f"city-{self.cycle:02d}"
            or permit.get("vla_response_sha256") != vla["vla_response_sha256"]
            or permit.get("wam_assessment_sha256") != digest(wam)
            or permit.get("rules", {}).get("allowed") is not True
        ):
            raise ValueError("City segment permit is stale or unbound")
        script = self.root / (permit["upload_name"] + "-upload.py")
        if hashlib.sha256(script.read_bytes()).hexdigest() != permit["upload_sha256"]:
            raise ValueError("Segment upload differs from authorized script")
        self.held(current)
        self.event("city_upload_permit", permit=permit)
        return permit

    def capture_evaluation(self, obs, suffix, anchor, after):
        """Held-out measurements never enter the model mailbox or authorization."""
        end = time.monotonic() + 20
        while time.monotonic() < end:
            self.held(anchor)
            result = obs.capture_history(
                f"city-{self.cycle:02d}-evaluation-{suffix}", after, evaluation_only=True
            )
            if result is not None:
                self.event("city_evaluation_captured", cycle=self.cycle, capture=result)
                return result
            time.sleep(0.05)
        raise TimeoutError("Held-out RGBD evaluation capture missing")

    def record_arrival(self, obs, permit, arrived):
        if not self.config["decisions"].get("capture_paired_views"):
            return
        self.paired_view.update(
            prepared_candidate=permit["candidate"], permit_sha256=digest(permit),
            arrival_observation=arrived,
            endpoint_outcome=self.capture_evaluation(obs, "endpoint", arrived, arrived["sim_s"]),
            endpoint_time_alignment_verified=False,
        )
        path = self.root / f"city-{self.cycle:02d}-paired-views.json"
        path.write_text(json.dumps(self.paired_view, indent=2, allow_nan=False) + "\n")
        self.event("city_paired_views_recorded", file=path.name,
                   sha256=hashlib.sha256(path.read_bytes()).hexdigest())

    def activation_permit(self, prepared):
        current = self.sample()
        permit = self.exchange("activate", current, prepared_permit_sha256=digest(prepared))
        if (
            permit.get("prepared_permit_sha256") != digest(prepared)
            or permit.get("observation_sha256") != digest(current)
            or permit.get("candidate") != prepared["candidate"]
            or self.clock() > permit["expires_at_worker_wall_s"]
        ):
            raise ValueError("Activation permit is stale or unbound")
        self.held(current)
        self.event("city_permit_consumed", permit=permit)
        return permit

    def stop(self):
        if self.closed:
            return
        try:
            if self.started:
                self.active = False
                self.event("city_session_revoked", completed_updates=len(self.completed))
                self.exchange("stop", self.sample())
        finally:
            self.closed = True
            self.active = False
        # Deliberately attempt a replay through the live guard after revocation.
        try:
            self.exchange("authorize", {})
        except ValueError:
            self.event("city_late_response_rejected", session_revoked=True)
        else:
            raise AssertionError("Revoked session accepted new authority")
