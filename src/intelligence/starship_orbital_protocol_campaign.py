"""One separately approved long-window protocol replay; no flight authority.

The earlier fixed four-call campaign remains immutable. This is a new developer
replay of its exact decision-time inputs, not a held-out flight or value study.
"""

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import os
import time

from .space_ops_investigator import DEEPSEEK_ENDPOINT, _send_once, canonical_bytes
from .starship_protocol_campaign import validate_bundle as validate_original_bundle
from .starship_protocol_campaign import _persist, _contains_secret
from .starship_reference_protocol import policy_for, build_payload, audit_response
from src.runtime.starship_replanning import digest

SETTING_ID = "ids_orbital_low_16384"
SCHEMA = "missionos.starship_orbital_protocol_bundle.v1"
SOURCE_NAMES = (
    "src/intelligence/starship_orbital_protocol_campaign.py",
    "src/intelligence/starship_reference_protocol.py",
    "src/intelligence/starship_protocol_campaign.py",
    "src/intelligence/starship_protocol_probe.py",
    "src/runtime/starship_reference_ids.py",
    "src/runtime/starship_decision_evidence.py",
    "src/runtime/starship_replanning.py",
    "src/intelligence/space_ops_investigator.py",
    "scripts/starship_orbital_protocol.py",
    "scripts/start_starship_gateway.py",
)


def _source_hashes():
    root = Path(__file__).resolve().parents[2]
    return {name: sha256((root / name).read_bytes()).hexdigest() for name in SOURCE_NAMES}


def _campaign_limits():
    return {
        "maximum_attempts": 2,
        "maximum_generated_tokens": 32768,
        "transport_timeout_s": 120,
        "decision_deadline_s": 150,
        "maximum_request_bytes": 16384,
        "maximum_response_bytes": 65536,
        "retries_per_attempt": 0,
        "resume_allowed": False,
        "order": [SETTING_ID, SETTING_ID],
        "readiness_rule": "exactly_two_valid_bounded_responses",
        "if_ready": "admit_at_most_one_separately_bounded_flight",
        "if_not_ready": "stop_orbital_protocol_testing",
        "flight_authority": False,
    }


def prepare_bundle(original_probe, approved_original_sha256):
    """Bind the previously verified full payload and freeze new limits offline."""
    original = validate_original_bundle(original_probe, approved_original_sha256)
    policy = policy_for(SETTING_ID)
    payload = build_payload(original["request"], original["original_payload"], policy=policy)
    return {
        "schema": SCHEMA,
        "scope": "separately_approved_developer_protocol_replay",
        "original_probe": original,
        "original_probe_sha256": approved_original_sha256,
        "original_wire_verified": True,
        "decision_request_sha256": original["decision_request_sha256"],
        "original_provider_request_sha256": original["original_provider_request_sha256"],
        "policy": policy,
        "payload": payload,
        "provider_request_sha256": digest(payload),
        "provider_request_bytes": len(canonical_bytes(payload)),
        "source_sha256": _source_hashes(),
        "proposed_campaign": _campaign_limits(),
        "provider_calls": 0,
        "flight_runs": 0,
        "dispatch": False,
        "held_out": False,
        "model_value_claim": False,
    }


def validate_bundle(bundle, approved_sha256):
    if type(bundle) is not dict or type(approved_sha256) is not str or digest(bundle) != approved_sha256:
        raise ValueError("orbital_protocol_approval_hash_mismatch")
    if bundle.get("schema") != SCHEMA:
        raise ValueError("invalid_orbital_protocol_bundle")
    try:
        expected = prepare_bundle(bundle["original_probe"], bundle["original_probe_sha256"])
        if bundle != expected:
            raise ValueError("orbital_protocol_bundle_mismatch")
    except (KeyError, TypeError):
        raise ValueError("invalid_orbital_protocol_bundle") from None
    return deepcopy(bundle)


def _invalid(reason):
    return {"status": "invalid", "errors": [reason], "decision": None,
            "provider_usage": {}, "reference_audit": None, "dispatch": False,
            "free_prose_verified": False}


