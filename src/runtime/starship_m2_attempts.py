"""Credential-free reader of the host's durable attempt accounting.

An attempted dispatch is not confirmed model inference. The host writes before
transport; crash/cancellation may leave an unconfirmed in-progress attempt.
"""

import json
import math
from pathlib import Path

from .starship_replanning import digest
from .starship_call_limits import call_count_allowed, valid_call_limit, canonical_uncapped_jev_escalations

SCHEMA = "missionos.starship_m2_host_attempts.v1"
FIELDS = {
    "sequence",
    "request_id",
    "request_sha256",
    "provider",
    "provider_request_sha256",
    "timeout_s",
    "remaining_s",
    "request_file_age_s",
    "status",
    "completed",
    "valid_response",
    "response_published",
}


def authority_call_limits_valid(authority):
    """Independently bind aggregate limits to the explicitly approved phase limits."""
    try:
        deployment, returning = authority["deployment_authority"], authority["return_authority"]
        phase_jev = (deployment["maximum_jev_calls"], returning["maximum_jev_calls"])
        llm = returning["maximum_llm_calls"]
        if (any(type(value) is not int or value < 0 for value in phase_jev)
                or not valid_call_limit(llm)
                or deployment["mode"] != authority["mode"]
                or returning["mode"] != authority["mode"]):
            return False
        if authority["mode"] == "fixture":
            if llm is None:
                return False
            jev, llm = 0, 0
        elif authority["mode"] == "live":
            jev = sum(phase_jev)
            if llm is None and not canonical_uncapped_jev_escalations(returning) and (
                    returning.get("provider_budget_policy") != "independent_provider_reservations_v1"
                    or "orbital_planning_window" not in returning):
                return False
        else:
            return False
        total = None if llm is None else jev + llm
        return (type(authority["maximum_jev_calls"]) is int
                and authority["maximum_jev_calls"] == jev
                and valid_call_limit(authority["maximum_llm_calls"])
                and authority["maximum_llm_calls"] == llm
                and valid_call_limit(authority["maximum_model_calls"])
                and authority["maximum_model_calls"] == total)
    except (KeyError, TypeError, ValueError):
        return False


def empty(authority, run_id, phase):
    return {
        "schema": SCHEMA,
        "authority_sha256": digest(authority),
        "run_id": run_id,
        "phase": phase,
        "clock_mapping": "request_sim_seconds_as_wall_budget_v1",
        "adoption_deadline": "actor_rechecks_current_simulation_time",
        "attempts": [],
    }


def snapshot(mailbox, authority, run_id):
    return {
        phase: json.loads((Path(mailbox) / phase / "host-attempts.json").read_text())
        for phase in ("deployment", "return")
    }



def provider_timeout_limit(authority, phase, provider, request):
    """Recompute the separately approved orbital cap from decision-time evidence.

    A claimed 150-second deadline alone grants no extra provider time. Deployment
    and Jev remain at 30 seconds. Only canonical M2 orbital-update or registered
    operational-trial contracts expose the existing M1 long window; inputs are checked by
    the independent scalar verifier rather than the executor's window selector.
    This returns an upper bound, never an action or permission to dispatch.
    """
    if phase != "return":
        return 30.0
    envelope = authority["return_authority"]
    if "orbital_planning_window" not in envelope:
        return 30.0
    from .starship_m2_contract import m2_contract_matches
    from .starship_operational_trials import is_trial
    from .starship_replanning_verifier import _expected_planning_window

    try:
        case = authority["case"]
        if (
            (case != "orbital_update" and not is_trial(case))
            or not m2_contract_matches(authority)
        ):
            raise ValueError("m2_orbital_window_authority")
        expected = _expected_planning_window(envelope, request)
        if (
            digest(request.get("planning_window")) != digest(expected)
            or type(request["time_s"]) not in (int, float)
            or type(request["deadline_s"]) not in (int, float)
            or not math.isfinite(request["time_s"])
            or not math.isfinite(request["deadline_s"])
            or request["deadline_s"] != request["time_s"] + expected["duration_s"]
        ):
            raise ValueError("m2_orbital_window_request")
    except (KeyError, TypeError, OverflowError) as error:
        raise ValueError("m2_orbital_window_request") from error
    return expected["provider_timeout_s"] if provider == "deepseek" and expected["admitted"] else 30.0


