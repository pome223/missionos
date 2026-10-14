"""Experimental mission-level fault response; no nominal return certificate.

The manifest stays in the plant. Only current synthetic diagnostic observations
cross the provider boundary. This actor cannot command engines, attitude, catch,
or a new return trajectory. A continued baseline is explicitly unqualified.
"""
from copy import deepcopy
from hashlib import sha256
import json
import math
import re
from pathlib import Path
import time

from .starship_mission_director import publish

SCHEMA = "missionos.starship_trouble_control_authority.v1"
ACTIONS = ("continue_mission", "collect_status", "withhold_deployment", "inhibit_return")
OBS_KEYS = {"time_s", "body", "phase", "altitude_m", "speed_mps", "body_rate_rad_s",
    "fuel_kg", "component_health", "released_count", "sequencer_state", "deployment_withheld",
    "return_inhibited", "return_deadline_s"}
HEALTH_KEYS = {"observed_at_s", "channel", "engine_health", "actuator_health", "payload_mechanism", "tower_ready"}


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def authority(profile, manifest, source_sha256, duration_s, mode, *, provider_models=None):
    from .starship_trouble_engine import manifest_digest, validate_manifest
    if mode not in ("fixture", "live") or type(duration_s) not in (int, float) or not 0 < duration_s <= profile["integration"]["max_duration_s"]:
        raise ValueError("invalid_trouble_control_authority")
    validate_manifest(manifest)
    models = deepcopy(provider_models) if provider_models is not None else {"jev": None, "deepseek": None}
    if (type(models) is not dict or set(models) != {"jev", "deepseek"}
            or mode == "fixture" and models != {"jev": None, "deepseek": None}
            or mode == "live" and (models["jev"] != "jev-1.13.0" or type(models["deepseek"]) is not str
                or re.fullmatch(r"deepseek-[A-Za-z0-9_.-]{1,80}", models["deepseek"]) is None)):
        raise ValueError("invalid_trouble_provider_models")
    return {"schema": SCHEMA, "mode": mode, "profile_sha256": digest(profile),
        "manifest_sha256": manifest_digest(manifest), "source_sha256": deepcopy(source_sha256),
        "provider_models": models,
        "duration_s": duration_s, "actions": list(ACTIONS), "diagnostic_delay_s": 2.,
        "diagnostic_terminal_phases": ["landing_burn", "booster_landing_burn"],
        "diagnostic_body_scope": "payload_ship_only_v1", "recent_action_limit": 4,
        "short_decision_s": 12., "orbital_decision_s": 150., "periodic_monitor_s": 120.,
        "deepseek_reasoning_effort": "low", "deepseek_maximum_output_tokens": 16384,
        "deepseek_provider_timeout_s": 120.,
        "maximum_llm_calls": None, "maximum_wall_time_s": 2400.,
        "maximum_output_bytes": 500_000_000, "minimum_free_bytes": 5*1024**3,
        "fallback": "withhold_ship_on_observed_fault_else_continue",
        "return_policy": "fixed_v1_unqualified_experimental_baseline",
        "fault_return_qualified": False, "physical_execution_authorized": False,
        "human_approval_authenticated": False, "attempts": 1, "automatic_retry": False}


