"""Host-only M3 providers; independent actors retain every execution decision.

The ascent and booster ledgers account for attempts, not authenticated provider
proof or a security boundary against a malicious same-UID worker. Model keys
stay in this host process. No flight or provider request occurs at import.
"""

from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
from threading import Thread
import time

from src.runtime.starship_ascent_supervisor import CONFIG as ASCENT_CONFIG
from src.runtime.starship_mission_director import MODE_ENV, publish, validate_observation
from src.runtime.starship_replanning import digest
from src.runtime.starship_return_prediction import STATE_KEYS
from src.runtime.starship_m2_attempts import empty
from .space_ops_investigator import JEV_MODEL
from .starship_flight_supervisor import _receipt
from .starship_m2_broker import (
    APPLY_MARGIN_S,
    MIN_PROVIDER_S,
    DeadlineTransport,
    HostAttempts,
    require_live_configuration as require_m2_configuration,
    serve_phase as serve_m2_phase,
)

LEDGER_SCHEMA = "missionos.starship_m3_host_attempts.v1"
PHASES = ("ascent", "booster", "deployment", "return")


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _deep_booster(authority, phase):
    return phase == "booster" and authority.get("prefix_provider_policy") == "jev_ascent_deepseek_booster_report_v1"


def _prefix_limit(authority, phase):
    if _deep_booster(authority, phase):
        if authority.get("maximum_booster_jev_calls") != 0 or authority.get("maximum_booster_llm_calls", 0) is not None:
            raise ValueError("m3_booster_provider_scope")
        return None
    expected = {"ascent": 4, "booster": 1}
    value = authority["maximum_" + phase + "_jev_calls"]
    if type(value) is not int or value != expected[phase]:
        raise ValueError("m3_phase_budget_not_registered")
    return value


def require_live_configuration(authority):
    """Canonical host preflight; never access or print secret values here."""
    from src.runtime.starship_m3_live import contract

    if type(authority) is not dict or digest(authority) != digest(
        contract(authority.get("mode"), authority.get("case"))
    ):
        raise ValueError("m3_authority_not_registered")
    prefix = authority["prefix_authority"]
    for phase in ("ascent", "booster"):
        _prefix_limit(prefix, phase)
    if digest(prefix["m2_authority"]) != digest(authority["m2_authority"]):
        raise ValueError("m3_inner_authority_mismatch")
    require_m2_configuration(authority["m2_authority"])


