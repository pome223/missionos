"""Explicit CPU/process/HTTP smoke of the urban loop, without a flight simulator."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import math
from pathlib import Path
import sys
import time
from uuid import uuid4

from .ship_urban_loop import LoopRejected, UrbanLoopPlan, UrbanLoopRuntime, digest
from .ship_urban_goal_contract import context_digest, segment_clearance, validate_goal_observation
from .ship_urban_model_processes import ManagedUrbanModels

FAULTS = (
    "none",
    "offshore_entry",
    "wrong_phase",
    "stale_observation",
    "reused_image",
    "cross_cycle",
    "unsafe_candidate",
    "rules_rejection",
    "http_failure",
    "timeout",
    "hold_loss",
    "shutdown_failure",
    "arrival_failure",
    "tracking_deviation",
    "offshore_drift",
    "reserve_exhausted",
)


class FixtureAutopilot:
    def __init__(self, plan, fault="none"):
        self.plan, self.fault = plan, fault
        self.point = list(plan.entry_ned_m)
        if fault == "offshore_entry":
            self.point[0] = plan.coast_north_m - 1
        self.target, self.sequence, self.mode = None, 0, "hold"
        self.dispatched, self.permits = [], set()
        self.model_ready = False
        self.inference_started = False
        self.returned = False
        self.obstacles = []

    def goal_context(self, observed_at_s):
        return {
            "schema_version": "ship_urban_observed_geometry.v1",
            "observed_at_s": observed_at_s,
            "coverage": {
                "lower_ned_m": list(self.plan.lower_ned_m),
                "upper_ned_m": list(self.plan.upper_ned_m),
            },
            "obstacles": deepcopy(self.obstacles),
        }

    def observe(self):
        self.sequence += 1
        moving = self.target is not None
        if moving and self.fault != "arrival_failure":
            distance = math.dist(self.point, self.target)
            step = min(2.0, distance)
            self.point = [a + step / distance * (b - a) for a, b in zip(self.point, self.target)]
            if step == distance:
                self.point, self.target, self.mode, moving = (
                    self.target,
                    None,
                    "hold",
                    False,
                )
        if self.fault == "tracking_deviation" and moving:
            self.point[1] += 4
        stamp = time.monotonic()
        if self.fault == "offshore_drift" and self.inference_started:
            self.point[0] = self.plan.coast_north_m - 1
        observation = {
            "run_id": self.plan.run_id,
            "sequence": self.sequence,
            "observed_at_s": stamp - (5 if self.fault == "stale_observation" else 0),
            "image_observed_at_s": stamp,
            "image_sha256": digest(
                {"fixture_frame": 0 if self.fault == "reused_image" else self.sequence}
            ),
            "position_valid": True,
            "position_ned_m": list(self.point),
            "velocity_ned_mps": [1.0 if moving else 0.0, 0.0, 0.0],
            "phase": "sea" if self.fault == "wrong_phase" else "urban",
            "ap_mode": ("manual" if self.fault == "hold_loss" and self.model_ready else self.mode),
            "battery_fraction": (
                0.1 if self.fault == "reserve_exhausted" and self.inference_started else 0.8
            ),
        }
        if self.plan.fixture_goal_policy:
            observation["goal_context"] = self.goal_context(observation["observed_at_s"])
            observation["image_sha256"] = digest({
                "fixture_frame": 0 if self.fault == "reused_image" else self.sequence,
                "position_ned_m": self.point,
                "context_sha256": context_digest(observation),
            })
        return observation

    def hold(self):
        self.target, self.mode = None, "hold"

    def dispatch(self, permit):
        if (
            permit["execution_scope"] != "fixture"
            or permit["permit_id"] in self.permits
            or time.monotonic() > permit["expires_at_s"]
            or permit["plan_sha256"] != digest(self.plan_material())
            or math.dist(self.point, permit["start_ned_m"]) > self.plan.hold_drift_m
        ):
            raise LoopRejected("fixture_ap_permit_rejected")
        if self.plan.fixture_goal_policy:
            context = self.goal_context(time.monotonic())
            if (
                permit["rules"].get("observation_context_sha256") != context_digest(context)
                or not segment_clearance(
                    self.point, permit["target_ned_m"], context, self.plan.clearance_m
                )["allowed"]
            ):
                raise LoopRejected("fixture_goal_context_changed_before_dispatch")
        self.permits.add(permit["permit_id"])
        self.dispatched.append(permit)
        self.target, self.mode = list(permit["target_ned_m"]), "mission"
        return {"accepted": True, "permit_sha256": digest(permit)}

    def plan_material(self):
        from dataclasses import asdict

        return asdict(self.plan)

    def return_handoff(self):
        self.returned = True
        self.mode = "return"
        return {"ap_return_observed": True, "execution_scope": "fixture"}


class FixtureRules:
    def __init__(self, fault="none", *, plan=None):
        self.fault, self.plan = fault, plan

    def authorize(self, start, target, observation):
        constraint = {
            "allowed": self.fault != "rules_rejection",
            "start_ned_m": start,
            "target_ned_m": target,
            "observation_sha256": digest(observation),
            "constraint_source": "explicit_empty_fixture_corridor",
        }
        if self.plan is not None and self.plan.fixture_goal_policy:
            context = validate_goal_observation(
                observation, now_s=time.monotonic(), max_age_s=self.plan.observation_age_s,
                lower_ned_m=self.plan.lower_ned_m, upper_ned_m=self.plan.upper_ned_m,
            )
            clearance = segment_clearance(start, target, context, self.plan.clearance_m)
            constraint.update(
                allowed=clearance["allowed"] and self.fault != "rules_rejection",
                constraint_source="observed_fixture_boxes",
                observation_context_sha256=context_digest(context),
                blocking_obstacle_ids=clearance["blocking_obstacle_ids"],
            )
        return constraint


def run_fixture(
    output_dir, *, approved=False, fault="none", goal_responsive=False, obstacle_side="none"
):
    if approved is not True:
        raise PermissionError("--approve-fixture is required; no model workers started")
    if fault not in FAULTS:
        raise ValueError("unknown urban loop fixture fault")
    if type(goal_responsive) is not bool or obstacle_side not in {"none", "left", "right"}:
        raise ValueError("invalid goal fixture scenario")
    if obstacle_side != "none" and not goal_responsive:
        raise ValueError("observed obstacle scenarios require --goal-responsive")
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=False)
    # Short wall-time bounds only for these CPU doubles, never native defaults.
    plan = replace(
        UrbanLoopPlan(uuid4().hex, "operator:explicit-fixture-opt-in", "fixture"),
        settle_s=0.05,
        inference_timeout_s=0.3,
        segment_timeout_s=1.0,
        startup_timeout_s=8.0,
        total_timeout_s=20.0,
    )
    if goal_responsive:
        plan = replace(
            plan, goal_ned_m=(1027.0, 0.0, -30.0), fixture_goal_policy=True,
            updates=3, max_leg_m=5.0, clearance_m=0.25,
        )
    ap = FixtureAutopilot(plan, fault)

    def command(model):
        return lambda port, nonce: [
            sys.executable,
            "-m",
            "scripts.ship_urban_loop_fixture_worker",
            "--model",
            model,
            "--port",
            str(port),
            "--nonce",
            nonce,
            "--fault",
            fault,
            "--policy",
            "goal-responsive" if goal_responsive else "legacy",
        ]

    class FixtureProcesses(ManagedUrbanModels):
        def start(self):
            result = super().start()
            ap.model_ready = True
            return result

        def infer(self, request):
            ap.inference_started = True
            return super().infer(request)

        def stopped(self):
            actual = super().stopped()
            return actual and fault != "shutdown_failure"

    models = FixtureProcesses(
        {model: command(model) for model in ("vla", "wam")},
        root / "workers",
        execution_scope="fixture",
        timeout_s=8,
    )
    class FixtureRuntime(UrbanLoopRuntime):
        def record(self, event, **fields):
            item = super().record(event, **fields)
            # Change the fixture world only after the common first arrival.
            # The worker receives the next observation, never this schedule or side.
            if (
                goal_responsive and obstacle_side != "none"
                and event == "segment_arrived" and len(ap.dispatched) == 1
            ):
                ap.obstacles = [{
                    "id": "observed-obstacle",
                    "center_ned_m": [1021.0, -0.25 if obstacle_side == "left" else 0.25, -30.0],
                    "half_size_m": [0.15, 0.30, 0.5],
                }]
                super().record("fixture_world_changed", obstacles=deepcopy(ap.obstacles))
            return item

    runtime = FixtureRuntime(plan, ap, models, FixtureRules(fault, plan=plan), poll_s=0.01)
    result = runtime.run()
    # Deliberately replay a real earlier response after session revocation.
    requests = [e["request"] for e in runtime.events if e["event"] == "model_requested"]
    responses = [e["response"] for e in runtime.events if e["event"] == "model_received"]
    late_rejected = None
    if requests and responses:
        try:
            runtime.accept_response(requests[0], responses[0])
        except LoopRejected:
            late_rejected = True
        else:
            late_rejected = False
    result.update(
        execution_backend="fixture_http_processes",
        fault=fault,
        late_response_rejected=late_rejected,
        fixture_ap_return_handoff=ap.returned,
        owned_processes_reaped=ManagedUrbanModels.stopped(models),
        vla_invoked=False,
        wam_invoked=False,
        px4_runtime_invoked=False,
        gazebo_runtime_invoked=False,
        native_model_adapter_connected=False,
    )
    if goal_responsive:
        result.update(goal_responsive=True, observed_obstacle_side=obstacle_side)
    (root / "result.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return result