def validate_observation(row):
    if type(row) is not dict or set(row) != OBS_KEYS:
        raise ValueError("trouble_observation_fields")
    if row["body"] not in ("ship", "booster") or row["phase"] not in (
            "stack_ascent", "ship_ascent", "orbital_coast", "deorbit_slew", "deorbit_burn",
            "ballistic_return", "landing_burn", "booster_boostback_slew", "booster_boostback",
            "booster_entry_coast", "booster_landing_burn"):
        raise ValueError("trouble_observation_phase")
    for key in ("time_s", "altitude_m", "speed_mps", "body_rate_rad_s", "fuel_kg"):
        value = row[key]
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e9:
            raise ValueError("trouble_observation_number")
    deadline = row["return_deadline_s"]
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline < 0):
        raise ValueError("trouble_observation_deadline")
    if (type(row["released_count"]) is not int or not 0 <= row["released_count"] <= 26
            or any(type(row[k]) is not bool for k in ("deployment_withheld", "return_inhibited"))
            or row["sequencer_state"] not in (None, "running", "held", "inhibited", "skipped")):
        raise ValueError("trouble_observation_sequence")
    health = row["component_health"]
    if (type(health) is not dict or set(health) != HEALTH_KEYS or health["channel"] != "synthetic_component_health_v1"
            or health["observed_at_s"] != row["time_s"] or health["payload_mechanism"] not in ("clear", "blocked")
            or type(health["tower_ready"]) is not bool):
        raise ValueError("trouble_health_fields")
    for field, flag in (("engine_health", "available"), ("actuator_health", "jammed")):
        entries = health[field]
        if type(entries) is not list or not 0 < len(entries) <= 80:
            raise ValueError("trouble_health_inventory")
        names = []
        for entry in entries:
            if (type(entry) is not dict or set(entry) != {"component", flag}
                    or type(entry["component"]) is not str
                    or re.fullmatch(r"(?:(?:booster|ship)_main_\d+|rcs_[0-2]_-?1_-?1|(?:upper_)?(?:flap_(?:aft|forward)_-?1|grid_fin_\d+))", entry["component"]) is None
                    or type(entry[flag]) is not bool):
                raise ValueError("trouble_health_entry")
            names.append(entry["component"])
        if len(set(names)) != len(names):
            raise ValueError("trouble_health_duplicate")
    return deepcopy(row)


def fault_observed(row):
    health = row["component_health"]
    return (any(not x["available"] for x in health["engine_health"])
        or any(x["jammed"] for x in health["actuator_health"])
        or row["body"] == "ship" and health["payload_mechanism"] == "blocked" or not health["tower_ready"])


def observe(state, vehicle, engine, body, phase, *, released=0, sequencer=None,
            withheld=False, return_inhibited=False, return_deadline=None):
    """Explicit ideal kinematic channel plus synthetic diagnostic and fuel proxy.

    Neither hidden mass/inertia nor manifest seed/targets/delays crosses this
    boundary. Diagnostics report only already-applied component availability.
    """
    from . import starship_sixdof as dyn
    from . import starship_physics as env
    from .starship_mission_director import fuel_sensor
    current = dyn.observe(state, vehicle)
    return validate_observation({"time_s": state.time_s, "body": body, "phase": phase,
        "altitude_m": float(current["altitude_m"]), "speed_mps": float(env.norm(state.v_eci_mps)),
        "body_rate_rad_s": float(env.norm(state.omega_body_rad_s)),
        "fuel_kg": fuel_sensor(state.propellant_kg, state.time_s, body),
        "component_health": engine.observation(state, vehicle, body), "released_count": released,
        "sequencer_state": sequencer, "deployment_withheld": withheld,
        "return_inhibited": return_inhibited,
        "return_deadline_s": return_deadline if phase == "orbital_coast" else None})


def check_action(grant, action, row, request=None):
    """Independent execution-side bound; never a recovery feasibility verdict."""
    validate_observation(row)
    if grant.get("schema") != SCHEMA or action not in grant["actions"]:
        return "outside_approved_actions"
    if action == "collect_status" and (row["phase"] in grant["diagnostic_terminal_phases"]
            or min(grant["duration_s"], row["return_deadline_s"] or float("inf"))-row["time_s"] < grant["diagnostic_delay_s"]):
        return "diagnostic_window_unavailable"
    if request is not None:
        if row["time_s"] >= request["deadline_s"]:
            return "decision_expired"
        if row["body"] != request["observation"]["body"]:
            return "wrong_body"
        old = request["observation"]
        def health_signature(value):
            irrelevant = {"observed_at_s"} | ({"payload_mechanism"} if value["body"] == "booster" else set())
            return {k:v for k,v in value["component_health"].items() if k not in irrelevant}
        if action == "continue_mission" and (row["phase"] != old["phase"] or health_signature(row) != health_signature(old)):
            return "continue_observation_stale"
    if action == "withhold_deployment" and (row["body"] != "ship" or row["released_count"] >= 26
            or row["phase"] not in ("stack_ascent", "ship_ascent", "orbital_coast")):
        return "deployment_authority_not_applicable"
    if action == "inhibit_return" and (row["body"] != "ship" or row["phase"] != "orbital_coast"
            or row["return_deadline_s"] is None or row["time_s"] >= row["return_deadline_s"]):
        return "return_already_committed_or_unavailable"
    return None