def run_campaign(bundle, approved_sha256, output_dir, *, key, sender=None, clock=time.monotonic):
    """Reserve both calls durably, run them once in order, never resume or retry.

    A second directory does not constitute authorization to repeat this campaign.
    Only protocol readiness is produced; no simulator or dispatch is invoked.
    Stop early on reported token-cap violation or a change to the frozen source.
    """
    bundle = validate_bundle(bundle, approved_sha256)
    if type(key) is not str or not key or len(key) > 4096 or any(c.isspace() for c in key):
        raise ValueError("orbital_protocol_credential_unavailable")
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    policy, payload = bundle["policy"], bundle["payload"]
    slots = [{"slot": index + 1, "setting_id": SETTING_ID,
              "provider_request_sha256": bundle["provider_request_sha256"],
              "maximum_output_tokens": policy["maximum_output_tokens"]} for index in range(2)]
    reservation = {
        "schema": "missionos.starship_orbital_protocol_reservation.v1",
        "approved_bundle_sha256": approved_sha256,
        "original_probe_sha256": bundle["original_probe_sha256"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_sha256": bundle["source_sha256"],
        "authorization": "explicit_operator_approval_of_two_new_orbital_attempts",
        "slots": slots,
        "worstcase_reserved_tokens": 32768,
        "transport_timeout_s": 120,
        "decision_deadline_s": 150,
        "retries": 0, "resume_allowed": False, "flight_authority": False,
    }
    _persist(directory / "reservation.json", reservation)
    handle = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)
    _persist(directory / "approved-bundle.json", bundle)
    sender = sender or _send_once
    attempts, stop_reason = [], None
    for slot in slots:
        if _source_hashes() != bundle["source_sha256"]:
            stop_reason = "source_changed"
            break
        started = clock()
        try:
            status, body = sender(DEEPSEEK_ENDPOINT, canonical_bytes(payload), key,
                                  timeout_s=policy["transport_timeout_s"])
        except Exception:
            status, body = "transport_failed", None
        elapsed = clock() - started
        if status == "response" and type(body) is bytes and key.encode() not in body:
            audit = audit_response(
                bundle["original_probe"]["request"], payload, body, elapsed,
                expected_payload_sha256=bundle["provider_request_sha256"],
                policy=policy, secret_values=(key,),
            )
        else:
            reason = ("credential_in_response" if type(body) is bytes and key.encode() in body
                      else "transport_timeout" if status == "transport_timeout" else "transport_failed")
            audit = _invalid(reason)
        if _contains_secret(audit, key):
            audit = _invalid("credential_in_response")
        elapsed = clock() - started
        if elapsed > policy["decision_timeout_s"]:
            audit.update(status="invalid", decision=None)
            if "decision_deadline" not in audit["errors"]:
                audit["errors"].append("decision_deadline")
        if _source_hashes() != bundle["source_sha256"]:
            audit.update(status="invalid", decision=None)
            audit["errors"].append("source_changed")
            stop_reason = "source_changed"
        attempt = {
            **slot,
            "transport_status": status if status in ("response", "transport_timeout", "transport_failed") else "transport_failed",
            "call_attempted": True,
            "latency_s": round(elapsed, 6),
            "audit": audit,
        }
        _persist(directory / f"attempt-{slot['slot']:02d}.json", attempt)
        attempts.append(attempt)
        if audit["provider_usage"].get("completion_tokens", 0) > policy["maximum_output_tokens"]:
            stop_reason = "reported_token_cap_violation"
        if stop_reason:
            break
    valid = sum(row["audit"]["status"] == "valid" for row in attempts)
    ready = valid == 2 and len(attempts) == 2 and stop_reason is None
    summary = {
        "schema": "missionos.starship_orbital_protocol_campaign.v1",
        "approved_bundle_sha256": approved_sha256,
        "original_probe_sha256": bundle["original_probe_sha256"],
        "source_sha256": bundle["source_sha256"],
        "reservation_sha256": digest(reservation),
        "setting_id": SETTING_ID,
        "attempted_calls": len(attempts), "valid_responses": valid,
        "unspent_reserved_slots": 2 - len(attempts),
        "worstcase_reserved_tokens": 32768,
        "observed_completion_tokens": sum(row["audit"]["provider_usage"].get("completion_tokens", 0) for row in attempts),
        "usage_missing_attempts": sum("completion_tokens" not in row["audit"]["provider_usage"] for row in attempts),
        "attempts": attempts,
        "ready_for_one_separately_bounded_flight": ready,
        "maximum_admitted_followup_flights": 1 if ready else 0,
        "decision": "ready_for_one_separately_bounded_flight" if ready else "stop_orbital_protocol_testing",
        "stop_reason": stop_reason,
        "reliability_estimate": False, "held_out": False, "model_value_claim": False,
        "raw_response_retained": False, "raw_reasoning_retained": False,
        "retries": 0, "resume_allowed": False, "flight_runs": 0, "dispatch": False,
    }
    _persist(directory / "summary.json", summary)
    return summary
