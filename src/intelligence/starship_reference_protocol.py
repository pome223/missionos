"""Pure request construction and response checks for registered reference protocols.

Policy selection does not authorize provider IO or flight operations. The caller
owns approval, durable budgets, the request deadline and result persistence.
Legacy probe wrappers retain their original wire bytes and 35-second deadline.
"""

from copy import deepcopy
from hashlib import sha256
import math

from .space_ops_investigator import canonical_bytes, _json
from src.runtime.starship_reference_ids import catalog_for, verify_reference_ids
from src.runtime.starship_replanning import digest

MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
_REGISTERED = (
    ("ids_low_4096", "low", 4096, 30, 35),
    ("ids_high_8192", "high", 8192, 30, 35),
    ("ids_orbital_low_16384", "low", 16384, 120, 150),
)


def policy_for(setting_id):
    """Return a fresh registered policy; this is configuration, not approval."""
    for name, effort, cap, transport, decision in _REGISTERED:
        if type(setting_id) is str and setting_id == name:
            return {"setting_id": name, "reasoning_effort": effort,
                    "maximum_output_tokens": cap, "transport_timeout_s": transport,
                    "decision_timeout_s": decision}
    raise ValueError("unregistered_probe_setting")


def _validated_policy(policy):
    if type(policy) is not dict:
        raise ValueError("unregistered_probe_setting")
    expected = policy_for(policy.get("setting_id"))
    try:
        same = canonical_bytes(policy) == canonical_bytes(expected)
    except (ValueError, TypeError, OverflowError, RecursionError):
        same = False
    if not same:
        raise ValueError("unregistered_probe_setting")
    return expected


def build_payload(request, original_payload, *, policy):
    """A new protocol, never represented as the unchanged original wire input."""
    policy = _validated_policy(policy)
    effort = policy["reasoning_effort"]
    maximum_output_tokens = policy["maximum_output_tokens"]
    payload = deepcopy(original_payload)
    public = _json(payload["messages"][1]["content"])
    public.pop("numeric_evidence_contract", None)
    catalog = catalog_for(request)
    # Values already appear in comparison_material. Only add the ID-to-path map;
    # the full value/unit catalog stays bound in the offline artifact.
    public["reference_ids"] = {}
    for entry in catalog["entries"]:
        public["reference_ids"].setdefault(entry["candidate_id"], {})[entry["field_path"]] = entry["reference_id"]
    payload["messages"][0]["content"] = public["objective"] + (
        " Return JSON with exactly action, compared_candidate_ids, summary, reference_ids. "
        "Choose action only from allowed_actions. Compare all eligible prepared candidates. "
        "summary is a brief operational explanation (1-600 characters), not a reasoning transcript. "
        "Cite 1-16 unique supplied reference_ids, at least one per compared candidate. "
        "These IDs point to the numerical inputs you used. Do not rewrite values, units, "
        "or hashes. Do not invent IDs or cite unavailable/future evidence."
    )
    payload["messages"][1]["content"] = canonical_bytes(public).decode()
    payload.update(reasoning_effort=effort, max_tokens=maximum_output_tokens)
    if len(canonical_bytes(payload)) > MAX_REQUEST_BYTES:
        raise ValueError("probe_request_too_large")
    return payload