def allowed_actions(grant, row):
    return [a for a in grant["actions"] if check_action(grant, a, row) is None]


def recent_actions(records, body, at_s, limit):
    """Past executed actions only; never expose future diagnostic completion."""
    result = []
    for record in records:
        if record["observation"]["body"] != body or record["time_s"] > at_s:
            continue
        diagnostic = record.get("diagnostic_observation")
        completed = diagnostic is not None and diagnostic["time_s"] <= at_s
        def health(row):
            irrelevant = {"observed_at_s"} | ({"payload_mechanism"} if body == "booster" else set())
            return {k:v for k,v in row["component_health"].items() if k not in irrelevant}
        result.append({"time_s": record["time_s"], "action": record["action"],
            "diagnostic_status": "completed" if completed else "pending" if record["action"] == "collect_status" else "not_requested",
            "diagnostic_health_unchanged": health(diagnostic) == health(record["observation"]) if completed else None})
    return result[-limit:]


def validate_request(request, grant):
    if (type(request) is not dict or set(request) != {"schema", "request_id", "authority_sha256",
            "observation", "triggers", "allowed_actions", "deadline_s", "deep_reasoning_allowed", "recent_actions"}
            or request["schema"] != "missionos.starship_trouble_request.v1"
            or request["authority_sha256"] != digest(grant)
            or type(request["request_id"]) is not str or re.fullmatch(r"trouble-(?:ship|booster)-\d+", request["request_id"]) is None):
        raise ValueError("trouble_request_fields_or_binding")
    row = validate_observation(request["observation"])
    history = request["recent_actions"]
    if type(history) is not list or len(history) > grant["recent_action_limit"]:
        raise ValueError("trouble_request_history")
    previous = -1.
    for item in history:
        if (type(item) is not dict or set(item) != {"time_s", "action", "diagnostic_status", "diagnostic_health_unchanged"}
                or type(item["time_s"]) not in (int, float) or not math.isfinite(item["time_s"])
                or not previous <= item["time_s"] <= row["time_s"] or item["action"] not in grant["actions"]
                or item["diagnostic_status"] not in ("completed", "pending", "not_requested")
                or (item["diagnostic_status"] == "not_requested") != (item["action"] != "collect_status")
                or (type(item["diagnostic_health_unchanged"]) is not bool if item["diagnostic_status"] == "completed" else item["diagnostic_health_unchanged"] is not None)):
            raise ValueError("trouble_request_history")
        previous = item["time_s"]
    if (request["allowed_actions"] != allowed_actions(grant, row)
            or type(request["triggers"]) is not list or not request["triggers"]
            or any(x not in ("initial_monitor", "health_changed", "phase_changed", "periodic_monitor", "diagnostic_ready") for x in request["triggers"])):
        raise ValueError("trouble_request_choices_or_triggers")
    window = grant["orbital_decision_s"] if row["phase"] == "orbital_coast" else grant["short_decision_s"]
    expected = min(row["time_s"]+window, grant["duration_s"], row["return_deadline_s"] or float("inf"))
    if request["deadline_s"] != expected or expected <= row["time_s"]:
        raise ValueError("trouble_request_deadline")
    deep = row["body"] == "ship" and row["phase"] == "orbital_coast" and expected-row["time_s"] >= 150.
    if request["deep_reasoning_allowed"] is not deep:
        raise ValueError("trouble_request_reasoning_window")
    return deepcopy(request)