def verify_attempts(study, actor_counts):
    """Keep all attempts, accepted host replies and usable actor receipts apart."""
    authority, run_id = study["authority"], study["run_id"]
    if not authority_call_limits_valid(authority):
        raise ValueError("m2_authority_call_limits")
    counts = {"deployment_jev": 0, "deployment_llm": 0, "return_jev": 0, "return_llm": 0}
    metadata = {
        "attempted": 0,
        "host_valid_responses": 0,
        "host_published_responses": 0,
        "actor_receipts": 0,
        "actor_valid_responses": 0,
        "actor_rules_accepted": 0,
        "incomplete_attempts": 0,
    }
    if authority["mode"] == "fixture":
        if study.get("provider_attempt_ledger") not in (None, {}):
            raise ValueError("fixture_host_attempts_forbidden")
        return metadata
    ledgers = study.get("provider_attempt_ledger")
    if type(ledgers) is not dict or set(ledgers) != {"deployment", "return"}:
        raise ValueError("m2_host_attempt_ledger_required")
    for phase in ("deployment", "return"):
        ledger = ledgers[phase]
        canonical = empty(authority, run_id, phase)
        if (
            type(ledger) is not dict
            or set(ledger) != set(canonical)
            or any(ledger[k] != v for k, v in canonical.items() if k != "attempts")
        ):
            raise ValueError("m2_host_attempt_ledger_binding")
        records = (
            study["deployment_run"]["mission_director"]["records"]
            if phase == "deployment"
            else study["return_study"]["decisions"]
        )
        known = {r["request"]["request_id"]: r for r in records}
        seen, matched = set(), set()
        if type(ledger["attempts"]) is not list:
            raise ValueError("m2_host_attempt_list")
        for sequence, attempt in enumerate(ledger["attempts"], 1):
            if type(attempt) is not dict or set(attempt) != FIELDS:
                raise ValueError("m2_host_attempt_schema")
            provider = attempt["provider"]
            record = known.get(attempt["request_id"])
            key = (attempt["request_id"], provider)
            if (
                record is None
                or key in seen
                or provider not in {"typesafe", "deepseek"}
                or phase == "deployment"
                and provider != "typesafe"
                or type(attempt["sequence"]) is not int
                or attempt["sequence"] != sequence
                or attempt["request_sha256"] != digest(record["request"])
            ):
                raise ValueError("m2_host_attempt_request_binding")
            seen.add(key)
            provider_sha = attempt["provider_request_sha256"]
            if (
                type(provider_sha) is not str
                or len(provider_sha) != 64
                or any(c not in "0123456789abcdef" for c in provider_sha)
            ):
                raise ValueError("m2_host_attempt_provider_hash")
            timeout, remaining = attempt["timeout_s"], attempt["remaining_s"]
            age = attempt["request_file_age_s"]
            request = record["request"]
            budget = (
                request["decision_deadline_s"] - request["observation"]["time_s"]
                if phase == "deployment"
                else request["deadline_s"] - request["time_s"]
            )
            provider_cap = provider_timeout_limit(authority, phase, provider, request)
            if (
                any(
                    type(x) not in (int, float) or not math.isfinite(x)
                    for x in (timeout, remaining, age)
                )
                or not 3 <= timeout <= min(provider_cap, remaining - 2.0)
                or age < 0.0 or not 5.0 <= remaining <= budget - age + 1e-6
            ):
                raise ValueError("m2_host_attempt_deadline")
            if (
                any(
                    type(attempt[k]) is not bool
                    for k in ("completed", "valid_response", "response_published")
                )
                or type(attempt["status"]) is not str
                or attempt["status"]
                not in {
                    "in_progress",
                    "succeeded",
                    "transport_failed",
                    "cancelled_request",
                    "transport_timeout",
                    "response_too_large",
                    "invalid_response",
                    "response_after_deadline",
                }
                or (attempt["status"] == "in_progress") is attempt["completed"]
                or attempt["valid_response"] is not (attempt["status"] == "succeeded")
                or attempt["response_published"]
                and not attempt["completed"]
            ):
                raise ValueError("m2_host_attempt_status")
            counts[phase + ("_jev" if provider == "typesafe" else "_llm")] += 1
            metadata["attempted"] += 1
            metadata["host_valid_responses"] += attempt["valid_response"]
            metadata["host_published_responses"] += attempt["response_published"]
            metadata["incomplete_attempts"] += not attempt["completed"]
            receipt = (record.get("response") or {}).get(
                "jev_invocation" if provider == "typesafe" else "llm_invocation"
            )
            if receipt and receipt.get("call_attempted"):
                if (
                    receipt.get("request_sha256") != provider_sha
                    or not attempt["completed"]
                    or not attempt["response_published"]
                    or receipt.get("status") != attempt["status"]
                ):
                    raise ValueError("m2_host_actor_attempt_mismatch")
                matched.add(key)
                metadata["actor_receipts"] += 1
                metadata["actor_valid_responses"] += receipt.get("model_inference_invoked") is True
                dispatch = record.get("dispatch") or {}
                metadata["actor_rules_accepted"] += (
                    dispatch.get("rules_accepted") is True
                    if phase == "deployment"
                    else record.get("accepted_action") is not None
                )
        if len(matched) != actor_counts[phase + "_jev"] + actor_counts[phase + "_llm"]:
            raise ValueError("m2_actor_receipt_without_host_attempt")
    if (
        counts["deployment_jev"] > authority["deployment_authority"]["maximum_jev_calls"]
        or counts["return_jev"] > authority["return_authority"]["maximum_jev_calls"]
        or not call_count_allowed(counts["return_llm"], authority["return_authority"]["maximum_llm_calls"])
        or not call_count_allowed(metadata["attempted"], authority["maximum_model_calls"])
        or study["runtime"]["model_calls"] != metadata["attempted"]
        or study["runtime"].get("actor_record_model_calls") != metadata["actor_receipts"]
    ):
        raise ValueError("m2_host_attempt_budget")
    return metadata