class PrefixAgent:
    """Approved host provider choice; observations and frozen report, never plant truth."""

    def __init__(self, authority, phase, run_id, transport):
        if phase not in ("ascent", "booster"):
            raise ValueError("m3_prefix_phase_required")
        self.authority, self.phase, self.run_id = deepcopy(authority), phase, run_id
        self.transport, self.calls = transport, 0
        self.limit = _prefix_limit(authority, phase)

    def _context(self, request):
        if type(request) is not dict:
            raise ValueError("m3_request_object_required")
        authority, phase = self.authority, self.phase
        if phase == "ascent":
            envelope = authority["ascent_authority"]
            fields = {
                "schema",
                "request_id",
                "authority_sha256",
                "observation",
                "trigger",
                "deadline_s",
                "allowed_actions",
            }
            if (
                set(request) != fields
                or request["schema"] != "missionos.starship_ascent_decision.v1"
                or digest(envelope) != digest(ASCENT_CONFIG)
                or request["authority_sha256"] != digest(envelope)
                or type(request["request_id"]) is not str
                or re.fullmatch(re.escape(self.run_id) + r"-ascent-[0-3]", request["request_id"])
                is None
                or request["trigger"] not in ("initial_monitor", "phase_changed", "health_changed")
                or request["allowed_actions"] != envelope["actions"]
            ):
                raise ValueError("m3_ascent_request_binding")
            row = request["observation"]
            if (
                type(row) is not dict
                or set(row)
                != {
                    "time_s",
                    "phase",
                    "navigation",
                    "engine_available",
                    "deployment_withheld",
                    "sequencer_state",
                    "released_count",
                }
                or not _number(row["time_s"])
                or row["time_s"] < 0
                or row["phase"] not in ("stack_ascent", "ship_ascent")
                or type(row["navigation"]) is not dict
                or type(row["engine_available"]) is not list
                or not row["engine_available"]
                or not all(type(x) is bool for x in row["engine_available"])
                or type(row["deployment_withheld"]) is not bool
                or type(row["released_count"]) is not int
                or row["released_count"] != 0
                or row["sequencer_state"] is not None
                and type(row["sequencer_state"]) is not str
            ):
                raise ValueError("m3_ascent_observation")
            nav = row["navigation"]
            if (
                set(nav) != {"schema", "basis", "retained_count", "state"}
                or nav["schema"] != "missionos.ship_return_prediction_origin.v1"
                or nav["basis"] != "synthetic_navigation_estimate_v1"
                or type(nav["retained_count"]) is not int
                or nav["retained_count"] != 0
                or type(nav["state"]) is not dict
                or set(nav["state"]) != set(STATE_KEYS)
                or not _number(nav["state"]["time_s"])
                or nav["state"]["time_s"] != row["time_s"]
            ):
                raise ValueError("m3_ascent_navigation_scope")
            deadline = request["deadline_s"]
            maximum = envelope["decision_timeout_s"]
            instruction = (
                "Supervise observed ascent. Choose whether the later deployment may continue "
                "or should be withheld. Do not command engines, attitude, abort or booster. "
                "Respect current health and preserve normal flight when appropriate."
            )
        else:
            envelope = authority["booster_authority"]
            fields = {
                "schema",
                "request_id",
                "point",
                "observation",
                "allowed_actions",
                "envelope_sha256",
                "decision_deadline_s",
            }
            if (
                set(request) != fields
                or request["schema"] != "missionos.starship_director_request.v5"
                or request["envelope_sha256"] != digest(envelope)
                or request["request_id"] != self.run_id + "-booster:1"
                or request["point"] != "booster_selection"
            ):
                raise ValueError("m3_booster_request_binding")
            row = validate_observation(request["observation"])
            choices = request["allowed_actions"]
            if (
                row["phase"] != "booster_return"
                or type(choices) is not list
                or not choices
                or choices
                != [a for a in envelope["decision_points"]["booster_selection"] if a in choices]
            ):
                raise ValueError("m3_booster_choices")
            deadline, maximum = request["decision_deadline_s"], envelope["decision_expiry_s"]
            instruction = (
                "Supervise the booster's approved recovery objective using the observed "
                "readiness and certified choices. Choose only a supplied action. Never command engines, "
                "attitude or catch timing. A simulation forecast is not real flight certification."
            )
        if not _number(deadline) or not row["time_s"] < deadline <= row["time_s"] + maximum:
            raise ValueError("m3_request_deadline")
        public = {
            "scope": "bounded_" + phase + "_supervision",
            "observation": row,
            "allowed_actions": request["allowed_actions"],
            "decision_deadline_s": deadline,
        }
        if phase == "booster":
            if _deep_booster(authority, phase):
                report = authority["booster_recovery_report"]
                if digest(report) != authority["booster_recovery_report_sha256"]:
                    raise ValueError("m3_recovery_report_binding")
                public.update(recovery_report=deepcopy(report),
                    recovery_report_sha256=digest(report),
                    objectives_to_compare=["capture", "splashdown"],
                    capture_controller_connected=envelope["capture_controller_connected"])
            else:
                from .starship_booster_recovery_evidence import model_history
                public["recovery_history"] = model_history()
        return public, instruction

    def assess(self, request):
        public, instruction = self._context(request)
        if _deep_booster(self.authority, self.phase):
            return self._assess_booster(request, public)
        receipt = _receipt("live", "typesafe", public)
        response = {
            "request_id": request["request_id"],
            "request_sha256": digest(request),
            "action": None,
            "mode": "live",
            "jev_invocation": receipt,
            "llm_invocation": None,
            "model_inference_invoked": False,
        }
        if os.environ.get(MODE_ENV) != "live":
            raise ValueError("m3_live_configuration_changed")
        if self.calls >= self.limit:
            receipt["status"] = "phase_call_budget_exhausted"
            return response
        if self.transport.remaining() - APPLY_MARGIN_S < MIN_PROVIDER_S:
            receipt["status"] = "insufficient_deadline_headroom"
            return response
        self.calls += 1
        criteria = {
            a: "Choose " + a + " within the supplied observations and approved authority."
            for a in public["allowed_actions"]
        }
        payload = {
            "model": JEV_MODEL,
            "state": public,
            "questions": {
                "decision": {"type": "choice", "instructions": instruction, "criteria": criteria}
            },
        }

        def decode(value):
            if (
                type(value) is not dict
                or value.get("model") != JEV_MODEL
                or type(value.get("answers")) is not dict
                or set(value["answers"]) != {"decision"}
            ):
                raise ValueError("invalid_m3_provider_answer")
            answer = value["answers"]["decision"]
            if (
                type(answer) is not dict
                or set(answer) != {"type", "choice", "probabilities", "confidence"}
                or answer["type"] != "choice"
                or type(answer["choice"]) is not str
                or answer["choice"] not in criteria
                or type(answer["probabilities"]) is not dict
                or set(answer["probabilities"]) != set(criteria)
            ):
                raise ValueError("invalid_m3_provider_answer")
            values = [*answer["probabilities"].values(), answer["confidence"]]
            if any(not _number(x) or not 0 <= x <= 1 for x in values) or not math.isclose(
                sum(answer["probabilities"].values()), 1.0, abs_tol=0.001
            ):
                raise ValueError("invalid_m3_provider_distribution")
            return answer["choice"], value["model"]

        response["action"] = self.transport._invoke("typesafe", payload, receipt, decode)
        receipt.update(
            maximum_instance_calls=self.limit,
            reserved_call_slot=self.calls if receipt["call_attempted"] else None,
        )
        response["model_inference_invoked"] = receipt["model_inference_invoked"]
        return response


    def _assess_booster(self, request, public):
        """DeepSeek judges the recovery objective; the actor still checks execution."""
        from .space_ops_investigator import _deepseek_model, _json, canonical_bytes
        if os.environ.get(MODE_ENV) != "live":
            raise ValueError("m3_live_configuration_changed")
        model = _deepseek_model()
        receipt = _receipt("live", "deepseek", public)
        receipt.update(maximum_output_tokens=800, maximum_instance_calls=None,
            decoder="missionos.booster_answer.v2")
        response = {"request_id": request["request_id"], "request_sha256": digest(request),
            "action": None, "mode": "live", "jev_invocation": None,
            "llm_invocation": receipt, "model_inference_invoked": False,
            "recovery_report_sha256": public["recovery_report_sha256"],
            "decision_context": public, "decision_summary": None,
            "free_text_summary_verified": False}
        # The mailbox consumes this request identity once; no retry or persistent
        # DeepSeek call ceiling is introduced. The actual flight deadline applies.
        if self.transport.remaining() - APPLY_MARGIN_S < MIN_PROVIDER_S:
            receipt["status"] = "insufficient_deadline_headroom"
            return response
        payload = {"model": model, "stream": False, "thinking": {"type": "disabled"},
            "temperature": 0, "max_tokens": 800, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content":
                "You supervise this simulator mission's booster recovery. Compare tower capture "
                "and ocean splashdown using the approved historical report AND current observations. "
                "Choose a recovery objective; allowed_actions separately states what the executor "
                "can execute now. Do not command engines, attitude or arms. Zero demonstrated catches "
                "does not prove impossibility; nominal splashdown evidence is NOT perturbation qualification. "
                "Return exactly JSON action (capture or splashdown) and summary (brief external decision "
                "explanation, not hidden reasoning). Do not invent a demonstrated catch or current-state certification."},
                {"role": "user", "content": canonical_bytes(public).decode()}]}
        def reject(category):
            receipt["validation_failure_reason"] = category
            raise ValueError("invalid_booster_model_response")
        def decode(value):
            if (type(value) is not dict or value.get("model") != model
                    or type(value.get("choices")) is not list or len(value["choices"]) != 1):
                reject("model_or_choices_shape")
            choice = value["choices"][0]
            message = choice.get("message", {})
            if (choice.get("finish_reason") != "stop" or message.get("role") != "assistant"
                    or message.get("tool_calls") or message.get("function_call")):
                reject("completion_not_stop_or_tool_call")
            try:
                answer = _json(message["content"])
            except (ValueError, KeyError, TypeError):
                reject("answer_json")
            # Some valid provider answers include a descriptive type wrapper.
            # It is bounded metadata only: never an action, grant or instruction.
            if (type(answer) is not dict or set(answer) not in (
                    {"action", "summary"}, {"action", "summary", "type"})
                    or "type" in answer and (type(answer["type"]) is not str or not 1 <= len(answer["type"]) <= 64)
                    or answer["action"] not in public["objectives_to_compare"]
                    or type(answer["summary"]) is not str or not 1 <= len(answer["summary"]) <= 1600):
                reject("answer_shape_or_objective")
            receipt["provider_type_metadata_observed"] = "type" in answer
            return {k: answer[k] for k in ("action", "summary")}, model
        answer = self.transport._invoke("deepseek", payload, receipt, decode)
        self.calls += int(receipt["call_attempted"])
        if answer:
            response.update(action=answer["action"], decision_summary=answer["summary"])
        response["model_inference_invoked"] = receipt["model_inference_invoked"]
        return response


