"""Bounded mission decisions, independent dispatch checks and later evidence.

This credential-free adapter is called before any legacy anomaly interlock.
Providers live in the Gateway process. A pending decision never stops the
integrator; only the explicit deployment hold changes the sequencer.
"""
from __future__ import annotations

from hashlib import sha256
import json
import math
from pathlib import Path
import time

SCENARIOS = {
    "sixdof_managed_normal": "normal",
    "sixdof_managed_release_fault": "release_fault",
    "sixdof_managed_fuel_shortage": "fuel_shortage",
    "sixdof_managed_tower_unavailable": "tower_unavailable",
    "sixdof_managed_operations_notice": "operations_notice",
    "sixdof_managed_invalid_response": "normal",
}
POINTS = {
    "deployment_start": ["continue", "hold", "stop_deployment", "collect_status"],
    "deployment_monitor": ["continue", "hold", "stop_deployment", "collect_status"],
    "deployment_diagnostic": ["continue", "stop_deployment"],
    "return_selection": ["fixed_return", "retained_return"],
    "booster_selection": ["capture", "divert"],
}
RESPONSE_FAULTS = tuple(f"{kind}_{point}" for kind in ("invalid", "timeout") for point in POINTS)
FIELDS = {"time_s", "phase", "released_count", "release_acknowledged", "sequencer_state",
          "fuel_kg", "return_deadline_s", "tower_ready", "numerical_tools", "operations_notice"}
SCOPE = "local_simulation_and_mission_decision_envelope"
MODE_ENV = "MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE"


def source_hashes(root):
    from .starship_sixdof_catalog import SIXDOF_SOURCES, SUPERVISION_SOURCES
    names = (*SIXDOF_SOURCES, *SUPERVISION_SOURCES,
        "src/runtime/starship_mission_control.py", "scripts/run_starship_mission_worker.py",
        "scripts/run_starship_managed_mission.py", "src/runtime/starship_mission_director.py",
        "src/runtime/starship_mission_director_verifier.py", "src/intelligence/starship_mission_director.py",
        "src/intelligence/starship_mission_planner.py", "src/runtime/starship_return_sites.py",
        "src/gateway/server.py", "src/gateway/starship_chat.py", "src/runtime/assets/starship_operator.html",
        "scripts/start_starship_gateway.py",
        "examples/spaceflight/starship-return-sites-model-test.json")
    return {name: sha256((root/name).read_bytes()).hexdigest() for name in names}


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def fuel_sensor(fuel_kg, time_s, body):
    """Reproducible bounded synthetic gauge noise; independent of policy/fault."""
    seed = sha256(f"gauge-v1:{body}:{int(time_s)}".encode()).digest()
    noise = (int.from_bytes(seed[:2], "big")/65535.-.5)*100.
    return max(0., round((fuel_kg+noise)/100.)*100.)


def contract(mode):
    if mode not in ("fixture", "live"):
        raise ValueError("mission_director_not_configured")
    return {"schema": "missionos.starship_mission_envelope.v2", "mode": mode,
            "scope": SCOPE, "decision_points": json.loads(json.dumps(POINTS)), "maximum_decisions": 5,
            "maximum_jev_calls": 5 if mode == "live" else 0,
            "maximum_llm_calls": 5 if mode == "live" else 0,
            "maximum_observation_requests": 1, "maximum_hold_s": 30.,
            "decision_expiry_s": 75.,
            "minimum_return_fuel_kg": 28000., "return_time_change_allowed": False,
            "return_choices": ["fixed_v1", "mass_state_terminal_v3"],
            "booster_sites": ["capture", "divert"],
            "initial_plan": {"deployment": "continue_subject_to_release_constraints", "return": "fixed_v1", "booster": "divert"},
            "no_response": {"deployment": "preserve_running_plan_subject_to_per_slot_gates_and_persistent_constraints_else_inhibit",
                            "return": "fixed_return", "booster": "divert"},
            "initial_decision_deadline": "before_first_planned_release",
            "out_of_scope": "record_escalation_and_apply_preapproved_fallback",
            "pending_time_policy": "integrate_and_pace_simulation_while_provider_pending",
            "physical_execution_authorized": False}


