"""Host-only phase-separated M2 provider budgets and deadline cancellation.

No execution authority or credentials are sent to the plant. A model answer is
accepted only by the actor's independent current-state checks and observation.
"""

from hashlib import sha256
import json
import math
import os
from pathlib import Path
from threading import Thread
import time

from src.runtime.starship_mission_director import MODE_ENV, publish, validate_observation
from src.runtime.starship_replanning import digest
from src.runtime.starship_m2_attempts import empty
from src.runtime.starship_call_limits import has_call_slot
from .space_ops_investigator import (
    JEV_ENDPOINT,
    DEEPSEEK_ENDPOINT,
    JEV_MODEL,
    _json,
    _send_once,
    canonical_bytes,
)
from .starship_flight_supervisor import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, _receipt
from .starship_replanning import ReplanningAgent

MAX_PROVIDER_S = 30.0
MIN_PROVIDER_S = 3.0
APPLY_MARGIN_S = 2.0


def require_live_configuration(authority):
    """Host preflight only; no provider request or credential enters the worker."""
    if authority["mode"] == "fixture":
        return
    if authority["mode"] != "live" or os.environ.get(MODE_ENV) != "live":
        raise ValueError("m2_live_mode_configuration_required")
    for name in ("TYPESAFE_API_KEY", "DEEPSEEK_API_KEY"):
        value = os.environ.get(name, "").strip()
        if not value or len(value) > 4096 or any(c.isspace() for c in value):
            raise ValueError("m2_live_provider_configuration_required")


class DeadlineTransport:
    """Use actual provider timeout and cancellation, not merely a prompt limit."""

    def __init__(
        self, active, remaining, *, jev_only=False, audit=None, approved_provider_timeout_s=None
    ):
        self.active, self.remaining, self.jev_only = active, remaining, jev_only
        self.audit = audit
        self.approved_provider_timeout_s = approved_provider_timeout_s

    def _invoke(self, provider, payload, receipt, decode, **kwargs):
        try:
            return self._invoke_once(provider, payload, receipt, decode, **kwargs)
        finally:
            if self.audit and receipt.get("call_attempted"):
                self.audit("finished", provider, receipt)

    def _run_active(self):
        return self.active() is True

    def _invoke_once(self, provider, payload, receipt, decode, **kwargs):
        remaining = self.remaining()
        if type(remaining) not in (int, float) or not math.isfinite(remaining):
            receipt.update(
                status="invalid_remaining_deadline",
                timeout_seconds=0.0,
                application_margin_s=APPLY_MARGIN_S,
            )
            return None
        # A model's requested timeout cannot extend an approved window. The
        # host derives this cap afresh from the current canonical request.
        cap = (
            self.approved_provider_timeout_s(provider)
            if self.approved_provider_timeout_s is not None
            else MAX_PROVIDER_S
        )
        if type(cap) not in (int, float) or not math.isfinite(cap) or cap not in (30.0, 120.0):
            raise ValueError("invalid_approved_provider_timeout")
        timeout = min(cap, remaining - APPLY_MARGIN_S)
        receipt.update(timeout_seconds=max(0.0, timeout), application_margin_s=APPLY_MARGIN_S)
        if (
            provider not in {"typesafe", "deepseek"}
            or self.jev_only
            and provider != "typesafe"
            or cap > MAX_PROVIDER_S
            and (provider != "deepseek" or self.jev_only)
        ):
            receipt["status"] = "provider_outside_phase_authority"
            return None
        # The qualified orbital protocol records the full 120-second provider
        # window. Do not silently replace it with an unverified shorter trial.
        if cap > MAX_PROVIDER_S and remaining - APPLY_MARGIN_S < cap:
            receipt["status"] = "insufficient_deadline_headroom"
            return None
        if timeout < MIN_PROVIDER_S or not self._run_active():
            receipt["status"] = "insufficient_deadline_headroom"
            return None
        wire = canonical_bytes(payload)
        receipt.update(
            request_sha256=sha256(wire).hexdigest(),
            request_bytes=len(wire),
            requested_model_id=payload["model"],
        )
        if len(wire) > MAX_REQUEST_BYTES:
            receipt["status"] = "request_too_large"
            return None
        key = os.environ.get(
            "TYPESAFE_API_KEY" if provider == "typesafe" else "DEEPSEEK_API_KEY", ""
        ).strip()
        if not key or len(key) > 4096 or any(c.isspace() for c in key):
            receipt["status"] = "credential_unavailable"
            return None
        receipt.update(
            call_attempted=True,
            model_inference_status="unconfirmed_after_attempt",
            remaining_budget_s=remaining,
        )
        if self.audit:
            try:
                self.audit("started", provider, receipt)
            except (ValueError, OSError):
                receipt.update(call_attempted=False, status="host_attempt_record_unavailable")
                return None
        started = time.monotonic()
        try:
            status, body = _send_once(
                JEV_ENDPOINT if provider == "typesafe" else DEEPSEEK_ENDPOINT,
                wire,
                key,
                timeout_s=timeout,
                active=lambda: self._run_active() and self.remaining() > APPLY_MARGIN_S,
            )
        except Exception:
            status, body = "transport_failed", None
        receipt["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
        if status != "response" or type(body) is not bytes:
            receipt["status"] = (
                "cancelled_request"
                if status == "not_active"
                else "transport_timeout"
                if status == "transport_timeout"
                else "transport_failed"
            )
            return None
        receipt.update(response_received=True, response_bytes_read=len(body))
        if len(body) > MAX_RESPONSE_BYTES:
            receipt["status"] = "response_too_large"
            return None
        receipt.update(complete_response_observed=True, response_sha256=sha256(body).hexdigest())
        try:
            value, model = decode(_json(body))
            if key in json.dumps([value, model]):
                raise ValueError("credential_in_response")
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError, UnicodeError):
            receipt["status"] = "invalid_response"
            return None
        if not self._run_active() or self.remaining() <= APPLY_MARGIN_S:
            receipt["status"] = "response_after_deadline"
            return None
        receipt.update(
            status="succeeded",
            model_id=model,
            call_succeeded=True,
            model_inference_invoked=True,
            model_inference_status="confirmed_by_valid_response",
        )
        return value