def audit_response(request, payload, body, elapsed_s, *, expected_payload_sha256, policy, secret_values=()):
    """Audit an externally supplied response; never send requests or repair JSON.

    expected_payload_sha256 must come from the registered plan, not the reply.
    Resolved numbers are host values. Neither the prose nor reasoning is proven
    true by reference resolution. Do not save raw response/reasoning in results.
    """
    result = {"schema": "missionos.starship_protocol_response_audit.v1",
              "status": "invalid", "errors": [], "reference_audit": None,
              "provider_usage": {}, "decision": None,
              "dispatch": False, "free_prose_verified": False}

    def fail(reason):
        result["errors"].append(reason)
        return result

    try:
        policy = _validated_policy(policy)
    except ValueError:
        return fail("unregistered_probe_setting")
    try:
        timely = (type(elapsed_s) in (int, float) and math.isfinite(elapsed_s)
                  and 0 <= elapsed_s <= policy["decision_timeout_s"])
    except OverflowError:
        timely = False
    if not timely:
        return fail("decision_deadline")
    if type(body) is not bytes or len(body) > MAX_RESPONSE_BYTES:
        return fail("response_size")
    result.update(response_sha256=sha256(body).hexdigest(), latency_s=elapsed_s)
    try:
        if (type(expected_payload_sha256) is not str
                or digest(payload) != expected_payload_sha256
                or len(canonical_bytes(payload)) > MAX_REQUEST_BYTES):
            return fail("provider_request_mismatch")
        public = _json(payload["messages"][1]["content"])
        for key in ("time_s", "stage", "notice", "resources", "candidates", "selected", "allowed_actions",
                    "trigger_event_ids", "comparison_context", "planning_window"):
            if public.get(key) != request.get(key):
                return fail("decision_input_mismatch")
        if (("active_authority" in public) != ("active_authority" in request)
                or public.get("active_authority") != request.get("active_authority")):
            return fail("decision_input_mismatch")
        expected_ids = {}
        for entry in catalog_for(request)["entries"]:
            expected_ids.setdefault(entry["candidate_id"], {})[entry["field_path"]] = entry["reference_id"]
        if public.get("reference_ids") != expected_ids:
            return fail("reference_catalog_mismatch")
        if (payload["reasoning_effort"], payload["max_tokens"]) != (policy["reasoning_effort"], policy["maximum_output_tokens"]):
            return fail("unregistered_probe_setting")
        result.update(provider_request_sha256=expected_payload_sha256, decision_request_sha256=digest(request))
        value = _json(body)
        if type(value) is not dict or value.get("model") != payload["model"]:
            return fail("model_mismatch")
        usage = value.get("usage", {})
        if type(usage) is not dict or type(usage.get("completion_tokens")) is not int or not 0 <= usage["completion_tokens"] <= 10**9:
            return fail("usage_unavailable")
        if type(usage) is dict:
            result["provider_usage"] = {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                                        if type(usage.get(k)) is int and 0 <= usage[k] <= 10**9}
            details = usage.get("completion_tokens_details", {})
            if type(details) is dict and type(details.get("reasoning_tokens")) is int and 0 <= details["reasoning_tokens"] <= 10**9:
                result["reasoning_tokens"] = details["reasoning_tokens"]
            if "reasoning_tokens" not in result or result["reasoning_tokens"] > usage["completion_tokens"]:
                return fail("reasoning_usage_unavailable")
            if result["provider_usage"].get("completion_tokens", 0) > payload["max_tokens"]:
                return fail("output_budget_exceeded")
        choices = value.get("choices")
        if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
            return fail("completion_shape")
        choice = choices[0]
        result["finish_reason"] = choice.get("finish_reason") if choice.get("finish_reason") in ("stop", "length", "content_filter", "tool_calls") else "unknown"
        if result["finish_reason"] != "stop":
            return fail("output_limit" if result["finish_reason"] == "length" else "finish_reason")
        answer = _json(choice["message"]["content"])
        if type(answer) is not dict or set(answer) != {"action", "compared_candidate_ids", "summary", "reference_ids"}:
            return fail("answer_shape")
        if type(answer["action"]) is not str or answer["action"] not in request["allowed_actions"]:
            return fail("action")
        prepared = {k for k, v in request["candidates"].items() if not v["constraint_rejections"] and v.get("comparison_material")}
        ids = answer["compared_candidate_ids"]
        if type(ids) is not list or any(type(x) is not str for x in ids) or len(set(ids)) != len(ids) or set(ids) != prepared:
            return fail("compared_candidates")
        if type(answer["summary"]) is not str or not 1 <= len(answer["summary"]) <= 600:
            return fail("summary")
        if any(type(key) is str and key and key in canonical_bytes(answer).decode() for key in secret_values):
            return fail("credential_in_response")
        audit = verify_reference_ids(request, answer["reference_ids"], ids)
        result["reference_audit"] = audit
        if audit["status"] != "valid":
            return fail("reference_ids")
        result.update(status="valid", decision=answer)
        return result
    except (ValueError, KeyError, TypeError, OverflowError, RecursionError, UnicodeError):
        return fail("malformed_response")