def validate_observation(row):
    if type(row) is not dict or set(row) != FIELDS:
        raise ValueError("invalid_director_observation")
    for key in ("time_s", "fuel_kg", "return_deadline_s"):
        if type(row[key]) not in (int, float) or not math.isfinite(row[key]) or row[key] < 0:
            raise ValueError("invalid_director_observation")
    if (type(row["released_count"]) is not int or not 0 <= row["released_count"] <= 26
            or type(row["release_acknowledged"]) is not bool or type(row["tower_ready"]) is not bool
            or row["sequencer_state"] not in ("running", "inhibited", "skipped", "held")
            or row["phase"] not in ("orbital_coast", "booster_return")
            or type(row["operations_notice"]) is not str or len(row["operations_notice"]) > 1000):
        raise ValueError("invalid_director_observation")
    tools = row["numerical_tools"]
    if (type(tools) is not dict or set(tools) != {"orbit_release_feasible", "retained_payload_present",
            "capture_corridor_certified", "mechanism_status"}
            or any(type(tools[k]) is not bool for k in tools if k != "mechanism_status")
            or tools["mechanism_status"] not in ("not_collected", "clear", "blocked", "unknown")):
        raise ValueError("invalid_director_tools")
    return json.loads(json.dumps(row, allow_nan=False))


def check_action(envelope, point, action, observation, *, elapsed_s, observation_requests, hold_used_s):
    """No model opinion or text can extend the immutable grant."""
    if envelope != contract(envelope.get("mode")):
        return "envelope_mismatch"
    if type(elapsed_s) not in (int, float) or not math.isfinite(elapsed_s) or elapsed_s < 0:
        return "invalid_decision_clock"
    if point not in POINTS or type(action) is not str or action not in POINTS[point]:
        return "outside_approved_choices"
    if elapsed_s >= envelope["decision_expiry_s"]:
        return "decision_expired"
    if point.startswith("deployment"):
        if observation["time_s"] >= observation["return_deadline_s"]:
            return "return_deadline_reached"
        # Continue activates the queue, never an unconditional physical release.
        # The executor rechecks orbit/pressure/rate at every release slot.
        if action == "continue" and (observation["sequencer_state"] in ("inhibited", "skipped")
                or observation["numerical_tools"]["mechanism_status"] == "blocked"
                or observation["fuel_kg"]-100. < envelope["minimum_return_fuel_kg"]):
            return "release_envelope_not_met"
        if action == "hold" and (hold_used_s >= envelope["maximum_hold_s"]
                or observation["return_deadline_s"]-observation["time_s"] <= envelope["maximum_hold_s"]):
            return "hold_budget_exhausted"
        if action == "collect_status" and observation_requests >= envelope["maximum_observation_requests"]:
            return "observation_budget_exhausted"
    if action == "capture" and (not observation["tower_ready"]
            or not observation["numerical_tools"]["capture_corridor_certified"]):
        return "capture_not_certified"
    return None


def fallback_action(envelope, point, row, *, observation_requests, hold_used_s):
    """A preapproved continuation, not a new model decision or safety certificate.

    Never resurrect a stopped sequence or override an interlock. An unresolved
    notice invalidates routine continuation without interpreting its contents.
    """
    if point == "booster_selection":
        return "divert"
    if point == "return_selection":
        return "fixed_return"
    # A temporary per-slot orbit/pressure/rate gate is not a permanent abort.
    # Keeping the sequence running never bypasses the executor's release gate.
    if (point.startswith("deployment") and row["sequencer_state"] == "running"
            and not row["operations_notice"] and row["time_s"] < row["return_deadline_s"]
            and row["numerical_tools"]["mechanism_status"] != "blocked"
            and row["fuel_kg"]-100. >= envelope["minimum_return_fuel_kg"]):
        return "continue"
    return "stop_deployment"


def publish(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, allow_nan=False)
    temporary.replace(path)


