"""Host-only bounded decisions over post-fault observations, never a manifest."""
from copy import deepcopy
import json
import math
from pathlib import Path
from threading import Thread
import time

from src.runtime.starship_trouble_control import digest, fault_observed, validate_request
from src.runtime.starship_mission_director import publish
from .starship_flight_supervisor import _receipt
from .starship_m2_broker import DeadlineTransport, require_live_configuration
from .space_ops_investigator import JEV_MODEL, _deepseek_model, _json, canonical_bytes

EFFECTS = {
    "continue_mission": "Keep the existing experimental timeline. Never clears a deployment or return inhibit. Fault recovery is NOT qualified.",
    "collect_status": "After two simulated seconds, sample the current diagnostic channel again and reassess. No new sensor, corrective force, or automatic deployment hold.",
    "withhold_deployment": "Irreversibly disable all remaining payload releases. Retained mass stays on the actual vehicle. No return success is promised.",
    "inhibit_return": "Irreversibly withhold deorbit until this bounded simulation ends. Orbital survival and later recovery remain unresolved. This is NOT safe-return certification.",
}


def provider_models():
    return {"jev": JEV_MODEL, "deepseek": _deepseek_model()}


def evidence_table(row):
    values = {key: row[key] for key in ("altitude_m", "speed_mps", "body_rate_rad_s", "fuel_kg", "released_count")}
    health = row["component_health"]
    values.update({"payload_mechanism": health["payload_mechanism"] if row["body"] == "ship" else "not_applicable",
        "tower_ready": health["tower_ready"]})
    for field, flag in (("engine_health", "available"), ("actuator_health", "jammed")):
        for item in health[field]:
            values[f"{item['component']}.{flag}"] = item[flag]
    return values


def public_input(request, grant):
    request = validate_request(request, grant)
    row = deepcopy(request["observation"])
    if row["body"] == "booster":
        row["component_health"]["payload_mechanism"] = "not_applicable"
    # Opaque manifest/source hashes also stay out. Only information available
    # at this observation time and preapproved action semantics are serialized.
    return {"observation": row, "triggers": request["triggers"],
        "recent_actions": request["recent_actions"],
        "allowed_actions": request["allowed_actions"], "deadline_s": request["deadline_s"],
        "deep_reasoning_allowed": request["deep_reasoning_allowed"],
        "effects": {key: EFFECTS[key] for key in request["allowed_actions"]},
        "evidence": evidence_table(row),
        "scope": "Experimental simulation only. Component health is a synthetic current diagnostic, kinematics are ideal measurements and fuel is a quantized proxy. No future failures are known. Do not extend authority or claim a faulted vehicle has a qualified return. Normal flight should not be unnecessarily interrupted."}


def fixture_decision(request, grant):
    """Deterministic transport/executor fixture, explicitly not AI evidence."""
    row = validate_request(request, grant)["observation"]
    action = "continue_mission"
    if fault_observed(row) and not row["deployment_withheld"]:
        if "diagnostic_ready" not in request["triggers"]:
            action = "collect_status"
        elif "withhold_deployment" in request["allowed_actions"]:
            action = "withhold_deployment"
    return {"request_id": request["request_id"], "request_sha256": digest(request),
        "mode": "fixture", "action": action, "summary": "", "evidence_ids": [],
        "model_inference_invoked": False, "selected_invocation": None,
        "jev_invocation": None, "llm_invocation": None}


