"""Host-only supplemental ascent/Booster judgments over observed numbers.

The worker supplies a strict numerical projection, not a plant object or fault
schedule. This adapter grants no permission and sends no request at import.
One Jev assessment may explicitly delegate its same original Booster epoch to
DeepSeek; invalid responses are never retried or replaced with a desired choice.
"""
from copy import deepcopy
from hashlib import sha256
import math
import os
import time
from threading import Lock

from .space_ops_investigator import JEV_MODEL, canonical_bytes, _json
from .starship_flight_supervisor import _receipt
from .starship_m2_broker import DeadlineTransport
from .starship_ship_capability_agent import _decode_jev, _usage
from src.runtime.starship_final_protocol import validate_request as validate_rpc

SCHEMA = "missionos.starship_final_supervision_observation.v1"
RESPONSE = "missionos.starship_final_supervision_judgment.v1"
LIVE_OPT_IN_ENV = "MISSIONOS_STARSHIP_FINAL_SUPERVISION_LIVE_ENABLED"
DEEPSEEK_MODEL = "deepseek-flash"
DELEGATE = "request_deep_reasoning"
ACTIONS = {"ascent": ("continue_mission", "withhold_deployment"),
           "booster": ("splashdown", "inhibit_recovery"),
           "sensor": ("continue_nominal_assumption", "request_independent_tracking", "adopt_accelerometer_estimate")}
EFFECTS = {
    "continue_mission": "Propose retaining the current ascent mission. Independent fast safety, propulsion and later release gates remain in force; this cannot command an engine.",
    "withhold_deployment": "Propose withholding later payload release. This does not abort ascent, change thrust, invent inventory or establish a successful return.",
    "splashdown": "Propose the available sea-recovery plan only if current independent checks and approved scope permit it. This does not command engines or prove a successful touchdown.",
    "inhibit_recovery": "Propose withholding the candidate recovery sequence for independent handling. This does not shut down running engines, command a catch, or prove that another outcome is safe.",
    DELEGATE: "Ask DeepSeek to judge the same observations within this request's original remaining epoch. This creates no authority and does not renew a deadline.",
}
COMMON = frozenset({"time_s", "source_time_s", "available_time_s", "altitude_m",
    "ground_speed_mps", "vertical_speed_mps", "horizontal_speed_mps", "propellant_kg",
    "body_rate_rad_s", "tilt_deg", "dynamic_pressure_pa", "attitude_error_deg",
    "observed_available_engine_count", "expected_engine_count"})
REQUIRED = frozenset({"time_s", "source_time_s", "available_time_s", "altitude_m",
    "ground_speed_mps", "propellant_kg", "body_rate_rad_s"})
EXTRA = {"ascent": frozenset({"measured_acceleration_mps2", "expected_acceleration_mps2",
    "minimum_orbit_margin_m", "deployment_allowed"}),
    "booster": frozenset({"distance_to_tower_m", "distance_to_splashdown_m",
    "recovery_control_available", "splashdown_feasible", "tower_ready"})}
BOOLEAN = frozenset({"deployment_allowed", "recovery_control_available", "splashdown_feasible", "tower_ready"})
SIGNED = frozenset({"vertical_speed_mps", "measured_acceleration_mps2", "expected_acceleration_mps2", "minimum_orbit_margin_m"})
HISTORY_FACTS = {"history.supported_catches": 0,
    "authority.catch_allowed": False, "evidence.current_state_catch_qualification_available": False}
HISTORY_SUMMARY = (
    "This simulator has zero demonstrated launch-connected supported tower catches. "
    "The catch development record contains failed approaches and does not certify the "
    "current state, nor prove catch physically impossible. Catch is outside this request's "
    "authority and has no eligible action. Current sea-recovery eligibility must come from "
    "current numerical checks, not historical catch failures or a tower image."
)
REJECTIONS = frozenset({"response_shape", "model_mismatch", "choice_shape",
    "choice_or_distribution_keys", "probability_sum_or_range", "choices_shape", "finish_reason",
    "message_shape", "answer_json", "answer_shape_or_reference_ids", "transport_error",
    "transport_decoder_not_observed", "incomplete_native_receipt", "response_after_deadline"})


def digest(value):
    return sha256(canonical_bytes(value)).hexdigest()


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def require(value, reason):
    if not value:
        raise ValueError(reason)