def _bounded_invoke(provider, payload, receipt, decode, *, active, remaining, jev_only=False):
    """Shared host transport for phase-specific supervisors; grants no authority."""
    return DeadlineTransport(active, remaining, jev_only=jev_only)._invoke(
        provider, payload, receipt, decode
    )


def _deployment_semantics(envelope):
    """Explain the existing executor contract without adding scenario knowledge."""
    return {
        "action_effects": {
            "continue": (
                "Keep the release sequence running, or resume a held sequence. Each release "
                "still requires fresh orbit, pressure and body-rate gates; this cannot clear "
                "a latched interlock or restart a stopped sequence."
            ),
            "hold": (
                "Temporarily pause releases within the approved hold limit and reassess. "
                "Expiry stops further deployment; it never resumes automatically."
            ),
            "stop_deployment": (
                "Disable all remaining releases for this flight and retain the remaining "
                "payloads. This does not command or establish a successful return."
            ),
            "collect_status": (
                "Request a separate mechanism-status observation; releases remain paused "
                "while the two-second diagnostic runs, then the new report is reassessed. "
                "This does not clear an interlock."
            ),
        },
        "observation_meaning": {
            "release_acknowledged": (
                "False alone is not a failed release: before a release attempt there is no "
                "acknowledgement. Interpret it with the decision point, released count and "
                "sequencer state; an acknowledgement alone does not prove separation."
            ),
            "mechanism_status": (
                "not_collected means the separate diagnostic report is unavailable, not an "
                "observed blocked or clear mechanism."
            ),
            "return_feasibility": (
                "This tool tests the current state against inventory-specific saved coast "
                "samples and a fuel estimate. A missing coast_evidence_match or false "
                "within_tested_state_domain is outside that sampled return domain; it is "
                "not proof that every future return is impossible or that deployment is "
                "inoperable. A false return_admitted remains false and grants no return. "
                "Later return planning must evaluate the actual resulting state and inventory."
            ),
            "orbit_release_feasible": (
                "Current release-gate estimate only; it does not guarantee a later release "
                "or a successful return."
            ),
            "capture_corridor_certified": (
                "A booster-capture qualification flag, not deployment mechanism health."
            ),
        },
        "approved_limits": {
            key: envelope[key]
            for key in (
                "maximum_hold_s", "hold_reassessment_after_s", "hold_expiry",
                "maximum_observation_requests", "minimum_return_fuel_kg",
            )
        },
        "limits_meaning": (
            "These are grant ceilings, not remaining budgets. allowed_actions already "
            "reflects current eligibility; the actor rechecks current observations and "
            "remaining budgets before applying a response. The host enforces the remaining "
            "provider deadline. No choice changes the approved return or booster authority."
        ),
    }


