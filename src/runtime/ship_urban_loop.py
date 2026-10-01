"""Bounded stop/observe/decide/move control, independent of model and AP transports.

This module grants only the supplied, preapproved urban segment authority. It
does not provision compute, implement a PX4 adapter, or verify a whole mission.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import re
import time
from uuid import uuid4


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def finite(value):
    return type(value) in (float, int) and math.isfinite(value)


def vector(value):
    return isinstance(value, (list, tuple)) and len(value) == 3 and all(map(finite, value))


class LoopRejected(ValueError):
    pass


@dataclass(frozen=True)
class UrbanLoopPlan:
    """An explicitly approved convex corridor in local NED metres.

    Obstacle clearance still needs an independent Rules adapter. The entry and
    exit are both inside this corridor; offshore travel belongs to the AP.
    """

    run_id: str
    approval_ref: str
    execution_scope: str
    lower_ned_m: tuple[float, float, float] = (1010.0, -20.0, -31.0)
    upper_ned_m: tuple[float, float, float] = (1200.0, 20.0, -29.0)
    coast_north_m: float = 1000.0
    entry_ned_m: tuple[float, float, float] = (1015.0, 0.0, -30.0)
    updates: int = 2
    max_leg_m: float = 30.0
    observation_age_s: float = 2.0
    hold_drift_m: float = 0.5
    hold_speed_mps: float = 0.3
    settle_s: float = 2.0
    max_sample_gap_s: float = 0.5
    target_error_m: float = 0.25
    startup_timeout_s: float = 120.0
    inference_timeout_s: float = 75.0
    segment_timeout_s: float = 60.0
    total_timeout_s: float = 600.0
    reserve_fraction: float = 0.2

    def __post_init__(self):
        if (
            not self.run_id
            or not self.approval_ref
            or self.execution_scope not in {"fixture", "sim"}
        ):
            raise ValueError("urban_loop_requires_bound_sim_or_fixture_approval")
        if type(self.updates) is not int or not 2 <= self.updates <= 8:
            raise ValueError("urban_loop_requires_two_to_eight_updates")
        if not all(vector(v) for v in (self.lower_ned_m, self.upper_ned_m, self.entry_ned_m)):
            raise ValueError("urban_loop_invalid_geometry")
        for name in (
            "max_leg_m",
            "observation_age_s",
            "hold_drift_m",
            "hold_speed_mps",
            "settle_s",
            "max_sample_gap_s",
            "target_error_m",
            "startup_timeout_s",
            "inference_timeout_s",
            "segment_timeout_s",
            "total_timeout_s",
            "reserve_fraction",
        ):
            if not finite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError("urban_loop_invalid_limit:" + name)
        if (
            not finite(self.coast_north_m)
            or self.lower_ned_m[0] <= self.coast_north_m
            or self.reserve_fraction >= 1
            or any(a >= b for a, b in zip(self.lower_ned_m, self.upper_ned_m))
            or not self.contains(self.entry_ned_m)
        ):
            raise ValueError("urban_loop_corridor_must_be_inland")

    def contains(self, point):
        return vector(point) and all(
            lo <= x <= hi for lo, x, hi in zip(self.lower_ned_m, point, self.upper_ned_m)
        )


class UrbanLoopRuntime:
    """Run one session; adapters must retain their own raw observations/receipts.

    AP: observe(), hold(), dispatch(permit), return_handoff(). Models: start(),
    infer(request), stop(), stopped(). Rules: authorize(start, target, observation).
    All blocking model work is monitored while AP holds. Model stop must cancel
    outstanding work, reap its owned processes and return boundedly. A transport
    timeout alone is never interpreted as a stopped model.
    """

    def __init__(self, plan, ap, models, rules, *, clock=time.monotonic, poll_s=0.05):
        self.plan, self.ap, self.models, self.rules = plan, ap, models, rules
        self.clock, self.poll_s = clock, poll_s
        self.session_id = uuid4().hex
        self.plan_sha256 = digest(asdict(plan))
        self.events = []
        self.pending = None
        self.active = False
        self.used = False
        self.epoch = 0
        self.cycle = 0
        self.last_observation = None
        self.last_arrival_s = -1.0
        self.input_images = set()
        self.started = None

    def record(self, event, **fields):
        item = {
            "event": event,
            "at_s": self.clock(),
            "run_id": self.plan.run_id,
            "session_id": self.session_id,
            "plan_sha256": self.plan_sha256,
            "epoch": self.epoch,
            "cycle": self.cycle,
            **fields,
        }
        self.events.append(item)
        return item

    def observe(self, *, held=False, anchor=None):
        row = self.ap.observe()
        now = self.clock()
        if (
            row.get("run_id") != self.plan.run_id
            or row.get("phase") != "urban"
            or row.get("position_valid") is not True
            or not self.plan.contains(row.get("position_ned_m"))
            or not vector(row.get("velocity_ned_mps"))
            or not finite(row.get("observed_at_s"))
            or not 0 <= now - row["observed_at_s"] <= self.plan.observation_age_s
            or type(row.get("sequence")) is not int
            or not finite(row.get("battery_fraction"))
            or not self.plan.reserve_fraction <= row["battery_fraction"] <= 1
        ):
            raise LoopRejected("invalid_or_outside_urban_observation")
        previous = self.last_observation
        if previous and (
            row["sequence"] <= previous["sequence"]
            or row["observed_at_s"] <= previous["observed_at_s"]
            or row["observed_at_s"] - previous["observed_at_s"] > self.plan.max_sample_gap_s
        ):
            raise LoopRejected("urban_observation_sequence_or_gap")
        self.last_observation = row
        if held and (
            row.get("ap_mode") != "hold"
            or math.hypot(*row["velocity_ned_mps"]) > self.plan.hold_speed_mps
            or (
                anchor is not None
                and math.dist(row["position_ned_m"], anchor) > self.plan.hold_drift_m
            )
        ):
            raise LoopRejected("urban_hold_not_observed")
        if now - self.started > self.plan.total_timeout_s:
            raise LoopRejected("urban_total_time_budget_exhausted")
        self.record("observation", observation=row)
        return row

    def wait_held(self, point, timeout_s, *, permit=None):
        end, stable = self.clock() + timeout_s, None
        while True:
            row = self.observe()
            if permit is not None:
                start, end_point = permit["start_ned_m"], permit["target_ned_m"]
                delta = [b - a for a, b in zip(start, end_point)]
                length2 = sum(x * x for x in delta)
                fraction = max(
                    0.0,
                    min(
                        1.0,
                        sum((x - a) * d for x, a, d in zip(row["position_ned_m"], start, delta))
                        / length2,
                    ),
                )
                nearest = [a + fraction * d for a, d in zip(start, delta)]
                if (
                    row.get("ap_mode") not in {"hold", "mission"}
                    or math.dist(nearest, row["position_ned_m"]) > self.plan.hold_drift_m
                ):
                    raise LoopRejected("urban_segment_tracking_violated")
            okay = (
                row.get("ap_mode") == "hold"
                and math.hypot(*row["velocity_ned_mps"]) <= self.plan.hold_speed_mps
                and math.dist(row["position_ned_m"], point) <= self.plan.target_error_m
            )
            stable = (row["observed_at_s"] if stable is None else stable) if okay else None
            if stable is not None and row["observed_at_s"] - stable >= self.plan.settle_s:
                return row
            if self.clock() > end:
                raise LoopRejected("urban_target_or_stable_hold_not_observed")
            time.sleep(self.poll_s)

    def monitored(self, operation, timeout_s, anchor):
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="urban-model")
        future = pool.submit(operation)
        end = self.clock() + timeout_s
        try:
            while True:
                self.observe(held=True, anchor=anchor)
                if self.clock() > end:
                    raise LoopRejected("urban_model_timeout")
                if future.done():
                    return future.result()
                time.sleep(self.poll_s)
        finally:
            future.cancel()
            # run() always stops owned model processes, including pending work.
            pool.shutdown(wait=False, cancel_futures=True)

    def request(self, model, observation, *, proposal=None):
        if not self.active or self.pending is not None:
            raise LoopRejected("urban_model_session_not_available")
        if observation["observed_at_s"] <= self.last_arrival_s:
            raise LoopRejected("urban_reobservation_required")
        frame_time, frame_hash = observation.get("image_observed_at_s"), observation.get(
            "image_sha256"
        )
        if (
            not finite(frame_time)
            or not isinstance(frame_hash, str)
            or re.fullmatch(r"[a-f0-9]{64}", frame_hash) is None
            or not 0 <= self.clock() - frame_time <= self.plan.observation_age_s
            or frame_time <= self.last_arrival_s
            or frame_hash in self.input_images
        ):
            raise LoopRejected("urban_fresh_image_required")
        self.input_images.add(frame_hash)
        request = {
            "schema_version": "ship_urban_loop_request.v1",
            "request_id": uuid4().hex,
            "run_id": self.plan.run_id,
            "session_id": self.session_id,
            "plan_sha256": self.plan_sha256,
            "epoch": self.epoch,
            "cycle": self.cycle,
            "model": model,
            "observation": observation,
            "proposal": proposal,
        }
        self.pending = request
        self.record("model_requested", request=request, request_sha256=digest(request))
        response = self.monitored(
            lambda: self.models.infer(request),
            self.plan.inference_timeout_s,
            observation["position_ned_m"],
        )
        self.accept_response(request, response)
        return response

    def accept_response(self, request, response):
        """Late, replayed and cross-session responses never restore authority."""
        if (
            not self.active
            or self.pending != request
            or request.get("epoch") != self.epoch
            or response.get("request_sha256") != digest(request)
            or response.get("session_id") != self.session_id
            or response.get("model") != request["model"]
            or response.get("dispatch_allowed") is not False
        ):
            self.record("response_rejected", request_id=request.get("request_id"))
            raise LoopRejected("urban_stale_or_unbound_response")
        self.pending = None
        self.record("model_received", response=response, response_sha256=digest(response))

    def authorize(self, vla, wam, observation):
        candidates = vla.get("candidates")
        if (
            not isinstance(candidates, list)
            or not 1 <= len(candidates) <= 4
            or any(not isinstance(c, dict) or not isinstance(c.get("id"), str) for c in candidates)
            or len({c["id"] for c in candidates}) != len(candidates)
        ):
            raise LoopRejected("urban_invalid_candidate_set")
        selected = [c for c in candidates if c["id"] == wam.get("selected_candidate_id")]
        if len(selected) != 1 or wam.get("proposal_sha256") != digest(vla):
            raise LoopRejected("urban_wam_not_bound_to_vla_candidates")
        target = selected[0].get("target_ned_m")
        start = observation["position_ned_m"]
        if (
            not self.plan.contains(target)
            or not 0.5 <= math.dist(start, target) <= self.plan.max_leg_m
        ):
            raise LoopRejected("urban_candidate_outside_approved_segment")
        constraint = self.rules.authorize(start, target, observation)
        if (
            constraint.get("allowed") is not True
            or constraint.get("start_ned_m") != start
            or constraint.get("target_ned_m") != target
            or constraint.get("observation_sha256") != digest(observation)
        ):
            raise LoopRejected("urban_independent_rules_rejected")
        permit = {
            "schema_version": "ship_urban_segment_permit.v1",
            "permit_id": uuid4().hex,
            "run_id": self.plan.run_id,
            "session_id": self.session_id,
            "plan_sha256": self.plan_sha256,
            "approval_ref": self.plan.approval_ref,
            "execution_scope": self.plan.execution_scope,
            "epoch": self.epoch,
            "cycle": self.cycle,
            "start_ned_m": start,
            "target_ned_m": target,
            "observation_sha256": digest(observation),
            "vla_response_sha256": digest(vla),
            "wam_response_sha256": digest(wam),
            "rules": constraint,
            "issued_at_s": self.clock(),
            "expires_at_s": self.clock() + self.plan.observation_age_s,
        }
        self.record("segment_authorized", permit=permit)
        return permit

    def run(self):
        if self.used:
            raise LoopRejected("urban_session_is_single_use")
        self.used, self.started = True, self.clock()
        failure, completed, stop_verified = None, 0, False
        try:
            entry = self.observe()
            if math.dist(entry["position_ned_m"], self.plan.entry_ned_m) > self.plan.target_error_m:
                raise LoopRejected("urban_entry_not_observed")
            self.ap.hold()
            entry = self.wait_held(self.plan.entry_ned_m, self.plan.segment_timeout_s)
            self.record("model_start_requested", observation=entry)
            identity = self.monitored(
                self.models.start, self.plan.startup_timeout_s, entry["position_ned_m"]
            )
            if identity.get("execution_scope") != self.plan.execution_scope:
                raise LoopRejected("urban_model_scope_mismatch")
            self.active = True
            self.record("models_ready", identity=identity)
            for cycle in range(1, self.plan.updates + 1):
                self.cycle = cycle
                observation = self.observe(held=True)
                vla = self.request("vla", observation)
                # A separate fresh observation is bound to WAM. The VLA
                # proposal remains a proposal; no movement precedes WAM.
                observation = self.observe(held=True, anchor=observation["position_ned_m"])
                wam = self.request("wam", observation, proposal=vla)
                current = self.observe(held=True, anchor=observation["position_ned_m"])
                permit = self.authorize(vla, wam, current)
                self.observe(held=True, anchor=permit["start_ned_m"])
                if not self.active or self.clock() > permit["expires_at_s"]:
                    raise LoopRejected("urban_permit_expired_before_dispatch")
                ack = self.ap.dispatch(permit)
                if ack != {"accepted": True, "permit_sha256": digest(permit)}:
                    raise LoopRejected("urban_dispatch_not_acknowledged")
                self.record("segment_dispatched", permit_sha256=digest(permit), receipt=ack)
                arrived = self.wait_held(
                    permit["target_ned_m"], self.plan.segment_timeout_s, permit=permit
                )
                self.last_arrival_s = arrived["observed_at_s"]
                completed += 1
                self.record("segment_arrived", permit_sha256=digest(permit), observation=arrived)
            self.ap.hold()
            exit_row = self.observe(held=True)
            self.record("ap_exit_hold_observed", observation=exit_row)
        except Exception as exc:
            failure = f"{type(exc).__name__}:{exc}"
            self.record("failed", reason=failure)
        finally:
            # Revoke first. Stop failure never permits sea return or new work.
            self.active, self.pending = False, None
            self.epoch += 1
            self.record("session_revoked")
            try:
                self.ap.hold()
            except Exception as exc:
                failure = failure or f"urban_abort_hold_failed:{exc}"
                self.record("abort_hold_failed", reason=str(exc))
            try:
                receipt = self.models.stop()
                stop_verified = self.models.stopped() is True
                self.record("models_stopped", verified=stop_verified, receipt=receipt)
                if not stop_verified:
                    failure = failure or "urban_model_shutdown_not_verified"
            except Exception as exc:
                failure = failure or f"urban_model_shutdown_failed:{exc}"
                self.record("model_shutdown_failed", reason=str(exc))
        if failure is None:
            try:
                row = self.observe(held=True)
                receipt = self.ap.return_handoff()
                if receipt.get("ap_return_observed") is not True:
                    raise LoopRejected("urban_ap_return_handoff_not_observed")
                self.record("ap_return_handoff", observation=row, receipt=receipt)
            except Exception as exc:
                failure = f"{type(exc).__name__}:{exc}"
                self.record("failed", reason=failure)
                try:
                    self.ap.hold()
                except Exception as hold_exc:
                    self.record("abort_hold_failed", reason=str(hold_exc))
        return {
            "schema_version": "ship_urban_loop_result.v1",
            "status": "completed" if failure is None else "blocked",
            "plan": asdict(self.plan),
            "plan_sha256": self.plan_sha256,
            "completed_updates": completed,
            "failure": failure,
            "model_shutdown_verified": stop_verified,
            "events": self.events,
            "physical_execution_invoked": False,
            "whole_mission_completion_verified": False,
            "onboard_energy_savings_verified": False,
        }
