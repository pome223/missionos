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


class HoldInterrupted(ValueError):
    """Transient movement before dispatch; other authority failures stay fatal."""


def attempt_name(cycle, attempt=0):
    name = f"city-{cycle:02d}"
    return name + (f"-attempt-{attempt:02d}" if attempt else "")


POINT_PHASES = {"D1": "00-D1", "D2": "01-D2", "D3": "02-D3"}


def decision_phases(config):
    """Approved model-decision holds by cycle; D3 exists only for the pad approach."""
    points = config.get("decisions", {}).get("points", ["D1", "D2"])
    if points not in (["D1", "D2"], ["D1", "D2", "D3"]) or (
        "D3" in points and not config["decisions"].get("pad_approach")
    ):
        raise ValueError("Unapproved model decision points")
    return {i: POINT_PHASES[name] for i, name in enumerate(points, 1)}


def require_city_request(config, message):
    """Gate model startup and calls by both the approved phase and measured position.

    Stop is always permitted for cleanup; it never invokes model inference.
    """
    if message["operation"] == "stop":
        return
    cycle = message.get("cycle")
    expected = decision_phases(config).get(cycle)
    row = message.get("observation", {})
    stages = [s for s in config.get("flight_stages", []) if s["name"] == expected]
    xyz = row.get("vehicle", {}).get("xyz", [])
    if (
        not expected
        or len(stages) != 1
        or row.get("phase") != expected
        or len(xyz) != 3
        or not all(math.isfinite(v) for v in xyz)
        or math.dist(xyz, stages[0]["target_world_xyz_m"]) > 1
        or row.get("nav_state") != 4
        or row.get("arming_state") != 2
        or row.get("landed") is not False
        or (message["operation"] == "start" and cycle != 1)
    ):
        raise ValueError("Model request outside approved inland phase/hold")


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
        self.attempt = 0
        # Optional executor-side check between a passing forecast and authority.
        self.before_authorize = None
        # Optional CPU fault-injection observer; it cannot change a request.
        self.on_request = None

    def held(self, anchor, *, require_stationary_view=True, max_view_drift_rad=0.03):
        row = self.sample()
        fatal = (
            row["nav_state"] != 4
            or row["arming_state"] != 2
            or row["landed"] is not False
            or row["position_valid"] is not True
            or row["battery_fraction"] < 0.2
            or row["reset_counters"] != anchor["reset_counters"]
            or not all(
                math.isfinite(v)
                for v in [
                    *row["vehicle"]["xyz"],
                    *row["velocity_ned"],
                    row["heading_ned_rad"],
                    row["battery_fraction"],
                    physical_heading(row),
                ]
            )
        )
        moved = (
            math.hypot(*row["velocity_ned"]) > 0.3
            or math.dist(row["vehicle"]["xyz"], anchor["vehicle"]["xyz"]) > 0.5
            or abs(math.remainder(row["heading_ned_rad"] - anchor["heading_ned_rad"], 2 * math.pi))
            > 0.03
            or (
                require_stationary_view
                and abs(
                    math.remainder(physical_heading(row) - physical_heading(anchor), 2 * math.pi)
                )
                > max_view_drift_rad
            )
        )
        if fatal or moved:
            self.event(
                "city_hold_rejected",
                require_stationary_view=require_stationary_view,
                max_view_drift_rad=max_view_drift_rad,
                observation=row,
                anchor=anchor,
            )
            error = ValueError if fatal else HoldInterrupted
            raise error("City decision hold, reserve, heading or estimator continuity lost")
        return row

    def exchange(self, operation, row, **fields):
        if self.closed or (operation not in {"start", "stop", "resume"} and not self.active):
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
        if self.attempt:
            message["attempt"] = self.attempt
        require_city_request(self.config, message)
        if self.on_request:
            self.on_request(operation, self.cycle)
        stem = self.root / "decisions" / f"{self.sequence:03d}"
        temp = stem.with_name(stem.name + "-request.tmp")
        temp.write_text(json.dumps(message, allow_nan=False))
        temp.replace(stem.with_name(stem.name + "-request.json"))
        response = stem.with_name(stem.name + "-response.json")
        end = (
            time.monotonic()
            + {
                "start": self.config.get("decisions", {}).get("startup_timeout_s", 180),
                "stop": 65,
                "resume": self.config.get("decisions", {}).get("startup_timeout_s", 180) + 5,
                "vla": 75,
                "wam": 75,
                "authorize": 2,
                "activate": 2,
            }[operation]
        )
        self.event(
            "city_request",
            operation=operation,
            cycle=self.cycle,
            attempt=self.attempt,
            request_sha256=digest(message),
        )
        lifecycle_only = (
            operation in {"start", "stop", "resume"}
            and self.config.get("decisions", {}).get("wam_profile") == "motion-v4"
        )
        while time.monotonic() < end:
            world_view = (
                operation in {"vla", "wam"}
                and self.config.get("decisions", {}).get("wam_profile") == "motion-v4"
            )
            if operation == "stop":
                # Cleanup must remain possible while the aircraft is moving.
                self.sample()
            else:
                self.held(
                    row,
                    require_stationary_view=not lifecycle_only,
                    max_view_drift_rad=0.25 if world_view else 0.03,
                )
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

    def revoke_attempt(self):
        self.active = False
        record = dict(
            run_id=self.config["run_id"],
            config_sha256=digest(self.config),
            cycle=self.cycle,
            attempt=self.attempt,
        )
        path = self.root / "decisions" / (attempt_name(self.cycle, self.attempt) + "-revoked.json")
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(record))
        temp.replace(path)
        self.event("city_attempt_revoked", **record)

    def recover_hold(self, anchor):
        """Keep the existing AP loiter target; never move it to follow a drift."""
        policy = self.config["decisions"]["hold_recovery"]
        deadline = time.monotonic() + policy["timeout_wall_s"]
        stable, previous = None, None
        self.event("city_recovery_started", cycle=self.cycle, attempt=self.attempt, anchor=anchor)
        while time.monotonic() < deadline:
            row = self.sample()
            if (
                row["nav_state"] != 4
                or row["arming_state"] != 2
                or row["landed"] is not False
                or row["position_valid"] is not True
                or not math.isfinite(row["battery_fraction"])
                or row["battery_fraction"] < 0.2
                or row["reset_counters"] != anchor["reset_counters"]
                or not all(
                    math.isfinite(v)
                    for v in [
                        *row["vehicle"]["xyz"],
                        *row["velocity_ned"],
                        row["heading_ned_rad"],
                        physical_heading(row),
                        row["sim_s"],
                    ]
                )
                or math.dist(row["vehicle"]["xyz"], anchor["vehicle"]["xyz"])
                > policy.get("maximum_anchor_distance_m", 1)
                # Gazebo clock snapshots can repeat while pose/PX4 samples
                # advance. Repeats earn no stable time; reversals/gaps fail.
                or (previous is not None and not 0 <= row["sim_s"] - previous <= 2)
            ):
                raise ValueError("AP recovery left its hold, reserve or estimator bounds")
            previous = row["sim_s"]
            okay = (
                math.dist(row["vehicle"]["xyz"], anchor["vehicle"]["xyz"]) <= 0.5
                and math.hypot(*row["velocity_ned"]) <= 0.3
                and abs(
                    math.remainder(row["heading_ned_rad"] - anchor["heading_ned_rad"], 2 * math.pi)
                )
                <= 0.03
                and abs(
                    math.remainder(physical_heading(row) - physical_heading(anchor), 2 * math.pi)
                )
                <= 0.03
            )
            stable = (row["sim_s"] if stable is None else stable) if okay else None
            if stable is not None and row["sim_s"] - stable >= policy["stable_sim_s"]:
                self.event(
                    "city_recovery_held",
                    cycle=self.cycle,
                    attempt=self.attempt,
                    stable_since_sim_s=stable,
                    observation=row,
                )
                return
            time.sleep(0.05)
        raise TimeoutError("AP recovery stable hold deadline")

    def prepare_segment(self, obs, next_target, upload):
        """Retry only before activation, with revoked authority and new imagery."""
        policy = self.config["decisions"].get("hold_recovery")
        if not policy:
            prepared = self.decide(obs, next_target)
            upload(prepared["upload_name"])
            return self.activation_permit(prepared)
        self.cycle += 1
        anchor = self.sample()
        require_city_request(
            self.config, dict(operation="resume", cycle=self.cycle, observation=anchor)
        )
        for attempt in range(1, policy["max_attempts"] + 1):
            self.attempt = attempt
            try:
                prepared = self.decide(obs, next_target, new_cycle=False)
                upload(prepared["upload_name"])
                return self.activation_permit(prepared)
            except HoldInterrupted:
                self.revoke_attempt()
                if attempt == policy["max_attempts"]:
                    raise
                self.recover_hold(anchor)
        raise AssertionError("Recovery attempt bound")

    def decide(self, obs, next_target, *, new_cycle=True):
        if new_cycle:
            self.cycle += 1
        anchor = self.sample()
        self.held(anchor)
        if not self.started:
            self.started = True
            self.exchange("start", anchor)
            self.active = True
        elif not self.active:
            self.exchange("resume", anchor)
            self.active = True
        refresh = self.config.get("decisions", {}).get("wam_profile") == "motion-v4"
        if refresh:
            anchor = self.sample()
            self.event(
                "city_observation_reanchored", phase_boundary="after_startup", observation=anchor
            )
        name = attempt_name(self.cycle, self.attempt)
        capture = self.capture(obs, name + "-vla-capture", self.sample())
        vla = self.exchange(
            "vla", self.held(anchor), capture=capture, next_target_world_xyz_m=next_target
        )
        if refresh:
            anchor = self.sample()
        # A new uninterrupted history follows the VLA call; old imagery is not
        # reused to manufacture a second observation.
        capture = self.capture(obs, name + "-wam-capture", self.sample())
        if refresh:
            anchor = self.held(anchor)
            self.event(
                "city_observation_reanchored",
                phase_boundary="fresh_WAM_capture",
                observation=anchor,
            )
        wam = self.exchange("wam", self.held(anchor), capture=capture, vla=vla)
        if wam.get("passed") is not True:
            raise ValueError("WAM visible-structure consistency rejected; no segment dispatched")
        if self.config["decisions"].get("capture_paired_views"):
            record = json.loads((self.root / capture["file"]).read_text())
            cutoff = record["frames"][-1]["stamp_ns"] / 1e9
            self.paired_view = dict(
                schema_version="yokohama_paired_views.v1",
                run_id=self.config["run_id"],
                cycle=self.cycle,
                input_capture=capture,
                input_cutoff_sim_s=cutoff,
                hold_outcome=self.capture_evaluation(obs, "hold", anchor, cutoff),
                evaluation_only=True,
                model_predictions_used_for_dispatch=False,
                decision_backend="fixture",
                future_observations_sent_to_model=False,
            )
        if self.before_authorize:
            self.before_authorize()
        current = self.held(anchor, max_view_drift_rad=0.25 if refresh else 0.03)
        permit = self.exchange("authorize", current, next_target_world_xyz_m=next_target)
        if (
            permit.get("run_id") != self.config["run_id"]
            or permit.get("config_sha256") != digest(self.config)
            or permit.get("cycle") != self.cycle
            or permit.get("observation_sha256") != digest(current)
            or self.clock() > permit["expires_at_worker_wall_s"]
            or permit.get("upload_name") != name
            or permit.get("attempt", 0) != self.attempt
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
            prepared_candidate=permit["candidate"],
            permit_sha256=digest(permit),
            arrival_observation=arrived,
            endpoint_outcome=self.capture_evaluation(obs, "endpoint", arrived, arrived["sim_s"]),
            endpoint_time_alignment_verified=False,
        )
        path = self.root / f"city-{self.cycle:02d}-paired-views.json"
        path.write_text(json.dumps(self.paired_view, indent=2, allow_nan=False) + "\n")
        self.event(
            "city_paired_views_recorded",
            file=path.name,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )

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
