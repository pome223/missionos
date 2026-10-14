"""Execute the four pre-registered protocol attempts; no vehicle authority."""

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
import time

from .space_ops_investigator import DEEPSEEK_ENDPOINT, _send_once, canonical_bytes
from .starship_protocol_probe import SCHEMA, SETTINGS, build_payload, audit_response
from src.runtime.starship_reference_ids import catalog_for
from src.runtime.starship_replanning import digest


def validate_bundle(bundle, approved_sha256):
    if type(approved_sha256) is not str or digest(bundle) != approved_sha256:
        raise ValueError("probe_approval_hash_mismatch")
    if bundle.get("schema") != SCHEMA or bundle.get("original_wire_verified") is not True:
        raise ValueError("invalid_probe_bundle")
    request, original = bundle["request"], bundle["original_payload"]
    if (digest(request) != bundle["decision_request_sha256"]
            or digest(original) != bundle["original_provider_request_sha256"]
            or len(canonical_bytes(original)) != bundle["original_provider_request_bytes"]
            or catalog_for(request) != bundle["catalog"]):
        raise ValueError("probe_source_binding_mismatch")
    expected = []
    for name, effort, cap in SETTINGS:
        payload = build_payload(request, original, effort, cap)
        expected.append({"setting_id": name, "payload": payload,
                         "provider_request_sha256": digest(payload),
                         "provider_request_bytes": len(canonical_bytes(payload)),
                         "planned_attempts": 2})
    if bundle["variants"] != expected:
        raise ValueError("probe_variant_mismatch")
    campaign = bundle["proposed_campaign"]
    caps = {"maximum_attempts": 4, "maximum_generated_tokens": 24576,
            "transport_timeout_s": 30, "decision_deadline_s": 35,
            "maximum_request_bytes": 16384, "maximum_response_bytes": 65536,
            "retries_per_attempt": 0,
            "order": [name for name, _, _ in SETTINGS for _ in range(2)]}
    if any(campaign.get(k) != v for k, v in caps.items()):
        raise ValueError("probe_campaign_limits_mismatch")
    if (bundle.get("provider_calls") != 0 or bundle.get("flight_runs") != 0
            or bundle.get("dispatch") is not False):
        raise ValueError("probe_not_fresh")
    return deepcopy(bundle)


def _persist(path, value):
    wire = canonical_bytes(value) + b"\n"
    with path.open("xb") as stream:
        stream.write(wire)
        stream.flush()
        os.fsync(stream.fileno())


def _contains_secret(value, key):
    if type(value) is str:
        return key in value
    if type(value) is dict:
        return any(_contains_secret(k, key) or _contains_secret(v, key) for k, v in value.items())
    if type(value) is list:
        return any(_contains_secret(v, key) for v in value)
    return False