def decision_request(request_id, domain, time_s, deadline_s, *, scope_sha256, facts, allowed_actions=None):
    """Build a supplemental FinalObservationClient request, without sending it."""
    from src.runtime.starship_final_protocol import request
    actions = (list(ACTIONS.get(domain, ())) + ([DELEGATE] if domain == "booster"
        and number(time_s) and number(deadline_s) and deadline_s-time_s >= 150. else [])
        if allowed_actions is None else allowed_actions)
    return validate_request(request(request_id, domain, time_s, deadline_s, {
        "schema": SCHEMA, "scope_sha256": scope_sha256,
        "facts": deepcopy(facts), "allowed_actions": deepcopy(actions)}))


def validate_request(request):
    request = validate_rpc(request)
    observation, phase = request["observation"], request["domain"]
    if phase == "sensor":
        from src.runtime.starship_final_sensor_execution import validate_public_request
        validate_public_request(observation)
        return deepcopy(request)
    require(phase in ACTIONS and type(observation) is dict and set(observation) == {
        "schema", "scope_sha256", "facts", "allowed_actions"} and observation["schema"] == SCHEMA,
        "final_supervision_observation_contract")
    scope = observation["scope_sha256"]
    require(type(scope) is str and len(scope) == 64 and all(x in "0123456789abcdef" for x in scope),
        "final_supervision_scope_hash")
    facts, actions = observation["facts"], observation["allowed_actions"]
    require(type(facts) is dict and REQUIRED <= facts.keys() and facts.keys() <= COMMON | EXTRA[phase]
        and facts["time_s"] == request["time_s"], "final_supervision_observed_fields")
    for key, value in facts.items():
        require(type(value) is bool if key in BOOLEAN else number(value), "final_supervision_fact_type")
        if key not in BOOLEAN and key not in SIGNED:
            require(value >= 0., "final_supervision_fact_range")
        if key in {"observed_available_engine_count", "expected_engine_count"}:
            require(type(value) is int and 0 <= value <= 64, "final_supervision_engine_count")
    require(0 <= facts["source_time_s"] <= facts["available_time_s"] <= request["time_s"]
        and request["time_s"]-facts["source_time_s"] <= 5., "final_supervision_observation_freshness")
    eligible = set(ACTIONS[phase]) | ({DELEGATE} if phase == "booster" and
        request["deadline_s"]-request["time_s"] >= 150. else set())
    require(type(actions) is list and actions and all(type(action) is str and action in eligible for action in actions)
        and len(set(actions)) == len(actions) and set(ACTIONS[phase]) <= set(actions),
        "final_supervision_eligible_actions")
    require(len(canonical_bytes(request)) <= 12000, "final_supervision_request_budget")
    return deepcopy(request)


def public_input(request, provider):
    request = validate_request(request)
    require(provider in ("typesafe", "deepseek"), "final_supervision_provider")
    actions = [action for action in request["observation"]["allowed_actions"] if provider != "deepseek" or action != DELEGATE]
    require(provider != "deepseek" or request["domain"] in ("booster", "sensor") and
        request["deadline_s"]-request["time_s"] >= 150. and DELEGATE in request["observation"]["allowed_actions"],
        "final_supervision_deepseek_scope")
    facts = deepcopy(request["observation"]["facts"])
    result = {"phase": request["domain"], "time_s": request["time_s"], "deadline_s": request["deadline_s"],
        "evidence": facts, "allowed_actions": actions,
        "effects": {action: (request["observation"]["effects"][action] if request["domain"] == "sensor"
            else EFFECTS[action]) for action in actions},
        "scope": "Experimental numerical-observation judgment only. LLM judges; human approves; Rules constrain; Executor acts; Verifier checks. A proposal is not approval, dispatch, recovery or real-flight certification. No engine, attitude or arm command is eligible."}
    if request["domain"] == "booster":
        facts.update(HISTORY_FACTS)
        result["recovery_history"] = HISTORY_SUMMARY
    return result