def valid_live_response(response, request, grant):
    """Strict local transport evidence, not cryptographic provider attestation."""
    from src.intelligence.starship_trouble_agent import public_input, evidence_table
    from src.intelligence.starship_flight_supervisor import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES
    try:
        if (type(response) is not dict or set(response) != {"request_id", "request_sha256", "mode", "action", "route",
                "summary", "evidence_ids", "evidence_values", "model_inference_invoked", "selected_invocation", "jev_invocation", "llm_invocation"}
                or response["mode"] != "live" or response["request_id"] != request["request_id"]
                or response["request_sha256"] != digest(request) or response["model_inference_invoked"] is not True
                or response["action"] not in request["allowed_actions"]):
            return False
        public_hash = digest(public_input(request, grant))
        window = request["deadline_s"]-request["observation"]["time_s"]
        def confirmed(receipt, provider, model):
            if type(receipt) is not dict:
                return False
            expected = {"schema_version": "runtime_invocation_evidence.v1", "invocation_kind": "decision_api",
                "mode": "live", "provider": provider, "status": "succeeded", "requested_model_id": model,
                "model_id": model, "model_inference_status": "confirmed_by_valid_response",
                "public_input_sha256": public_hash, "retries": 0, "redirects": 0,
                "maximum_request_bytes": MAX_REQUEST_BYTES, "maximum_response_bytes": MAX_RESPONSE_BYTES,
                "maximum_instance_calls": None, "reserved_call_slot": None}
            if any(receipt.get(k) != v for k, v in expected.items()):
                return False
            if any(receipt.get(k) is not True for k in ("call_attempted", "response_received", "complete_response_observed", "call_succeeded", "model_inference_invoked")):
                return False
            if any(receipt.get(k) is not False for k in ("approval_granted", "dispatch", "execution_authorized", "physical_execution", "model_value_claim")):
                return False
            if any(type(receipt.get(k)) is not str or re.fullmatch(r"[0-9a-f]{64}", receipt[k]) is None for k in ("request_sha256", "response_sha256")):
                return False
            for key, limit in (("request_bytes", "maximum_request_bytes"), ("response_bytes_read", "maximum_response_bytes")):
                if type(receipt.get(key)) is not int or type(receipt.get(limit)) is not int or not 0 < receipt[key] <= receipt[limit]:
                    return False
            timeout, latency = receipt.get("timeout_seconds"), receipt.get("latency_ms")
            if (type(timeout) not in (int, float) or type(latency) not in (int, float)
                    or not math.isfinite(timeout) or not math.isfinite(latency)
                    or not 3. <= timeout <= min(120. if provider == "deepseek" else 30., window-2.)
                    or not 0 <= latency <= window*1000.):
                return False
            return True
        jev, llm = response["jev_invocation"], response["llm_invocation"]
        if not confirmed(jev, "typesafe", grant["provider_models"]["jev"]) or jev.get("selected_choice") != response["route"]:
            return False
        refs = response["evidence_ids"]
        if type(refs) is not list or len(refs) > 12 or any(type(ref) is not str for ref in refs) or len(set(refs)) != len(refs):
            return False
        table = evidence_table(request["observation"])
        if response["evidence_values"] != {key: table[key] for key in refs}:
            return False
        if response["route"] == "deep_reasoning":
            return (request["deep_reasoning_allowed"] is True and confirmed(llm, "deepseek", grant["provider_models"]["deepseek"])
                and response["selected_invocation"] == llm and llm.get("selected_action") == response["action"]
                and llm.get("thinking_enabled") is True and llm.get("reasoning_effort") == grant["deepseek_reasoning_effort"]
                and llm.get("maximum_output_tokens") == grant["deepseek_maximum_output_tokens"]
                and llm.get("timeout_seconds") == grant["deepseek_provider_timeout_s"]
                and jev["latency_ms"]+llm["latency_ms"] <= window*1000.
                and type(response["summary"]) is str and 0 < len(response["summary"]) <= 600 and bool(refs))
        return (response["route"] == response["action"] and response["selected_invocation"] == jev
            and response["summary"] == "" and refs == [] and type(llm) is dict
            and llm.get("status") == "not_routed" and llm.get("call_attempted") is False
            and llm.get("model_inference_invoked") is False)
    except (ValueError, KeyError, TypeError, OverflowError):
        return False


