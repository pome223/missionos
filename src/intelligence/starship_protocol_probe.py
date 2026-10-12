"""Offline replay preparation. No provider, simulator, or command dispatch path.

Capture the production request builder behind a transport with no network
method, then derive a separately hashed ID-reference protocol. This does not
change the frozen flight contract or count as a live model attempt.
"""

from copy import deepcopy
from hashlib import sha256
import os

from . import starship_replanning as host
from .space_ops_investigator import canonical_bytes
from .starship_flight_supervisor import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES
from .starship_reference_protocol import (
    policy_for, build_payload as _build_reference_payload,
    audit_response as _audit_reference_response,
)
from src.runtime.starship_reference_ids import catalog_for
from src.runtime.starship_replanning import MODE_ENV, digest

SCHEMA = "missionos.starship_protocol_probe.v1"
SETTINGS = (("ids_low_4096", "low", 4096), ("ids_high_8192", "high", 8192))


class _CaptureOnly:
    def __init__(self):
        self.payload = None

    def _invoke(self, provider, payload, _receipt, _decode):
        if provider != "deepseek" or self.payload is not None:
            raise ValueError("probe_requires_one_direct_deepseek_request")
        self.payload = deepcopy(payload)
        return None


def reconstruct(study, decision_index):
    """Rebuild one request using only its decision-time input and earlier slots.

    The saved receipt's final wire hash, rather than a similar semantic input,
    is the acceptance condition. A changed builder or ambiguous history fails.
    No later observation, flight outcome, or model answer is put in the payload.
    This single-process offline helper temporarily sets only the mode flag.
    """
    if type(decision_index) is not int or not 0 <= decision_index < len(study["decisions"]):
        raise ValueError("invalid_probe_decision_index")
    selected = study["decisions"][decision_index]
    request, envelope = deepcopy(selected["request"]), deepcopy(study["envelope"])
    receipt = selected["response"]["llm_invocation"]
    agent = host.ReplanningAgent(envelope, lambda: False)
    agent.transport = capture = _CaptureOnly()
    agent.calls = decision_index
    for previous in study["decisions"][:decision_index]:
        route = (previous.get("response") or {}).get("routing") or {}
        if route.get("route") in ("prepared_uncertain_comparison", "jev_escalation"):
            agent.llm_calls += 1
        if route.get("route") == "prepared_uncertain_comparison":
            agent.reasoned_generations.add(route["generation_sha256"])
    original_mode = os.environ.get(MODE_ENV)
    try:
        os.environ[MODE_ENV] = "live"
        agent.assess(request)
    finally:
        if original_mode is None:
            os.environ.pop(MODE_ENV, None)
        else:
            os.environ[MODE_ENV] = original_mode
    if capture.payload is None:
        raise ValueError("no_deepseek_request_captured")
    wire = canonical_bytes(capture.payload)
    if sha256(wire).hexdigest() != receipt["request_sha256"] or len(wire) != receipt["request_bytes"]:
        raise ValueError("original_provider_request_mismatch")
    if selected["response"]["request_sha256"] != digest(request):
        raise ValueError("original_decision_request_mismatch")
    return request, capture.payload


def build_payload(request, original_payload, effort, maximum_output_tokens):
    """Legacy fixed probe settings; shared construction preserves exact wire bytes."""
    for name, registered_effort, cap in SETTINGS:
        if (effort, maximum_output_tokens) == (registered_effort, cap):
            return _build_reference_payload(request, original_payload, policy=policy_for(name))
    raise ValueError("unregistered_probe_setting")


def prepare(study, decision_index):
    request, original = reconstruct(study, decision_index)
    variants = []
    for name, effort, cap in SETTINGS:
        payload = build_payload(request, original, effort, cap)
        variants.append({
            "setting_id": name, "payload": payload,
            "provider_request_sha256": digest(payload),
            "provider_request_bytes": len(canonical_bytes(payload)),
            "planned_attempts": 2,
        })
    return {
        "schema": SCHEMA, "scope": "offline_protocol_preparation_only",
        "request": request, "decision_request_sha256": digest(request),
        "envelope_sha256": digest(study["envelope"]),
        "source_manifest_sha256": digest(study.get("source_sha256", {})),
        "original_payload": original, "original_provider_request_sha256": digest(original),
        "original_provider_request_bytes": len(canonical_bytes(original)),
        "original_wire_verified": True,
        "catalog": catalog_for(request), "variants": variants,
        "proposed_campaign": {
            "status": "prepared_not_executed", "execution_authorized": False,
            "maximum_attempts": 4, "maximum_generated_tokens": 24576,
            "transport_timeout_s": 30, "decision_deadline_s": 35,
            "maximum_request_bytes": MAX_REQUEST_BYTES,
            "maximum_response_bytes": MAX_RESPONSE_BYTES,
            "retries_per_attempt": 0,
            "order": [v["setting_id"] for v in variants for _ in range(2)],
            "readiness_rule": "both fixed attempts valid within limits; not a reliability estimate",
            "selection_rule": "first passing setting in registered order, no action preference",
            "if_ready": "one separately bounded flight after integration and approval",
            "if_none_ready": "stop tuning; reserve DeepSeek for decisions with more time",
        },
        "provider_calls": 0, "flight_runs": 0, "dispatch": False,
        "held_out": False, "model_value_claim": False,
    }


def audit_response(request, payload, body, elapsed_s, *, expected_payload_sha256):
    """Legacy 35-second audit; later protocols require a separate caller policy."""
    policy = {}
    if type(payload) is dict:
        for name, effort, cap in SETTINGS:
            if (payload.get("reasoning_effort"), payload.get("max_tokens")) == (effort, cap):
                policy = policy_for(name)
                break
    return _audit_reference_response(
        request, payload, body, elapsed_s,
        expected_payload_sha256=expected_payload_sha256, policy=policy,
        secret_values=tuple(os.environ.get(name, "").strip()
                            for name in ("DEEPSEEK_API_KEY", "TYPESAFE_API_KEY")),
    )