class TroubleAgent:
    def __init__(self, grant, transport):
        self.grant, self.transport = deepcopy(grant), transport

    def assess(self, request):
        public = public_input(request, self.grant)
        if self.grant["mode"] == "fixture":
            return fixture_decision(request, self.grant)
        require_live_configuration(self.grant)
        if provider_models() != self.grant["provider_models"]:
            raise ValueError("trouble_approved_provider_model_changed")
        actions = request["allowed_actions"]
        criteria = {action: EFFECTS[action] for action in actions}
        if request["deep_reasoning_allowed"]:
            criteria["deep_reasoning"] = "Conflicting observations or consequences require deliberate assessment among these approved choices; at least 150 seconds were available at observation."
        jev, llm = _receipt("live", "typesafe", public), _receipt("live", "deepseek", public)
        for receipt in (jev, llm):
            receipt["maximum_instance_calls"] = None
        llm.update(maximum_output_tokens=self.grant["deepseek_maximum_output_tokens"],
            thinking_enabled=True, reasoning_effort=self.grant["deepseek_reasoning_effort"])
        llm["status"] = "not_routed"
        payload = {"model": JEV_MODEL, "state": public, "questions": {"decision": {
            "type": "choice", "instructions": "Choose a mission-level response using only current observations and these action effects. You cannot certify a faulted return. Use additional observation or deep_reasoning when useful; do not invent future faults or low-level commands.",
            "criteria": criteria}}}

        def decode_jev(value):
            if type(value) is not dict or value.get("model") != JEV_MODEL or set(value.get("answers", {})) != {"decision"}:
                raise ValueError("invalid_trouble_jev_response")
            answer = value["answers"]["decision"]
            if (type(answer) is not dict or set(answer) != {"type", "choice", "probabilities", "confidence"}
                    or answer["type"] != "choice" or type(answer["choice"]) is not str or answer["choice"] not in criteria
                    or type(answer["probabilities"]) is not dict or set(answer["probabilities"]) != set(criteria)):
                raise ValueError("invalid_trouble_jev_choice")
            numbers = [*answer["probabilities"].values(), answer["confidence"]]
            if (any(type(x) not in (int, float) or not math.isfinite(x) or not 0 <= x <= 1 for x in numbers)
                    or not math.isclose(sum(answer["probabilities"].values()), 1, abs_tol=.001)):
                raise ValueError("invalid_trouble_jev_distribution")
            return answer["choice"], value["model"]

        action = self.transport._invoke("typesafe", payload, jev, decode_jev)
        jev["selected_choice"] = action if jev["status"] == "succeeded" else None
        selected, summary, refs, route = jev, "", [], action
        if action == "deep_reasoning":
            selected, action = llm, None
            if provider_models() != self.grant["provider_models"]:
                raise ValueError("trouble_approved_provider_model_changed")
            model = self.grant["provider_models"]["deepseek"]
            payload = {"model": model, "stream": False, "thinking": {"type": "enabled"},
                "reasoning_effort": self.grant["deepseek_reasoning_effort"],
                "max_tokens": self.grant["deepseek_maximum_output_tokens"], "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": "Decide a simulated mission response within allowed_actions. Return only a JSON object with exactly action, summary, evidence_ids; no extra keys. action must be a string from allowed_actions. summary must be a string of 1 to 600 characters: a concise operational explanation, not a reasoning transcript. evidence_ids must be an array of 1 to 12 unique string IDs, each exactly matching a key in evidence. The serialized JSON must not exceed 8000 characters. No invented commands, safety certification or successful recovery claims. Inhibit means unresolved orbit, not a demonstrated safe outcome."},
                    {"role": "user", "content": canonical_bytes(public).decode()}]}

            def decode_llm(value):
                if type(value) is not dict or value.get("model") != model:
                    raise ValueError("invalid_trouble_llm_model")
                choices = value.get("choices")
                if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict or choices[0].get("finish_reason") != "stop":
                    raise ValueError("invalid_trouble_llm_completion")
                message = choices[0]["message"]
                if (type(message) is not dict or message.get("role") != "assistant" or message.get("tool_calls")
                        or message.get("function_call") or type(message.get("content")) is not str
                        or len(message["content"]) > 8000):
                    raise ValueError("invalid_trouble_llm_message")
                result = _json(message["content"])
                if (type(result) is not dict or set(result) != {"action", "summary", "evidence_ids"}
                        or type(result["action"]) is not str or result["action"] not in actions
                        or type(result["summary"]) is not str or not 0 < len(result["summary"]) <= 600
                        or type(result["evidence_ids"]) is not list or not 1 <= len(result["evidence_ids"]) <= 12
                        or any(type(x) is not str or x not in public["evidence"] for x in result["evidence_ids"])
                        or len(set(result["evidence_ids"])) != len(result["evidence_ids"])):
                    raise ValueError("invalid_trouble_llm_action_or_references")
                # Provider reasoning_content is neither read nor saved.
                return result, model

            result = self.transport._invoke("deepseek", payload, llm, decode_llm)
            llm["selected_action"] = result["action"] if result is not None and llm["status"] == "succeeded" else None
            if result is not None:
                action, summary, refs = result["action"], result["summary"], result["evidence_ids"]
        return {"request_id": request["request_id"], "request_sha256": digest(request), "mode": "live",
            "action": action, "route": route, "summary": summary, "evidence_ids": refs,
            "evidence_values": {key: public["evidence"][key] for key in refs},
            "model_inference_invoked": jev["model_inference_invoked"] or llm["model_inference_invoked"],
            "selected_invocation": selected, "jev_invocation": jev, "llm_invocation": llm}


def serve(mailbox, grant, child, active):
    """One broker per body prevents an orbital model from blocking the booster."""
    root = Path(mailbox)
    def body_loop(body):
        seen = set()
        while child.poll() is None and active():
            try:
                request = json.loads((root/body/"request.json").read_text())
                validate_request(request, grant)
                token = request["request_id"]
                if token in seen:
                    time.sleep(.005)
                    continue
                seen.add(token)
                started = time.monotonic()
                window = request["deadline_s"]-request["observation"]["time_s"]
                def current():
                    if child.poll() is not None or not active():
                        return False
                    try:
                        cancelled = json.loads((root/body/"cancelled.json").read_text())
                    except (OSError, ValueError):
                        cancelled = {}
                    return cancelled.get("request_id") != token
                def remaining():
                    return window-(time.monotonic()-started)
                def audit(stage, provider, receipt):
                    publish(root/"attempts"/f"{token}-{provider}-{stage}.json", receipt)
                transport = DeadlineTransport(current, remaining,
                    jev_only=not request["deep_reasoning_allowed"], audit=audit,
                    approved_provider_timeout_s=lambda provider: grant["deepseek_provider_timeout_s"] if provider == "deepseek" and request["deep_reasoning_allowed"] else 30.)
                publish(root/"inputs"/f"{token}.json", public_input(request, grant))
                response = TroubleAgent(grant, transport).assess(request)
                publish(root/"responses"/f"{token}.json", response)
                if current():
                    publish(root/body/"response.json", response)
            except (OSError, ValueError, KeyError, TypeError) as error:
                # Do not retry a model failure. The actor independently times
                # out and applies its already-approved fallback.
                if "token" in locals():
                    publish(root/"errors"/f"{token}.json", {"error": type(error).__name__, "automatic_retry": False})
            time.sleep(.005)
    threads = [Thread(target=body_loop, args=(body,), daemon=True) for body in ("ship", "booster")]
    for thread in threads:
        thread.start()
    return threads