class PrefixHostAttempts(HostAttempts):
    """Reuse durable attempt events; bind their identities to the prefix grant."""

    def __init__(self, root, authority, run_id, phase, current):
        self.path = Path(root) / "host-attempts.json"
        self.current, self.phase = current, phase
        self.authority = {
            phase + "_authority": {
                "maximum_jev_calls": 0 if _deep_booster(authority, phase) else _prefix_limit(authority, phase),
                "maximum_llm_calls": None if _deep_booster(authority, phase) else 0,
            }
        }
        self.record = empty(authority, run_id, phase)
        self.record["schema"] = LEDGER_SCHEMA
        if self.path.exists():
            raise ValueError("host_attempt_ledger_already_exists")
        publish(self.path, self.record)


def serve_phase(root, authority, phase, run_id, child, sources_current, *, inflight_contract=None):
    if phase in ("deployment", "return"):
        return serve_m2_phase(
            root, authority["m2_authority"], phase, run_id, child, sources_current,
            **({"inflight_contract": inflight_contract} if phase == "return" and inflight_contract is not None else {})
        )
    if phase not in ("ascent", "booster"):
        raise ValueError("m3_phase_not_registered")
    root, seen, current = Path(root), set(), {}

    def active():
        return child.poll() is None and sources_current()

    def request_current():
        if not active() or not current:
            return False
        try:
            if digest(json.loads((root / "request.json").read_text())) != current["sha256"]:
                return False
            cancelled = root / "cancelled.json"
            if cancelled.exists():
                cancellation = json.loads(cancelled.read_text())
                if (
                    type(cancellation) is not dict
                    or cancellation.get("request_sha256") == current["sha256"]
                ):
                    return False
            return time.monotonic() < current["deadline"]
        except (OSError, ValueError, TypeError):
            return False

    ledger = PrefixHostAttempts(root, authority["prefix_authority"], run_id, phase, current)
    transport = DeadlineTransport(
        request_current,
        lambda: current.get("deadline", 0.0) - time.monotonic(),
        jev_only=not _deep_booster(authority["prefix_authority"], phase),
        audit=ledger.event,
    )
    agent = PrefixAgent(authority["prefix_authority"], phase, run_id, transport)
    while active():
        path = root / "request.json"
        if path.is_file() and not path.is_symlink():
            try:
                request = json.loads(path.read_text())
                # Validate before reserving identity or deriving wall deadlines.
                public, _ = agent._context(request)
                token = request["request_id"]
                if token in seen:
                    time.sleep(0.02)
                    continue
                seen.add(token)
                age = max(0.0, time.time() - path.stat().st_mtime)
                current.update(
                    request_id=token,
                    request_age_s=age,
                    sha256=digest(request),
                    deadline=time.monotonic()
                    + public["decision_deadline_s"]
                    - public["observation"]["time_s"]
                    - age,
                )
                response = agent.assess(request)
                if request_current():
                    publish(root / "response.json", response)
                    ledger.published()
            except (ValueError, KeyError, TypeError, OSError, OverflowError):
                pass  # Credential-free actor owns finite timeout and fallback.
        time.sleep(0.02)