def run_campaign(bundle, approved_sha256, output_dir, *, key, sender=None, clock=time.monotonic):
    """Reserve the entire budget durably before sending. Never resume or retry.

    The real sender has one fixed endpoint, no redirects/retries, a 30 s timeout
    and a 64 KiB read cap. Dependency injection exists for local stub testing.
    Actual future reuse of this interface needs a new explicit authorization;
    changing an output directory is not permission for another campaign.
    """
    bundle = validate_bundle(bundle, approved_sha256)
    if type(key) is not str or not key or len(key) > 4096 or any(c.isspace() for c in key):
        raise ValueError("probe_credential_unavailable")
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    variants = {v["setting_id"]: v for v in bundle["variants"]}
    slots = [
        {"slot": i + 1, "setting_id": name,
         "provider_request_sha256": variants[name]["provider_request_sha256"],
         "maximum_output_tokens": variants[name]["payload"]["max_tokens"]}
        for i, name in enumerate(bundle["proposed_campaign"]["order"])
    ]
    source_root = Path(__file__).resolve().parents[2]
    source_names = (
        "src/intelligence/starship_protocol_campaign.py",
        "src/intelligence/starship_protocol_probe.py",
        "src/runtime/starship_reference_ids.py",
        "src/runtime/starship_decision_evidence.py",
        "src/runtime/starship_replanning.py",
        "src/intelligence/space_ops_investigator.py",
        "scripts/run_starship_protocol_probe.py",
        "scripts/start_starship_gateway.py",
    )
    source_hashes = {name: sha256((source_root / name).read_bytes()).hexdigest() for name in source_names}
    reservation = {"schema": "missionos.starship_protocol_reservation.v1",
                   "approved_probe_sha256": approved_sha256,
                   "created_at": datetime.now(timezone.utc).isoformat(),
                   "source_sha256": source_hashes,
                   "authorization": "explicit_operator_approval_of_fixed_four_attempts",
                   "slots": slots, "worstcase_reserved_tokens": 24576,
                   "retries": 0, "resume_allowed": False, "flight_authority": False}
    _persist(directory / "reservation.json", reservation)
    # Persist the directory entry as well as the reservation contents before IO.
    handle = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)
    sender = sender or _send_once
    attempts = []
    for slot in slots:
        variant = variants[slot["setting_id"]]
        payload = variant["payload"]
        started = clock()
        try:
            status, body = sender(DEEPSEEK_ENDPOINT, canonical_bytes(payload), key)
        except Exception:
            status, body = "transport_failed", None
        elapsed = clock() - started
        if status == "response" and type(body) is bytes and key.encode() not in body:
            audit = audit_response(bundle["request"], payload, body, elapsed,
                                   expected_payload_sha256=variant["provider_request_sha256"])
        else:
            reason = "credential_in_response" if type(body) is bytes and key.encode() in body else (
                "transport_timeout" if status == "transport_timeout" else "transport_failed")
            audit = {"status": "invalid", "errors": [reason], "decision": None,
                     "provider_usage": {}, "reference_audit": None, "dispatch": False}
        if _contains_secret(audit, key):
            audit = {"status": "invalid", "errors": ["credential_in_response"],
                     "decision": None, "provider_usage": audit["provider_usage"],
                     "reference_audit": None, "dispatch": False}
        elapsed = clock() - started
        if elapsed > 35:
            audit.update(status="invalid", decision=None)
            audit["errors"].append("decision_deadline")
        attempt = {**slot, "transport_status": status if status in (
            "response", "transport_timeout", "transport_failed") else "transport_failed",
            "call_attempted": True, "latency_s": round(elapsed, 6), "audit": audit}
        _persist(directory / f"attempt-{slot['slot']:02d}.json", attempt)
        attempts.append(attempt)
        if audit["provider_usage"].get("completion_tokens", 0) > payload["max_tokens"]:
            break  # Do not spend remaining reservations after a reported cap violation.
    settings = []
    for name, _, _ in SETTINGS:
        rows = [a for a in attempts if a["setting_id"] == name]
        valid = sum(a["audit"]["status"] == "valid" for a in rows)
        settings.append({"setting_id": name, "attempts": len(rows),
                         "valid_responses": valid, "ready_for_one_flight": valid == 2 and len(attempts) == 4})
    selected = next((s["setting_id"] for s in settings if s["ready_for_one_flight"]), None)
    summary = {"schema": "missionos.starship_protocol_campaign.v1",
               "approved_probe_sha256": approved_sha256,
               "source_sha256": source_hashes,
               "reservation_sha256": digest(reservation),
               "unspent_reserved_slots": 4 - len(attempts),
               "attempted_calls": len(attempts), "worstcase_reserved_tokens": 24576,
               "observed_completion_tokens": sum(a["audit"]["provider_usage"].get("completion_tokens", 0) for a in attempts),
               "usage_missing_attempts": sum("completion_tokens" not in a["audit"]["provider_usage"] for a in attempts),
               "attempts": attempts, "settings": settings, "selected_setting_id": selected,
               "decision": "ready_for_one_bounded_flight" if selected else "stop_short_deadline_tuning",
               "reliability_estimate": False, "held_out": False,
               "raw_response_retained": False, "raw_reasoning_retained": False,
               "flight_runs": 0, "dispatch": False}
    _persist(directory / "summary.json", summary)
    return summary