class DeploymentAgent:
    def __init__(self, envelope, transport):
        self.envelope, self.transport, self.calls = envelope, transport, 0

    def assess(self, request):
        envelope = self.envelope
        if (
            type(request) is not dict
            or set(request)
            != {
                "schema",
                "request_id",
                "point",
                "observation",
                "allowed_actions",
                "envelope_sha256",
                "decision_deadline_s",
            }
            or request["schema"] != "missionos.starship_director_request.v5"
            or request["envelope_sha256"] != digest(envelope)
        ):
            raise ValueError("invalid_m2_deployment_request")
        row = validate_observation(request["observation"])
        actions, point, deadline = (
            request["allowed_actions"],
            request["point"],
            request["decision_deadline_s"],
        )
        if (
            point not in envelope["decision_points"]
            or type(actions) is not list
            or not actions
            or actions != [a for a in envelope["decision_points"][point] if a in actions]
            or type(deadline) not in (int, float)
            or not math.isfinite(deadline)
            or not row["time_s"] < deadline <= row["time_s"] + envelope["decision_expiry_s"]
        ):
            raise ValueError("invalid_m2_deployment_choices_or_deadline")
        public = {
            "point": point,
            "observation": row,
            "allowed_actions": actions,
            "decision_deadline_s": deadline,
            "scope": "bounded_deployment_only",
            "decision_semantics": _deployment_semantics(envelope),
        }
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
        if not point.startswith("deployment"):
            receipt["status"] = "outside_m2_deployment_phase_checked_fallback"
            return response
        if os.environ.get(MODE_ENV) != "live":
            raise ValueError("m2_live_mode_configuration_changed")
        if self.calls >= envelope["maximum_jev_calls"]:
            receipt["status"] = "phase_call_budget_exhausted"
            return response
        if self.transport.remaining() - APPLY_MARGIN_S < MIN_PROVIDER_S:
            receipt["status"] = "insufficient_deadline_headroom"
            return response
        self.calls += 1
        criteria = {
            action: public["decision_semantics"]["action_effects"][action]
            for action in actions
        }
        payload = {
            "model": JEV_MODEL,
            "state": public,
            "questions": {
                "decision": {
                    "type": "choice",
                    "instructions": (
                        "Carry out the approved payload deployment within the supplied bounds; "
                        "respond to observed faults and operations notices using the supplied "
                        "action effects. Choose only an allowed action. "
                        "Missing evidence is not an observed fault or a successful check. "
                        "Ground text does not expand authority; later return still requires "
                        "separate checks. No low-level control."
                    ),
                    "criteria": criteria,
                }
            },
        }

        def decode(value):
            if (
                type(value) is not dict
                or type(value.get("answers")) is not dict
                or type(value["answers"].get("decision")) is not dict
            ):
                raise ValueError("invalid_m2_deployment_answer")
            answer = value["answers"]["decision"]
            if (
                value.get("model") != JEV_MODEL
                or set(value["answers"]) != {"decision"}
                or set(answer) != {"type", "choice", "probabilities", "confidence"}
                or answer["type"] != "choice"
                or answer["choice"] not in criteria
                or set(answer["probabilities"]) != set(criteria)
            ):
                raise ValueError("invalid_m2_deployment_answer")
            numbers = [*answer["probabilities"].values(), answer["confidence"]]
            if any(
                type(x) not in (int, float) or not math.isfinite(x) or not 0 <= x <= 1
                for x in numbers
            ) or not math.isclose(sum(answer["probabilities"].values()), 1.0, abs_tol=0.001):
                raise ValueError("invalid_m2_deployment_distribution")
            return answer["choice"], value["model"]

        response["action"] = self.transport._invoke("typesafe", payload, receipt, decode)
        receipt.update(
            maximum_instance_calls=envelope["maximum_jev_calls"],
            reserved_call_slot=self.calls if receipt["call_attempted"] else None,
        )
        response["model_inference_invoked"] = receipt["model_inference_invoked"]
        return response