class FinalSupervisionAgent:
    """One send per provider/request; unlimited distinct request IDs, no retry.

    ``live_authorized(request, provider)`` must affirm the exact source-bound
    supplemental request. An injected fixture transport is mandatory in fixture
    mode. Fixture decoding remains fixture evidence regardless of its contents.
    """
    def __init__(self, *, mode="fixture", transport=None, live_authorized=lambda _r, _p: False,
                 active=lambda: True, clock=time.monotonic):
        require(mode in ("fixture", "live") and all(callable(x) for x in (live_authorized, active, clock))
            and (transport is None or callable(getattr(transport, "_invoke", None))),
            "final_supervision_configuration")
        require(mode != "fixture" or transport is not None, "final_supervision_fixture_transport_required")
        self.mode, self.transport, self.live_authorized = mode, transport, live_authorized
        self.active, self.clock, self.sessions, self.records, self._lock = active, clock, {}, [], Lock()

    def assess(self, request, *, provider="typesafe", remaining_s, outer_deadline_monotonic):
        require(self._lock.acquire(blocking=False), "final_supervision_concurrent_assessment")
        try:
            return self._assess(request, provider, remaining_s, outer_deadline_monotonic)
        finally:
            self._lock.release()

    def _assess(self, request, provider, remaining_s, outer):
        request, now = validate_request(request), self.clock()
        public = public_input(request, provider)
        require(self.active() is True and number(now) and number(remaining_s) and remaining_s > 0
            and number(outer) and now < outer and outer-now <= request["deadline_s"]-request["time_s"]+1e-6,
            "final_supervision_original_epoch_required")
        if self.mode == "live":
            require(os.environ.get(LIVE_OPT_IN_ENV) == "1" and self.live_authorized(deepcopy(request), provider) is True,
                "final_supervision_live_authorization_required")
        identity, request_hash = request["request_id"], digest(request)
        session = self.sessions.get(identity)
        if provider == "typesafe":
            require(session is None, "final_supervision_request_already_consumed")
            session = {"request_sha256": request_hash, "outer_deadline_monotonic": outer,
                "original_started_wall_s": now, "providers": [], "delegated": False}
            self.sessions[identity] = session
        else:
            require(session is not None and session["request_sha256"] == request_hash
                and session["outer_deadline_monotonic"] == outer and session["delegated"] is True
                and provider not in session["providers"], "final_supervision_explicit_same_epoch_delegation_required")
            #150s is the original decision-window declaration. Queue/setup/Jev
            #time is consumed, never renewed; 120s provider +2s application
            #reserve must still fit before a DeepSeek request can start.
            require(min(remaining_s, outer-now) >= 122., "final_supervision_deepseek_headroom")
        session["providers"].append(provider)
        model = JEV_MODEL if provider == "typesafe" else DEEPSEEK_MODEL
        deadline = min(outer, now+(35. if provider == "typesafe" else 150.), now+remaining_s)
        receipt = _receipt(self.mode, provider, public)
        receipt.update(request_model=model, requested_model_id=model, request_input_sha256=digest(public),
            original_request_sha256=request_hash, original_deadline_monotonic=outer,
            decision_started_wall_s=now, decision_deadline_monotonic=deadline,
            maximum_instance_calls=None, raw_reasoning_recorded=False,
            maximum_output_tokens=16384 if provider == "deepseek" else None,
            transport_mode=self.mode, retry_allowed=False)
        if provider == "typesafe":
            payload = {"model": model, "state": public, "questions": {"decision": {
                "type": "choice", "instructions": "Choose an eligible current mission proposal from the observed evidence and action effects. Do not infer a causal fault from missing evidence. Explicitly request deeper reasoning only when that action is offered and useful. A choice is never permission or a low-level command.",
                "criteria": public["effects"]}}}
        else:
            payload = {"model": model, "stream": False, "thinking": {"type": "enabled"},
                "reasoning_effort": "low", "max_tokens": 16384, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": "Judge only the supplied observations and finite authority. Return JSON with exactly action, summary, reference_ids. action must be allowed; summary is a brief external operational explanation (1 to 600 characters), never an internal reasoning transcript. Cite 1 to 16 unique numerical/boolean evidence IDs. History does not certify this current state. Do not invent observations, permissions or successful recovery."},
                    {"role": "user", "content": canonical_bytes(public).decode()}]}
        decoded, resolved = [], []
        def reject(reason):
            receipt["validation_error"] = reason
            raise ValueError("final_supervision_provider_response_rejected")
        def decode(value):
            require(not decoded, "final_supervision_duplicate_decode")
            if type(value) is not dict:
                reject("response_shape")
            _usage(value, receipt)
            if value.get("model") != model:
                reject("model_mismatch")
            if provider == "typesafe":
                try:
                    answer = _decode_jev(value, model)
                except ValueError:
                    reject("choice_shape")
                probabilities = answer["probabilities"]
                if (type(probabilities) is not dict or set(probabilities) != set(public["allowed_actions"])
                        or type(answer["choice"]) is not str or answer["choice"] not in public["allowed_actions"]):
                    reject("choice_or_distribution_keys")
                values = [*probabilities.values(), answer["confidence"]]
                if (any(not number(x) or not 0 <= x <= 1 for x in values)
                        or not math.isclose(sum(probabilities.values()), 1., abs_tol=.001)):
                    reject("probability_sum_or_range")
                receipt.update(native_choice_probabilities=deepcopy(probabilities), native_choice_confidence=answer["confidence"])
                result = {"action": answer["choice"], "summary": "", "reference_ids": []}
            else:
                choices = value.get("choices")
                if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
                    reject("choices_shape")
                choice = choices[0]
                finish = choice.get("finish_reason")
                receipt["finish_reason"] = finish if finish in ("stop", "length", "content_filter", "tool_calls") else "unknown"
                if finish != "stop":
                    reject("finish_reason")
                message = choice.get("message")
                if (type(message) is not dict or message.get("role") != "assistant" or message.get("tool_calls")
                        or message.get("function_call") or type(message.get("content")) is not str
                        or len(message["content"]) > 8000):
                    reject("message_shape")
                try:
                    result = _json(message["content"])
                except (ValueError, TypeError):
                    reject("answer_json")
                if (type(result) is not dict or set(result) != {"action", "summary", "reference_ids"}
                        or type(result["action"]) is not str or result["action"] not in public["allowed_actions"]
                        or type(result["summary"]) is not str or not 1 <= len(result["summary"]) <= 600
                        or type(result["reference_ids"]) is not list or not 1 <= len(result["reference_ids"]) <= 16
                        or any(type(ref) is not str or ref not in public["evidence"] for ref in result["reference_ids"])
                        or len(set(result["reference_ids"])) != len(result["reference_ids"])):
                    reject("answer_shape_or_reference_ids")
                resolved.extend({"reference_id": ref, "value": public["evidence"][ref]} for ref in result["reference_ids"])
            decoded.append(deepcopy(result))
            return result, model
        def checked_decode(value):
            try:
                return decode(value)
            except (ValueError, TypeError, KeyError, OverflowError, UnicodeError):
                receipt.setdefault("validation_error", "response_shape")
                raise
        transport = self.transport or DeadlineTransport(
            lambda: self.active() is True and self.clock() < deadline,
            lambda: max(0., deadline-self.clock()), jev_only=provider == "typesafe",
            approved_provider_timeout_s=lambda name: 120. if name == "deepseek" else 30.)
        try:
            answer = transport._invoke(provider, payload, receipt, checked_decode)
        except Exception:
            receipt["status"] = "transport_failed"
            receipt.setdefault("validation_error", "transport_error")
            answer = None
        try:
            valid = (answer is not None and len(decoded) == 1 and digest(answer) == digest(decoded[0])
                and receipt["status"] in ("succeeded", "fixture_succeeded"))
        except (ValueError, TypeError, KeyError, OverflowError, UnicodeError):
            valid = False
        if answer is not None and not valid:
            receipt.setdefault("validation_error", "transport_decoder_not_observed")
        if valid and (self.clock() >= deadline or self.active() is not True):
            receipt.update(status="response_after_deadline", validation_error="response_after_deadline")
            valid = False
        if self.mode == "live" and valid and not (
                receipt.get("requested_model_id") == receipt.get("model_id") == model
                and all(receipt.get(k) is True for k in ("call_attempted", "response_received", "complete_response_observed", "call_succeeded", "model_inference_invoked"))
                and receipt.get("retries") == receipt.get("redirects") == 0):
            receipt["validation_error"] = "incomplete_native_receipt"
            valid = False
        if self.mode == "fixture":
            receipt.update(status="fixture_succeeded" if valid else "fixture_rejected", provider=None,
                mode="fixture", transport_mode="fixture", invocation_kind="not_invoked",
                call_attempted=False, call_succeeded=False, hosted_model_attempted=False,
                model_inference_invoked=False, model_inference_status="fixture_not_inference",
                local_fixture_attempted=True, local_fixture_response_observed=bool(decoded))
        if receipt.get("validation_error") not in REJECTIONS:
            receipt.pop("validation_error", None)
        receipt["decision_finished_wall_s"] = self.clock()
        session["delegated"] = bool(provider == "typesafe" and valid and answer["action"] == DELEGATE)
        result = {"schema": RESPONSE, "request_id": identity, "request_sha256": request_hash,
            "domain": request["domain"], "provider": provider, "mode": self.mode,
            "action": answer["action"] if valid else None, "summary": answer["summary"] if valid else "",
            "references": deepcopy(resolved) if valid else [], "provider_receipt": receipt,
            "status": ("valid" if self.mode == "live" else "fixture_valid") if valid else "no_valid_judgment",
            "model_inference_invoked": self.mode == "live" and valid,
            "execution_permission_granted": False, "free_prose_verified": False,
            "reference_values_verified": bool(valid and resolved), "internal_reasoning_verified": False,
            "maximum_instance_calls": None, "automatic_retry": False}
        self.records.append(deepcopy(result))
        return result

    def record(self):
        return {"schema": "missionos.starship_final_supervision_agent.v1", "mode": self.mode,
            "judgments": deepcopy(self.records), "maximum_instance_calls": None,
            "automatic_retry": False, "execution_permission_granted": False}