class TroubleControl:
    def __init__(self, grant, profile, manifest, sources, *, mailbox=None, fixture=None,
                 clock=time.monotonic, sleep=time.sleep):
        if grant != authority(profile, manifest, sources, grant["duration_s"], grant["mode"], provider_models=grant["provider_models"]):
            raise ValueError("trouble_control_grant_mismatch")
        if grant["mode"] == "live" and (mailbox is None or fixture is not None):
            raise ValueError("trouble_control_live_mailbox_required")
        self.grant, self.mailbox = deepcopy(grant), Path(mailbox) if mailbox else None
        self.clock, self.sleep, self.fixture = clock, sleep, fixture
        self.pending, self.previous, self.last_request, self.diagnostic_due = {}, {}, {}, {}
        self.triggers, self.records, self.observations = {}, [], []
        self.withheld, self.return_inhibited = False, False
        self.sequence = 0

    def admit(self, profile, manifest, duration_s):
        if (self.grant["profile_sha256"] != digest(profile) or self.grant["manifest_sha256"] != digest(manifest)
                or self.grant["duration_s"] != duration_s):
            raise ValueError("trouble_control_plant_binding_mismatch")

    def _remember(self, row, witness):
        item = {"observation": deepcopy(row), "state": deepcopy(witness)}
        if not self.observations or self.observations[-1] != item:
            self.observations.append(item)

    def step(self, row, witness):
        row = validate_observation(row)
        body, now = row["body"], row["time_s"]
        previous = self.previous.get(body)
        triggers = self.triggers.setdefault(body, set())
        if previous is None:
            triggers.add("initial_monitor")
        else:
            if now < previous["time_s"]:
                raise ValueError("trouble_control_clock_reversed")
            if row["phase"] != previous["phase"]:
                triggers.add("phase_changed")
            relevant = HEALTH_KEYS-{"observed_at_s"} - ({"payload_mechanism"} if body == "booster" else set())
            if any(row["component_health"][k] != previous["component_health"][k] for k in relevant):
                triggers.add("health_changed")
        if now-self.last_request.get(body, now) >= self.grant["periodic_monitor_s"]:
            triggers.add("periodic_monitor")
        if now >= self.diagnostic_due.get(body, float("inf")):
            del self.diagnostic_due[body]
            triggers.add("diagnostic_ready")
            for record in reversed(self.records):
                if record["observation"]["body"] == body and record["action"] == "collect_status":
                    record["diagnostic_observation"] = deepcopy(row)
                    record["diagnostic_completion"] = {"status": "completed", "time_s": now, "reason": None}
                    self._remember(row, witness)
                    break
        self.previous[body] = row
        for record in self.records:
            if record["observation"]["body"] == body and record["later_observation"] is None and now > record["time_s"]:
                record["later_observation"] = deepcopy(row)
                self._remember(row, witness)
        pending = self.pending.get(body)
        if pending:
            request = pending["request"]
            if self.mailbox is not None:
                elapsed = now-request["observation"]["time_s"]
                while self.grant["mode"] == "live" and self.clock()-pending["wall_started"] < elapsed:
                    self.sleep(min(.01, elapsed-(self.clock()-pending["wall_started"])))
                if self.grant["mode"] == "fixture":
                    # Local fixture broker still crosses the real subprocess
                    # boundary, without pacing a simulated flight in real time.
                    until = self.clock()+.1
                    while not (self.mailbox/body/"response.json").exists() and self.clock() < until:
                        self.sleep(.005)
                try:
                    response = json.loads((self.mailbox/body/"response.json").read_text())
                except (OSError, ValueError):
                    response = None
                if type(response) is not dict or response.get("request_id") != request["request_id"]:
                    response = None
            else:
                response = self.fixture(deepcopy(request)) if self.fixture else None
            expired = now >= request["deadline_s"]
            if now > request["observation"]["time_s"] and (response is not None or expired):
                bound = (type(response) is dict and response.get("request_sha256") == digest(request)
                    and response.get("request_id") == request["request_id"] and response.get("mode") == self.grant["mode"])
                if self.grant["mode"] == "live":
                    bound = bound and valid_live_response(response, request, self.grant)
                proposed = response.get("action") if bound else None
                rejection = check_action(self.grant, proposed, row, request) if bound else "invalid_or_missing_response"
                if rejection is None:
                    action, fallback = proposed, False
                else:
                    action = "withhold_deployment" if fault_observed(row) and "withhold_deployment" in allowed_actions(self.grant, row) else "continue_mission"
                    fallback = True
                fallback_rejection = check_action(self.grant, action, row)
                if fallback_rejection is not None:
                    raise ValueError("trouble_fallback_outside_grant")
                record = {"request": request, "response": response, "observation": deepcopy(row), "time_s": now,
                    "accepted_action": proposed if not fallback else None, "action": action, "fallback": fallback,
                    "rejection": rejection, "fallback_rejection": fallback_rejection, "later_observation": None,
                    "diagnostic_observation": None,
                    "diagnostic_completion": {"status": "pending" if action == "collect_status" else "not_requested", "time_s": None, "reason": None}}
                self.records.append(record)
                self._remember(row, witness)
                del self.pending[body]
                if action == "withhold_deployment":
                    self.withheld = True
                if action == "inhibit_return":
                    self.return_inhibited = True
                if action == "collect_status":
                    self.diagnostic_due[body] = now+self.grant["diagnostic_delay_s"]
                if self.mailbox:
                    publish(self.mailbox/body/"cancelled.json", {"request_id": request["request_id"], "request_sha256": digest(request)})
                return action
        if body not in self.pending and triggers and now < min(self.grant["duration_s"], row["return_deadline_s"] or float("inf")):
            window = self.grant["orbital_decision_s"] if row["phase"] == "orbital_coast" else self.grant["short_decision_s"]
            deadline = min(now+window, self.grant["duration_s"], row["return_deadline_s"] or float("inf"))
            request = {"schema": "missionos.starship_trouble_request.v1", "request_id": f"trouble-{body}-{self.sequence}",
                "authority_sha256": digest(self.grant), "observation": deepcopy(row), "triggers": sorted(triggers),
                "allowed_actions": allowed_actions(self.grant, row), "deadline_s": deadline,
                "recent_actions": recent_actions(self.records, body, now, self.grant["recent_action_limit"]),
                "deep_reasoning_allowed": body == "ship" and row["phase"] == "orbital_coast" and deadline-now >= 150.}
            validate_request(request, self.grant)
            self.sequence += 1
            self.last_request[body] = now
            triggers.clear()
            self.pending[body] = {"request": request, "wall_started": self.clock()}
            self._remember(row, witness)
            if self.mailbox:
                publish(self.mailbox/body/"request.json", request)
        return None

    def finish(self):
        return {"schema": "missionos.starship_trouble_control_record.v1", "authority": deepcopy(self.grant),
            "records": deepcopy(self.records), "observation_witnesses": deepcopy(self.observations),
            "pending_requests": [deepcopy(p["request"]) for p in self.pending.values()],
            "deployment_withheld": self.withheld, "return_inhibited": self.return_inhibited,
            "fault_return_qualified": False, "physical_execution": False}


def record_terminal_interruptions(run):
    """Record real terminal evidence, never synthesize a diagnostic observation.

    Called after the concurrent executor returns; this does not mutate physics
    or retroactively mark an interrupted request as completed.
    """
    for record in run["trouble_control"]["records"]:
        if record["diagnostic_completion"]["status"] != "pending":
            continue
        body = run if record["observation"]["body"] == "ship" else run.get("booster_run")
        if body is None:
            continue
        outcome = body["outcome"]
        end = body["final_state"]["time_s"]
        if outcome["termination"] == "surface_contact" and end >= record["time_s"]:
            record["diagnostic_completion"] = {"status": "interrupted", "time_s": end,
                "reason": "body_surface_contact", "outcome_sha256": digest(outcome)}