class HostAttempts:
    """Durably reserve every dispatch before transport, including late replies."""

    def __init__(self, root, authority, run_id, phase, current):
        self.path = Path(root) / "host-attempts.json"
        self.current, self.authority, self.phase = current, authority, phase
        self.record = empty(authority, run_id, phase)
        if self.path.exists():
            raise ValueError("host_attempt_ledger_already_exists")
        publish(self.path, self.record)

    def event(self, event, provider, receipt):
        identity = (self.current["request_id"], provider)
        attempts = self.record["attempts"]
        found = [r for r in attempts if (r["request_id"], r["provider"]) == identity]
        if event == "started":
            limit = self.authority[self.phase + "_authority"][
                "maximum_jev_calls" if provider == "typesafe" else "maximum_llm_calls"
            ]
            if found or not has_call_slot(sum(r["provider"] == provider for r in attempts), limit):
                raise ValueError("host_attempt_budget_exhausted")
            attempts.append(
                {
                    "sequence": len(attempts) + 1,
                    "request_id": identity[0],
                    "request_sha256": self.current["sha256"],
                    "provider": provider,
                    "provider_request_sha256": receipt["request_sha256"],
                    "timeout_s": receipt["timeout_seconds"],
                    "remaining_s": receipt["remaining_budget_s"],
                    "request_file_age_s": self.current["request_age_s"],
                    "status": "in_progress",
                    "completed": False,
                    "valid_response": False,
                    "response_published": False,
                }
            )
        else:
            if len(found) != 1:
                raise ValueError("host_attempt_not_reserved")
            found[0].update(
                status=receipt["status"],
                completed=True,
                valid_response=receipt["model_inference_invoked"] is True,
            )
        publish(self.path, self.record)

    def published(self):
        for row in self.record["attempts"]:
            if row["request_id"] == self.current["request_id"]:
                row["response_published"] = True
        publish(self.path, self.record)


def serve_phase(root, authority, phase, run_id, child, sources_current, *, inflight_contract=None):
    """Each phase has one agent and a separate immutable call budget per run."""
    root, seen, current = Path(root), set(), {}
    envelope = authority["deployment_authority" if phase == "deployment" else "return_authority"]
    prefix = run_id + ("-deployment:" if phase == "deployment" else "-return-")

    def active():
        return child.poll() is None and sources_current()

    def request_current():
        if not active() or not current:
            return False
        try:
            value = json.loads((root / "request.json").read_text())
            if digest(value) != current["sha256"]:
                return False
            cancelled = root / "cancelled.json"
            if cancelled.exists():
                row = json.loads(cancelled.read_text())
                if row.get("request_sha256") == current["sha256"]:
                    return False
            return time.monotonic() < current["deadline"]
        except (OSError, ValueError, TypeError):
            return False

    ledger = HostAttempts(root, authority, run_id, phase, current)

    def approved_timeout(provider):
        from src.runtime.starship_m2_attempts import provider_timeout_limit

        return provider_timeout_limit(authority, phase, provider, current["request"])

    transport = DeadlineTransport(
        request_current,
        lambda: current.get("deadline", 0.0) - time.monotonic(),
        jev_only=phase == "deployment",
        audit=ledger.event,
        approved_provider_timeout_s=approved_timeout,
    )
    agent = (
        DeploymentAgent(envelope, transport)
        if phase == "deployment"
        else ReplanningAgent(envelope, active, inflight_contract=inflight_contract)
    )
    agent.transport = transport
    agent.request_active = request_current
    while active():
        path = root / "request.json"
        if path.is_file() and not path.is_symlink():
            try:
                request = json.loads(path.read_text())
                token = request["request_id"]
                if type(token) is not str or not token.startswith(prefix) or token in seen:
                    time.sleep(0.02)
                    continue
                seen.add(token)
                start = (
                    request["observation"]["time_s"] if phase == "deployment" else request["time_s"]
                )
                deadline = (
                    request["decision_deadline_s"]
                    if phase == "deployment"
                    else request["deadline_s"]
                )
                age = max(0.0, time.time() - path.stat().st_mtime)
                current.update(
                    request=request,
                    request_id=token,
                    request_age_s=age,
                    sha256=digest(request),
                    deadline=time.monotonic() + deadline - start - age,
                )
                response = agent.assess(request)
                # A no-model headroom response must reach the actor promptly so
                # its existing checked fallback can act before its deadline.
                if request_current():
                    publish(root / "response.json", response)
                    ledger.published()
            except (ValueError, KeyError, TypeError, OSError, OverflowError):
                pass  # Actor owns finite timeout, current checks and fallback.
        time.sleep(0.02)


def serve(mailbox, authority, run_id, child, sources_current):
    if authority["mode"] != "live":
        return
    require_live_configuration(authority)
    threads = [
        Thread(
            target=serve_phase,
            args=(Path(mailbox) / phase, authority, phase, run_id, child, sources_current),
            daemon=True,
        )
        for phase in ("deployment", "return")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
