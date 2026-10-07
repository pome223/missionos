"""Host-only, routing-only Jev shadow for the finite synthetic dispenser.

The returned route describes where a judgment *would* go. It never selects the
executor's policy, invokes a target role, or changes a simulator decision. Inputs
are limited to the same public history and budget available to the baseline.
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import math
import os
from queue import Empty, Queue
import re
from threading import Lock, Thread
import time
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from src.runtime.starship_dispenser_experiment import (
    PublicBudget,
    PublicHistory,
    RetryObservation,
    SensorObservation,
    default_experiment_config,
)

JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
MAX_CALLS = 22
MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
TIMEOUT_SECONDS = 15
ROUTE_TARGETS = {
    "bounded": "history_rule",
    "need_observation": "observation_collector",
    "human_review": "human",
    "deep_reasoning": "deepseek_proposal_only",
}


def canonical_request_bytes(value: dict) -> bytes:
    """The exact JSON encoding used on the wire and for request SHA-256."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _exact_keys(value, keys: set[str]) -> None:
    if type(value) is not dict or set(value) != keys:
        raise ValueError("invalid_public_input")


def _integer(value, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid_public_input")


def _public_inputs(history: dict, budget: dict) -> tuple[dict, dict]:
    """Validate complete public endpoint history; never inspect a hidden world."""
    _exact_keys(history, {"observations", "retry_results"})
    _exact_keys(budget, set(PublicBudget.__dataclass_fields__))
    config = default_experiment_config()
    for name in (
        "deadline_ticks",
        "max_attempts",
        "payload_count",
        "retry_ticks",
        "wait_ticks",
        "tick_s",
    ):
        _integer(budget[name], config[name], config[name])
    for name, maximum in (("time_ticks", 12), ("attempts", 5), ("released", 3)):
        _integer(budget[name], 0, maximum)
    observations = history["observations"]
    retries = history["retry_results"]
    if type(observations) not in (list, tuple) or not 1 <= len(observations) <= 13:
        raise ValueError("invalid_public_input")
    if type(retries) not in (list, tuple) or len(retries) > 5:
        raise ValueError("invalid_public_input")
    typed_observations = []
    previous = -1
    for value in observations:
        _exact_keys(value, {"time_ticks", "healthy"})
        _integer(value["time_ticks"], 0, budget["time_ticks"])
        if type(value["healthy"]) is not bool or value["time_ticks"] <= previous:
            raise ValueError("invalid_public_input")
        previous = value["time_ticks"]
        typed_observations.append(SensorObservation(**value))
    if typed_observations[0].time_ticks != 0 or previous != budget["time_ticks"]:
        raise ValueError("invalid_public_input")
    typed_retries = []
    previous_end = 0
    for value in retries:
        _exact_keys(value, {"start_ticks", "end_ticks", "success"})
        _integer(value["start_ticks"], previous_end, budget["time_ticks"])
        _integer(value["end_ticks"], 0, budget["time_ticks"])
        if type(value["success"]) is not bool or value["end_ticks"] - value["start_ticks"] != 2:
            raise ValueError("invalid_public_input")
        previous_end = value["end_ticks"]
        typed_retries.append(RetryObservation(**value))
    retry_intervals = {(row.start_ticks, row.end_ticks) for row in typed_retries}
    observed_retry_intervals = set()
    for start, end in zip(typed_observations, typed_observations[1:]):
        interval = (start.time_ticks, end.time_ticks)
        if end.time_ticks - start.time_ticks == 2:
            observed_retry_intervals.add(interval)
        elif end.time_ticks - start.time_ticks != 1:
            raise ValueError("invalid_public_input")
    if observed_retry_intervals != retry_intervals:
        raise ValueError("invalid_public_input")
    if budget["attempts"] != len(typed_retries) or budget["released"] != sum(
        row.success for row in typed_retries
    ):
        raise ValueError("invalid_public_input")
    typed_history = PublicHistory(tuple(typed_observations), tuple(typed_retries))
    typed_budget = PublicBudget(**budget)
    # Fresh JSON values avoid input mutation and make tuple/list callers identical.
    return json.loads(canonical_request_bytes(asdict(typed_history))), asdict(typed_budget)


def build_starship_jev_request(public_history: dict, public_budget: dict) -> dict:
    """Build a safe, reproducible request, raising ValueError for invalid input.

    The law is a whitelist from the frozen public configuration, never the full
    experiment configuration (which also includes private run identifiers).
    """
    history, budget = _public_inputs(public_history, public_budget)
    config = default_experiment_config()
    law_keys = (
        "tick_s",
        "deadline_ticks",
        "wait_ticks",
        "retry_ticks",
        "max_attempts",
        "payload_count",
        "initial_release_count",
        "recovery_times_s",
        "prior_weights",
        "sensor_denominator",
        "healthy_likelihood_ready",
        "healthy_likelihood_jammed",
        "reward_per_payload",
        "time_cost_numerator",
        "time_cost_denominator",
        "retry_cost",
        "observation_schedule",
        "retry_success_rule",
    )
    return {
        "model": JEV_MODEL,
        "state": {
            "schema": "missionos.starship_jev_public_state.v1",
            "public_history": history,
            "public_budget": budget,
            "public_model_law": {key: config[key] for key in law_keys},
            "context": (
                "Finite synthetic dispenser, not a spacecraft. Recovery hypotheses and prior "
                "weights describe the known distribution, not the hidden realized recovery time. "
                "The latent recovery time is intentionally unknown and healthy readings are noisy. "
                "These are normal partial observability, not missing required observations. "
                "A failed retry refers to its start; a healthy reading at its end can coexist. "
                "Sensors are independent conditional on hidden recovery and absolute tick. "
                "Only initial and completed action endpoint readings are observed. "
                "Objective: 10 * released - 0.15 * elapsed_seconds - attempts. "
                "No actual recovery time, future observations, outcome, selected action, or hidden "
                "world identifier is supplied. Classify judgment needs only; do not choose actions."
            ),
        },
        "questions": {
            "assessment_route": {
                "type": "choice",
                "instructions": (
                    "Classify what is needed to judge this situation using only supplied facts. "
                    "More reasoning cannot supply a missing required observation. "
                    "An unknown latent state and ordinary sensor noise are not missing facts. "
                    "A separate downstream approval alone does not require human_review. "
                    "This is a would-route shadow: no selected role will be invoked, and the "
                    "classification grants no approval, dispatch, or execution authority."
                ),
                "criteria": {
                    "bounded": "Supplied evidence supports a straightforward bounded judgment.",
                    "deep_reasoning": "Evidence is available but requires resolving complex tradeoffs or conflicts.",
                    "need_observation": "A required observation is absent; obtain evidence before judging.",
                    "human_review": "An operator must resolve ambiguous objectives or policy.",
                },
            },
        },
    }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _provider_opener():
    return build_opener(ProxyHandler({}), _NoRedirect())


def _bounded_request(wire: bytes, key: str) -> tuple[str, bytes | None]:
    """Bound response acceptance even if a server trickles bytes indefinitely.

    An in-flight request cannot be recalled after the deadline. Its daemon can
    only finish that single bounded read; it cannot publish a late accepted
    response, retry, or mutate invocation evidence.
    """
    completed = Queue(maxsize=1)
    deadline = time.monotonic() + TIMEOUT_SECONDS

    def request_once():
        try:
            request = Request(
                JEV_ENDPOINT,
                data=wire,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {key}",
                },
            )
            opener = _provider_opener()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            with opener.open(request, timeout=remaining) as response:
                if time.monotonic() >= deadline:
                    return
                body = response.read(MAX_RESPONSE_BYTES + 1)
            if time.monotonic() < deadline:
                completed.put_nowait(("response", body))
        except Exception:
            # Raw exception messages and provider bodies are not diagnostic data.
            if time.monotonic() < deadline:
                completed.put_nowait(("transport_failed", None))

    Thread(target=request_once, daemon=True, name="starship-jev-shadow-request").start()
    try:
        response = completed.get(timeout=max(0, deadline - time.monotonic()))
    except Empty:
        return "transport_timeout", None
    if time.monotonic() >= deadline:
        return "transport_timeout", None
    return response


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid_response")
        result[key] = value
    return result