class MissionDirector:
    def __init__(self, envelope, mailbox=None, run_id="standalone", *, fixture_decider=None, response_fault=None):
        if envelope != contract(envelope.get("mode")):
            raise ValueError("invalid_mission_envelope")
        self.envelope = json.loads(json.dumps(envelope))
        self.mailbox = Path(mailbox) if mailbox else None
        if self.mailbox is not None and not self.mailbox.is_dir():
            raise ValueError("director_mailbox_required")
        self.run_id = run_id
        self.fixture_decider = fixture_decider
        if response_fault is not None and (envelope["mode"] != "fixture" or response_fault not in RESPONSE_FAULTS):
            raise ValueError("fixture_response_fault_only")
        self.response_fault = response_fault
        self.records, self.pending, self.finished = [], {}, set()
        self.observation_requests, self.hold_used_s = 0, 0.
        self._pace = {}

    def pace(self, clock_id, time_s):
        pending = self.pending.get(clock_id)
        if pending and self.mailbox:
            simulation_start, wall_start = self._pace[clock_id]
            delay = (time_s-simulation_start)-(time.monotonic()-wall_start)
            if delay > 0:
                time.sleep(min(delay, 2.))

    def update(self, point, row, *, decision_deadline_s=None):
        row = validate_observation(row)
        clock_id = "booster" if point == "booster_selection" else "ship"
        self.confirm(clock_id, row)
        if point in self.finished:
            return None
        request = self.pending.get(clock_id)
        if request is None:
            if len(self.records) >= self.envelope["maximum_decisions"]:
                return None
            token = f"{self.run_id}:{len(self.records)+1}"
            if decision_deadline_s is not None and (type(decision_deadline_s) not in (int, float)
                    or not math.isfinite(decision_deadline_s) or decision_deadline_s <= row["time_s"]):
                raise ValueError("invalid_decision_deadline")
            deadline = min(row["time_s"]+self.envelope["decision_expiry_s"],
                           row["time_s"]+self.envelope["decision_expiry_s"] if decision_deadline_s is None else decision_deadline_s)
            if not math.isfinite(deadline) or deadline <= row["time_s"]:
                raise ValueError("invalid_decision_deadline")
            request = {"schema": "missionos.starship_director_request.v2", "request_id": token,
                       "point": point, "observation": row, "allowed_actions": POINTS[point],
                       "decision_deadline_s": deadline,
                       "envelope_sha256": digest(self.envelope)}
            record = {"request": request, "clock_id": clock_id, "response": None,
                      "dispatch": None, "later_observation": None}
            self.records.append(record)
            self.pending[clock_id] = request
            self._pace[clock_id] = (row["time_s"], time.monotonic())
            if self.mailbox:
                publish(self.mailbox/"request.json", request)
            elif self.fixture_decider:
                record["response"] = self.fixture_decider(request)
        elif request["point"] != point:
            return None
        record = next(item for item in self.records if item["request"]["request_id"] == request["request_id"])
        response = record["response"]
        if response is None and self.mailbox and (self.mailbox/"response.json").is_file():
            try:
                candidate = json.loads((self.mailbox/"response.json").read_text())
                if type(candidate) is dict and candidate.get("request_id") == request["request_id"]:
                    response = candidate
            except (ValueError, OSError):
                pass
        if response is not None and type(response) is not dict:
            response = {"request_id": request["request_id"], "action": None,
                        "mode": "malformed_response", "model_inference_invoked": False}
        if self.response_fault == "timeout_"+point:
            response = None
        elif response is not None and self.response_fault == "invalid_"+point:
            response = {**response, "action": None, "synthetic_response_fault": "invalid_action"}
        elapsed = row["time_s"]-request["observation"]["time_s"]
        if response is None and row["time_s"] < request["decision_deadline_s"]:
            return None
        fallback = fallback_action(self.envelope, point, row,
            observation_requests=self.observation_requests, hold_used_s=self.hold_used_s)
        response = response or {"request_id": request["request_id"], "action": fallback,
                                "mode": "timeout_fallback", "model_inference_invoked": False}
        # Bind the exact observation and envelope, not just an action string.
        action = response.get("action")
        reason = check_action(self.envelope, point, action, row, elapsed_s=elapsed,
                              observation_requests=self.observation_requests, hold_used_s=self.hold_used_s)
        if row["time_s"] >= request["decision_deadline_s"]:
            reason = reason or "decision_deadline_reached"
        if (response.get("request_id") != request["request_id"]
                or response.get("request_sha256") != digest(request)
                or response.get("mode") != self.envelope["mode"]):
            reason = reason or "response_binding_mismatch"
        applied = fallback if reason else action
        if applied == "collect_status":
            self.observation_requests += 1
        if applied == "hold":
            self.hold_used_s += self.envelope["maximum_hold_s"]
        record.update(response=response, dispatch={"action": applied, "time_s": row["time_s"],
                      "rules_accepted": reason is None, "rejection": reason, "observation": row,
                      "escalation_requested": reason == "outside_approved_choices"})
        self.finished.add(point)
        del self.pending[clock_id]
        return applied

    def confirm(self, clock_id, row):
        for record in self.records:
            dispatch = record["dispatch"]
            if (record["clock_id"] == clock_id and dispatch and record["later_observation"] is None
                    and row["time_s"] > dispatch["time_s"]):
                record["later_observation"] = validate_observation(row)

    def finish(self):
        return {"schema": "missionos.starship_director_record.v1", "envelope": self.envelope,
                "response_fault": self.response_fault,
                "records": self.records, "observation_requests": self.observation_requests,
                "hold_used_s": self.hold_used_s, "human_inflight_commands": 0,
                "human_workload_measured": False, "model_value_demonstrated": False}
