"""Bounded ascent monitoring; engine, attitude and terminal control stay in GNC.

Only synthetic observed channels reach the provider. A conservative decision
can latch payload withholding, which the concurrent plant enforces at release.
This does not add an autonomous ascent abort trajectory or model rocket safety.
"""

from copy import deepcopy
import json
from pathlib import Path
import time

from .starship_replanning import digest, estimate
from .starship_mission_director import publish

CONFIG = {
    "schema": "missionos.starship_ascent_authority.v1",
    "actions": ["continue_mission", "withhold_deployment"],
    "maximum_requests": 4,
    "decision_timeout_s": 12.0,
    "sample_interval_s": 1.0,
    "provider": "jev_only",
    "fallback": "withhold_on_observed_engine_loss_else_continue",
    "no_response_before_orbit": "resolve_before_any_release",
    "low_level_control": "unchanged_gnc",
    "physical_execution": False,
}


class AscentSupervisor:
    def __init__(
        self, authority, *, mode, run_id, mailbox=None, clock=time.monotonic, sleep=time.sleep
    ):
        if (
            authority != CONFIG
            or mode not in ("fixture", "live")
            or (mode == "live" and mailbox is None)
        ):
            raise ValueError("ascent_authority_or_provider_missing")
        self.authority = deepcopy(authority)
        self.mode = mode
        self.run_id = run_id
        self.mailbox = Path(mailbox) if mailbox is not None else None
        self.clock = clock
        self.sleep = sleep
        self.pending = None
        self.deferred_triggers = set()
        self.records = []
        self.observations = []
        self.events = []
        self.previous = None
        self.last_observed = -1e9
        self.request_count = 0
        self.withheld = False
        self.last_command_time = None
        self.confirmations = []

    def deployment_permission(self):
        return not self.withheld

    def _decide(self, request, observation):
        if self.mode == "fixture":
            action = (
                "continue_mission"
                if all(observation["engine_available"])
                else "withhold_deployment"
            )
            return {
                "request_id": request["request_id"],
                "request_sha256": digest(request),
                "action": action,
                "model_inference_invoked": False,
                "mode": "fixture",
            }
        try:
            response = json.loads((self.mailbox / "response.json").read_text())
        except (OSError, ValueError):
            return None
        if type(response) is not dict:
            return None
        if response.get("request_id") != request["request_id"]:
            return None
        return response

    @staticmethod
    def _live_response_valid(response):
        receipt = response.get("jev_invocation")
        return (response.get("mode") == "live" and response.get("model_inference_invoked") is True
                and response.get("llm_invocation") is None and type(receipt) is dict
                and receipt.get("call_attempted") is True
                and receipt.get("model_inference_invoked") is True
                and receipt.get("provider") == "typesafe" and receipt.get("status") == "succeeded")

    def observe(self, tick):
        if tick.body_id != "ship":
            return
        # Private plant state is only an input to this explicitly declared sensor.
        nav = estimate(tick.state)
        observed = {
            "time_s": tick.time_s,
            "phase": tick.phase,
            "navigation": nav,
            "engine_available": [bool(s.available) for s in tick.state.engine_states],
            "deployment_withheld": getattr(tick, "deployment_withheld", False),
            "sequencer_state": getattr(tick, "sequencer_state", None),
            "released_count": getattr(tick, "released_count", 0),
        }
        phase_changed = self.previous is not None and observed["phase"] != self.previous["phase"]
        health_changed = (
            self.previous is not None
            and observed["engine_available"] != self.previous["engine_available"]
        )
        # Coalesce event kinds while an earlier request is pending. The next
        # request uses a fresh observation; an event must not disappear merely
        # because its earlier model call resolves on a later physics tick.
        if self.previous is None:
            self.deferred_triggers.add("initial_monitor")
        if phase_changed:
            self.deferred_triggers.add("phase_changed")
        if health_changed:
            self.deferred_triggers.add("health_changed")
        if self.previous is not None and observed["time_s"] < self.previous["time_s"]:
            raise ValueError("ascent_clock_reversed")
        if (
            tick.time_s - self.last_observed >= self.authority["sample_interval_s"] - 1e-7
            or phase_changed
            or health_changed
        ):
            self.observations.append(deepcopy(observed))
            self.last_observed = tick.time_s
        self.previous = observed
        if self.mode == "live":
            for entry in self.records:
                if entry["later_observation"] is None and tick.time_s > entry["time_s"]:
                    entry["later_observation"] = deepcopy(observed)
        if not all(observed["engine_available"]) and not self.withheld:
            self.withheld = True
            self.last_command_time = tick.time_s
            self.events.append({"time_s": tick.time_s, "trigger": "observed_engine_loss_interlock",
                                "model_request": False, "action": "withhold_deployment"})
        if (
            self.last_command_time is not None
            and observed["time_s"] > self.last_command_time
            and observed["deployment_withheld"]
        ):
            if not self.confirmations:
                self.confirmations.append(
                    {
                        "command_time_s": self.last_command_time,
                        "later_observation": deepcopy(observed),
                    }
                )
        if self.pending:
            pending = self.pending
            request = pending["request"]
            # A pending AI response cannot freeze the simulated time or allow one
            # body to leap ahead. The common coordinator invokes this before GNC.
            elapsed = tick.time_s - request["observation"]["time_s"]
            while self.clock() - pending["wall_started"] < elapsed:
                self.sleep(min(0.01, elapsed - (self.clock() - pending["wall_started"])))
            response = self._decide(request, observed)
            later = tick.time_s > request["observation"]["time_s"]
            expired = tick.time_s >= request["deadline_s"] or tick.phase == "orbital_coast"
            if later and (response is not None or expired):
                valid = (
                    type(response) is dict
                    and response.get("request_sha256") == digest(request)
                    and response.get("request_id") == request["request_id"]
                    and response.get("action") in self.authority["actions"]
                    and tick.time_s < request["deadline_s"]
                    and (self.mode == "fixture" or self._live_response_valid(response))
                )
                # A new observed engine loss invalidates an earlier continue.
                same_stage = observed["phase"] == request["observation"]["phase"]
                health_stale = (same_stage and observed["engine_available"] != request["observation"]["engine_available"])
                # A conservative hold remains valid across separation; an old
                # continue cannot override current health or an earlier latch.
                unsafe_continue = (response or {}).get("action") == "continue_mission" and (
                    health_stale or not all(observed["engine_available"]))
                valid = valid and not unsafe_continue
                action = (
                    response["action"]
                    if valid
                    else (
                        "withhold_deployment"
                        if not all(observed["engine_available"])
                        else "continue_mission"
                    )
                )
                self.withheld = self.withheld or action == "withhold_deployment"
                if action == "withhold_deployment" and self.last_command_time is None:
                    self.last_command_time = tick.time_s
                self.records.append(
                    {
                        "request": request,
                        **({"later_observation": None} if self.mode == "live" else {}),
                        "response": response,
                        "accepted_action": response["action"] if valid else None,
                        "action": action,
                        "fallback": not valid,
                        "time_s": tick.time_s,
                        "observation": deepcopy(observed),
                        "stale_health": health_stale,
                        "physical_engine_command": False,
                    }
                )
                if self.mailbox:
                    publish(self.mailbox / "cancelled.json", {"request_id": request["request_id"],
                        "request_sha256": digest(request), "reason": "actor_resolved",
                        "observed_time_s": tick.time_s})
                self.pending = None
        if tick.phase not in ("stack_ascent", "ship_ascent"):
            return
        trigger = next((kind for kind in ("health_changed", "phase_changed", "initial_monitor")
                        if kind in self.deferred_triggers), None)
        if (
            trigger
            and self.pending is None
            and self.request_count < self.authority["maximum_requests"]
        ):
            # This observation covers all event kinds coalesced since the
            # previous request, so do not submit duplicate requests afterward.
            self.deferred_triggers.clear()
            request = {
                "schema": "missionos.starship_ascent_decision.v1",
                "request_id": f"{self.run_id}-ascent-{self.request_count}",
                "authority_sha256": digest(self.authority),
                "observation": deepcopy(observed),
                "trigger": trigger,
                "deadline_s": tick.time_s + self.authority["decision_timeout_s"],
                "allowed_actions": deepcopy(self.authority["actions"]),
            }
            self.events.append(
                {"time_s": tick.time_s, "trigger": trigger, "request_id": request["request_id"]}
            )
            self.request_count += 1
            self.pending = {"request": request, "wall_started": self.clock()}
            if self.mailbox:
                publish(self.mailbox / "request.json", request)

    def finish(self):
        return {
            "schema": "missionos.starship_ascent_supervision.v1",
            "authority": self.authority,
            "mode": self.mode,
            "observations": self.observations,
            "trigger_events": self.events,
            "decisions": self.records,
            "requests": self.request_count,
            "pending": self.pending["request"] if self.pending else None,
            "withheld": self.withheld,
            "confirmations": self.confirmations,
            "physical_execution": False,
            "abort_trajectory_implemented": False,
        }