def _validated_response(body: bytes) -> tuple[str, str, dict, float]:
    value = json.loads(body, object_pairs_hook=_unique_object)
    if type(value) is not dict or not isinstance(value.get("model"), str):
        raise ValueError("invalid_response")
    model = value["model"]
    if not re.fullmatch(r"jev-[A-Za-z0-9_.-]{1,64}", model):
        raise ValueError("invalid_response")
    answers = value.get("answers")
    _exact_keys(answers, {"assessment_route"})
    answer = answers["assessment_route"]
    _exact_keys(answer, {"type", "choice", "probabilities", "confidence"})
    if answer["type"] != "choice" or type(answer["choice"]) is not str:
        raise ValueError("invalid_response")
    if answer["choice"] not in ROUTE_TARGETS:
        raise ValueError("invalid_response")
    probabilities = answer["probabilities"]
    _exact_keys(probabilities, set(ROUTE_TARGETS))
    values = list(probabilities.values()) + [answer["confidence"]]
    if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in values):
        raise ValueError("invalid_response")
    if not math.isclose(sum(probabilities.values()), 1.0, rel_tol=0, abs_tol=0.01):
        raise ValueError("invalid_response")
    return answer["choice"], model, probabilities, float(answer["confidence"])


class StarshipJevRouter:
    """An instance-scoped, at-most-22-call diagnostic; no cache and no retry."""

    def __init__(self, mode: str = "off", max_calls: int = MAX_CALLS):
        if type(mode) is not str or mode not in ("off", "fixture", "live"):
            raise ValueError("invalid_router_mode")
        if type(max_calls) is not int or not 0 <= max_calls <= MAX_CALLS:
            raise ValueError("invalid_router_call_limit")
        self._mode = mode
        self._max_calls = max_calls
        self._calls = 0
        self._lock = Lock()

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def max_calls(self) -> int:
        return self._max_calls

    @property
    def calls_reserved(self) -> int:
        with self._lock:
            return self._calls

    def route(self, public_history: dict, public_budget: dict) -> dict:
        invocation = {
            "schema_version": "runtime_invocation_evidence.v1",
            "invocation_kind": "decision_api" if self._mode == "live" else "not_invoked",
            "mode": self._mode,
            "provider": "typesafe" if self._mode == "live" else None,
            "requested_model_id": JEV_MODEL if self._mode == "live" else None,
            "model_id": None,
            "status": "not_invoked",
            "call_attempted": False,
            "call_succeeded": False,
            "response_received": False,
            "complete_response_observed": False,
            "model_inference_invoked": False,
            "model_inference_status": "not_invoked",
            "latency_ms": 0.0,
            "public_input_sha256": None,
            "request_sha256": None,
            "response_sha256": None,
            "request_hash_encoding": "canonical_json_utf8",
            "response_hash_encoding": "raw_bytes",
            "request_bytes": 0,
            "response_bytes_read": 0,
            "maximum_calls": self._max_calls,
            "reserved_call_slot": None,
            "maximum_request_bytes": MAX_REQUEST_BYTES,
            "maximum_response_bytes": MAX_RESPONSE_BYTES,
            "timeout_seconds": TIMEOUT_SECONDS,
            "retries": 0,
            "redirects": 0,
            "probabilities": None,
            "confidence": None,
            "confidence_calibrated": False,
            "raw_prompt_recorded": False,
            "raw_response_recorded": False,
            "would_route_only": True,
            "approval_granted": False,
            "dispatch": False,
            "executor_influenced": False,
            "model_value_claim": False,
        }

        def result(status: str, route: str | None = None) -> dict:
            invocation["status"] = status
            return {
                "route": route,
                "target_role": ROUTE_TARGETS.get(route),
                "invocation": invocation,
            }

        try:
            payload = build_starship_jev_request(public_history, public_budget)
        except (TypeError, ValueError, KeyError, OverflowError):
            return result("invalid_input")
        wire = canonical_request_bytes(payload)
        state = payload["state"]
        invocation["public_input_sha256"] = _hash(
            canonical_request_bytes(
                {
                    "public_history": state["public_history"],
                    "public_budget": state["public_budget"],
                }
            )
        )
        invocation["request_sha256"] = _hash(wire)
        invocation["request_bytes"] = len(wire)
        if len(wire) > MAX_REQUEST_BYTES:
            return result("request_too_large")
        if self._mode == "off":
            return result("disabled")
        if self._mode == "fixture":
            invocation["invocation_kind"] = "deterministic_fixture"
            return result("fixture_only", "bounded")
        if os.environ.get("MISSIONOS_STARSHIP_JEV_SHADOW_MODE") != "live":
            return result("live_opt_in_required")
        key = os.environ.get("TYPESAFE_API_KEY", "").strip()
        if not key or len(key) > 4096 or any(char.isspace() for char in key):
            return result("credential_unavailable")
        with self._lock:
            if self._calls >= self._max_calls:
                return result("budget_exhausted")
            self._calls += 1
            invocation["reserved_call_slot"] = self._calls
        invocation["call_attempted"] = True
        invocation["model_inference_status"] = "unconfirmed_after_attempt"
        started = time.monotonic()
        try:
            status, body = _bounded_request(wire, key)
            if status != "response":
                return result(status)
            invocation["response_received"] = True
            invocation["response_bytes_read"] = len(body)
        except Exception:
            # Never serialize HTTP bodies, exception messages, URLs, or credentials.
            return result("transport_failed")
        finally:
            invocation["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
        if len(body) > MAX_RESPONSE_BYTES:
            return result("response_too_large")
        invocation["complete_response_observed"] = True
        invocation["response_sha256"] = _hash(body)
        try:
            route, model, probabilities, confidence = _validated_response(body)
        except (TypeError, ValueError, KeyError, OverflowError, RecursionError):
            return result("invalid_response")
        invocation.update(
            {
                "model_id": model,
                "probabilities": probabilities,
                "confidence": confidence,
                "call_succeeded": True,
                "model_inference_invoked": True,
                "model_inference_status": "confirmed_by_valid_response",
            }
        )
        return result("succeeded", route)
