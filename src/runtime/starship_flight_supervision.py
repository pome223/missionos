"""Bounded simulator command mailbox; no model, network, or flight authority.

The injected no-effect release is a local test fault. The sole executable
supervisory command cancels the remaining deployment sequence. An accepted
command is not an observed result; later simulator telemetry records its effect.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import time

MAX_DECISION_AGE_S = 75.0
OBSERVATION_INTERVAL_S = 2.0
MAX_RESPONSE_BYTES = 32_768
ACTIONS = ("hold", "skip_remaining_deployment")
ROUTES = ("bounded", "need_observation", "human_review", "deep_reasoning")
RESPONSE_FIELDS = {"request_id", "observation_id", "action", "route", "jev_invocation", "llm_invocation"}


def _reject_constant(value):
    raise ValueError(f"nonfinite JSON number: {value}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


class FlightSupervision:
    """Observe live state, accept at most one bounded reply, and retain facts.

    ``step`` returns a command to the simulator executor, never changes its
    state. ``pace`` ties only the pending decision interval to wall time; the
    caller continues the same physical integrator at every simulation step.
    A missing mailbox is an explicitly non-commanding local hold.
    """

    def __init__(self, mailbox: Path | None, request_id: str, *, collect_observation=False):
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
            raise ValueError("invalid supervision request ID")
        if type(collect_observation) is not bool:
            raise ValueError("invalid observation collection scope")
        self.collect_observation = collect_observation
        self.mailbox = Path(mailbox) if mailbox is not None else None
        if self.mailbox is not None:
            self.mailbox.mkdir(parents=True, exist_ok=True)
            if any((self.mailbox / name).exists() for name in ("request.json", "response.json", "request.json.tmp",
                                                            "request-followup.json", "response-followup.json", "request-followup.json.tmp")):
                raise FileExistsError("supervision mailbox already contains an exchange")
        self.log = {"schema": "missionos.starship_flight_supervision.v1", "request_id": request_id,
                    "request": None, "response": None, "rules": [], "commands": [], "observations": [],
                    "status": "not_triggered", "max_decision_age_s": MAX_DECISION_AGE_S,
                    "observation_interval_s": OBSERVATION_INTERVAL_S,
                    "effect_observation_interval_s": None, "physical_execution": False}
        if collect_observation:
            self.log.update(observation_collection_allowed=True, collection=None,
                            followup_request=None, followup_response=None)
        self._next_observation_s = 0.0
        self._pending_wall_s = None
        self._post_observations = 0

    def pace(self, simulation_time_s: float) -> None:
        """Advance one simulated second per wall second only while pending."""
        if self.log["status"] not in ("pending", "collecting"):
            return
        elapsed_simulation = simulation_time_s - self.log["request"]["observations"][-1]["time_s"]
        remaining = elapsed_simulation - (time.monotonic() - self._pending_wall_s)
        while remaining > 0:
            time.sleep(min(remaining, 1.0))
            remaining = elapsed_simulation - (time.monotonic() - self._pending_wall_s)

    def step(self, telemetry: dict, *, return_reserve_kg: float, payload_interval_s: float,
             orbit_bound: bool = True) -> tuple[dict | None, dict | None]:
        """Return (new observation, optional skip command) from actual state."""
        status = self.log["status"]
        if status not in ("not_triggered", "observing", "pending", "collecting", "command_accepted"):
            return None, None
        if telemetry["phase"] != "orbital_coast":
            if status in ("observing", "pending", "collecting"):
                self._reject(telemetry, "phase_ended", "expired")
            return None, None
        now = telemetry["time_s"]
        if now + 1e-9 < self._next_observation_s:
            return None, None
        if not math.isfinite(payload_interval_s) or payload_interval_s < OBSERVATION_INTERVAL_S:
            raise ValueError("payload observation interval must be at least two seconds")
        sequence = len(self.log["observations"]) + 1
        observation = {"observation_id": f"{self.log['request_id']}-obs-{sequence:04d}", "sequence": sequence, **telemetry}
        self.log["observations"].append(observation)
        self.log["effect_observation_interval_s"] = payload_interval_s
        self._next_observation_s = now + OBSERVATION_INTERVAL_S
        if status == "command_accepted":
            self._post_observations += 1
            self._next_observation_s = now + payload_interval_s
            if self._post_observations >= 2:
                # This is the simulator's recorded state, not an independent
                # verification or a declaration of successful mission recovery.
                self.log["status"] = "effect_observations_recorded"
            return observation, None
        if self.log["request"] is None:
            self.log["status"] = "observing"
            if sequence < 2:
                return observation, None
            request = {"schema": "missionos.starship_flight_supervision_request.v1",
                       "request_id": self.log["request_id"], "observations": self.log["observations"][:2],
                       "allowed_actions": list(ACTIONS)}
            self.log["request"] = request
            if self.mailbox is None:
                self._reject(observation, "no_broker", "no_broker_hold")
                return observation, None
            raw = json.dumps(request, allow_nan=False, separators=(",", ":"))
            temporary = self.mailbox / "request.json.tmp"
            temporary.write_text(raw, encoding="utf-8")
            temporary.replace(self.mailbox / "request.json")
            self._pending_wall_s = time.monotonic()
            self.log["status"] = "pending"
            # Even an instantaneous broker reply must be checked against a
            # later integrated state, never the request-creation observation.
            return observation, None
        request_observation = self.log["request"]["observations"][-1]
        age = now - request_observation["time_s"]
        if age > MAX_DECISION_AGE_S or now >= observation["return_deadline_s"]:
            self._reject(observation, "decision_expired" if age > MAX_DECISION_AGE_S else "return_deadline", "expired")
            return observation, None
        if status == "collecting":
            operation = self.log["collection"]
            operation.update(status="report_received", completed_time_s=now,
                             received_observation_id=observation["observation_id"])
            previous = self.log["observations"][-2]
            request = {"schema": "missionos.starship_flight_supervision_request.v1",
                       "request_id": self.log["request_id"], "observations": [previous, observation],
                       "allowed_actions": list(ACTIONS)}
            self.log["followup_request"] = request
            temporary = self.mailbox / "request-followup.json.tmp"
            temporary.write_text(json.dumps(request, allow_nan=False, separators=(",", ":")), encoding="utf-8")
            temporary.replace(self.mailbox / "request-followup.json")
            self.log["status"] = "pending"
            return observation, None
        following = self.log.get("followup_request")
        response_path = self.mailbox / ("response-followup.json" if following else "response.json")
        if not response_path.exists():
            return observation, None
        try:
            if response_path.is_symlink() or response_path.stat().st_size > MAX_RESPONSE_BYTES:
                raise ValueError("response file is not bounded")
            raw = response_path.read_bytes()
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("response file is not bounded")
            response = json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
            json.dumps(response, allow_nan=False)
            if (not isinstance(response, dict) or set(response) != RESPONSE_FIELDS
                    or not isinstance(response.get("jev_invocation"), dict)
                    or not isinstance(response.get("llm_invocation"), dict)):
                raise ValueError("invalid response object")
        except (OSError, ValueError, UnicodeError, RecursionError):
            self._reject(observation, "malformed_response", "rejected")
            return observation, None
        self.log["followup_response" if following else "response"] = response
        if following:
            request_observation = following["observations"][-1]
        reasons = []
        if response["request_id"] != self.log["request_id"]:
            reasons.append("request_id_mismatch")
        if response["observation_id"] != request_observation["observation_id"]:
            reasons.append("observation_id_mismatch")
        if response["action"] not in ACTIONS:
            reasons.append("action_not_allowed")
        if response["route"] not in ROUTES:
            reasons.append("route_invalid")
        if (not following and self.collect_observation and response["route"] == "need_observation"
                and response["action"] == "hold" and not reasons):
            if (observation["sequencer_state"] != "inhibited" or observation["payload_released_count"] != 0
                    or observation["release_attempt_count"] != 1 or observation["release_acknowledged"] is not True
                    or orbit_bound is not True or observation["propellant_kg"] < return_reserve_kg):
                self._reject(observation, "collection_precondition_failed", "rejected")
                return observation, None
            self.log["collection"] = {"collection_id": self.log["request_id"]+"-status-1",
                "kind": "deployment_status", "issued_time_s": now, "request_observation_id": observation["observation_id"],
                "accepted": True, "status": "requested", "received_observation_id": None, "completed_time_s": None,
                "actuator_operation": False, "physical_execution": False}
            self.log["status"] = "collecting"
            return observation, None
        if response["action"] == "hold":
            reasons.append("hold_requested")
        elif response["route"] not in ("bounded", "deep_reasoning"):
            reasons.append("route_requires_hold")
        evidence = (following or self.log["request"])["observations"]
        if (len(evidence) != 2 or evidence[1]["time_s"] - evidence[0]["time_s"] < OBSERVATION_INTERVAL_S - 1e-8
                or any(not x["release_acknowledged"] or x["release_attempt_count"] != 1
                       or x["sequencer_state"] != "inhibited" or x["payload_released_count"] != 0 for x in evidence)):
            reasons.append("missing_no_effect_evidence")
        if (observation["sequencer_state"] != "inhibited" or observation["payload_released_count"] != 0
                or observation["release_attempt_count"] != 1 or observation["release_acknowledged"] is not True):
            reasons.append("current_state_changed")
        if orbit_bound is not True:
            reasons.append("orbit_not_bound")
        if observation["propellant_kg"] < return_reserve_kg:
            reasons.append("return_reserve")
        if self.log["commands"]:
            reasons.append("command_already_used")
        rule = {"time_s": now, "observation_id": observation["observation_id"], "action": response["action"],
                "allowed": not reasons, "reasons": reasons}
        self.log["rules"].append(rule)
        if reasons:
            self.log["status"] = "held" if reasons == ["hold_requested"] else "rejected"
            return observation, None
        command = {"command_id": f"{self.log['request_id']}-skip-1", "request_id": self.log["request_id"],
                   "action": "skip_remaining_deployment", "time_s": now,
                   "observation_id": observation["observation_id"], "accepted": True,
                   "sequencer_state_before": "inhibited", "sequencer_state_after": "skipped",
                   "payload_released_count": observation["payload_released_count"]}
        self.log["commands"].append(command)
        self.log["status"] = "command_accepted"
        self._next_observation_s = now + payload_interval_s
        return observation, command

    def _reject(self, observation: dict, reason: str, status: str) -> None:
        self.log["rules"].append({"time_s": observation["time_s"],
                                 "observation_id": observation.get("observation_id"),
                                 "action": "hold", "allowed": False, "reasons": [reason]})
        self.log["status"] = status

    def finish(self, *, sequencer_state: str | None, simulation_time_s: float) -> dict:
        if self.log["status"] in ("observing", "pending", "collecting", "command_accepted"):
            self.log["status"] = "mission_ended_before_evidence"
        self.log["final_sequencer_state"] = sequencer_state
        self.log["final_time_s"] = simulation_time_s
        return self.log