def serve(mailbox, authority, run_id, child, sources_current, *, inflight_binding=None):
    require_live_configuration(authority)
    if authority["mode"] != "live":
        return
    from src.runtime.starship_m3_inflight import CASES, validate_binding
    from src.runtime.starship_m3_live import sources
    inflight_contract = None
    if authority["case"] in CASES:
        inflight_contract = validate_binding(inflight_binding, authority, run_id=run_id, sources=sources())
    elif inflight_binding is not None:
        raise ValueError("m3_unapproved_inflight_broker")
    root = Path(mailbox)
    if (
        root.is_symlink()
        or not root.is_dir()
        or any((root / p).is_symlink() or not (root / p).is_dir() for p in PHASES)
        or json.loads((root / "m3-authority.json").read_text())
        != {"run_id": run_id, "authority_sha256": digest(authority)}
        or json.loads((root / "authority.json").read_text())
        != {"run_id": run_id, "authority_sha256": digest(authority["m2_authority"])}
    ):
        raise ValueError("m3_mailbox_authority_binding")
    threads = [
        Thread(
            target=serve_phase,
            args=(root / phase, authority, phase, run_id, child, sources_current),
            kwargs={"inflight_contract": inflight_contract},
            daemon=True,
        )
        for phase in PHASES
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
