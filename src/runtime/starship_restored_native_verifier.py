"""Saved-only native orchestration checks; never a provider or plant runner.

The parent supplies a separately pinned host record and a bounded SHA->bytes
artifact map. This module opens no worker paths and authenticates no HMAC or
operator identity from JSON. Physical consistency, native provider evidence,
approval consumption, reservation changes and dispatch are separate outcomes.
Fixture v1/v2 records remain admitted only by their existing strict verifier.
"""
from __future__ import annotations

import gzip
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import json
import math
from pathlib import PurePosixPath
import re

from . import starship_restored_admission as admission
from . import starship_restored_contract as contract
from . import starship_restored_verifier as physical
from . import starship_restored_decision_pipe as pipe
from . import starship_sixdof_verifier as saved
from . import starship_state_mission_metrics as state_checks
from . import starship_booster_terminal_metrics as booster_checks
from . import starship_sixdof as dyn
from . import starship_physics as env
from .starship_restored_approval import RestoredApprovalBinding
from .starship_restored_integration import RestoredIntegrationPolicy
from .starship_restored_return import RestoredReturnRequest, RestoredReturnResult
from .starship_inflight_runtime import _context

SCHEMA = "missionos.starship_restored_native_mission.v1"
EXECUTION_SCHEMA = "missionos.starship_restored_native_execution.v1"
GOVERNANCE_SCHEMA = "missionos.starship_restored_return_governance.v1"
HOST_SCHEMA = "missionos.starship_restored_native_host.v1"
MAX_ARTIFACT_BYTES = 2**31
MAX_RAW_BYTES = 128*2**20
MAX_HOST_RECORD_BYTES = 512*2**20
OPS_INTENT_SCHEMA = "missionos.starship_restored_native_operations.v2"
INTENT_SCHEMA = "missionos.operational_intent.v1"
INTENT_OBSERVATION_SCHEMA = "missionos.operational_intent_observation.v1"
INTENT_REQUEST_SCHEMA = "missionos.starship_restored_decision_request.v2"
RETURN_CONTEXT_VERSION = "return_context.version"
RETURN_REVIEW_SOURCE = "src/runtime/starship_restored_return_review.py"
RETURN_REVIEW_POLICY = {"schema": "missionos.starship_restored_return_review_policy.v1",
    "confidence_floor": .5, "selected_action_margin_floor": .1}
RETURN_CONTEXT_REASONS = frozenset({
    "current_source_inventory_vehicle_or_frozen_opportunity_mismatch",
    "notice_generation_or_content_changed_rebind_saved_prediction",
    "resource_generation_or_content_changed_rebind_saved_prediction", "notice_stale",
    "original_nominal_booking_guard", "dispatch_not_first_due_reserved_tick",
    "actual_state_outside_continuous_predicted_coast", "prediction_coast_sample_gap", "prediction_coast_event_straddle",
    "fresh_position_m", "fresh_velocity_mps", "fresh_body_rate_rad_s", "fresh_attitude_deg", "fresh_fuel_kg",
    "fresh_actual_engine_availability", "fresh_throttle", "fresh_gimbal_rad", "fresh_flap_rad",
    "numerical_contact_capability_unmet", "synthetic_area_or_contact_availability_window",
    "resource_observation_is_future", "synthetic_energy_budget", "synthetic_temperature_budget",
    "synthetic_reserve_after_modeled_loss_budget"})


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(value):
    return sha256(_wire(value)).hexdigest()


def _json_read(raw):
    """One strict decoder shared by separately bounded saved input classes."""
    def pairs(items):
        row = dict(items)
        _require(len(row) == len(items), "native_duplicate_json_key")
        return row
    row = json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("native_nonfinite_json")))
    _wire(row)
    return row


def _read(raw):
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_RAW_BYTES, "native_json_raw_budget")
    return _json_read(raw)


def _host_record_budget(maximum):
    _require(type(maximum) is int and 0 < maximum <= MAX_HOST_RECORD_BYTES,
        "native_host_record_budget_invalid")
    return maximum


def _read_host_record(raw, maximum):
    maximum = _host_record_budget(maximum)
    _require(type(raw) is bytes and 0 < len(raw) <= maximum, "native_json_raw_budget")
    return _json_read(raw)


def _same(first, second):
    return _wire(first) == _wire(second)


def _has_return_context(request):
    return type(request) is dict and type(request.get("facts")) is dict and RETURN_CONTEXT_VERSION in request["facts"]


def _review_policy(value):
    """External opt-in, independently decoded without the producer policy API."""
    if value is None:
        return None
    _require(type(value) is dict and set(value) == set(RETURN_REVIEW_POLICY)
        and value.get("schema") == RETURN_REVIEW_POLICY["schema"]
        and all(type(value.get(key)) in (int, float) and math.isfinite(value[key])
            and value[key] == RETURN_REVIEW_POLICY[key]
            for key in ("confidence_floor", "selected_action_margin_floor")),
        "native_return_review_external_policy_invalid")
    return _json_read(_wire(value))


def _review_binding(value, policy):
    keys = {"return_review_policy", "return_review_policy_sha256"}
    _require(type(value) is dict and (not keys.intersection(value) if policy is None else
        keys <= value.keys() and _same(value["return_review_policy"], policy)
        and value["return_review_policy_sha256"] == _hash(policy)),
        "native_return_review_policy_binding_dropped_forged_or_undeclared")


def _review_route(request, first, policy):
    """Recompute routing from saved original evidence, never producer proof."""
    row = {"schema": "missionos.starship_restored_return_review_route.v1", "policy_sha256": _hash(policy),
        "route": "not_evaluated", "review_required": False, "metrics_are_uncalibrated": True,
        "confidence": None, "selected_action_probability": None, "maximum_other_probability": None,
        "selected_action_margin": None, "low_confidence": False, "small_selected_margin": False}
    if request["phase"] != "return":
        row["route"] = "not_applicable"
        return row
    if first is None:
        return row
    if (type(first) is not dict or first.get("status") != "valid" or first.get("provider") != "typesafe"
            or first.get("action") not in request["allowed_actions"]):
        row["route"] = "no_valid_jev_judgment"
        return row
    if first.get("action") == "request_deep_reasoning":
        row.update(route="explicit_jev_delegation", review_required=True)
        return row
    receipt = first.get("provider_receipt")
    probabilities = receipt.get("native_choice_probabilities") if type(receipt) is dict else None
    confidence = receipt.get("native_choice_confidence") if type(receipt) is dict else None
    actions = request["allowed_actions"]
    valid = (type(receipt) is dict and receipt.get("status") == "succeeded"
        and receipt.get("validation_error") is None and type(probabilities) is dict
        and set(probabilities) == set(actions) and first.get("action") in actions
        and all(type(value) in (int, float) and math.isfinite(value)
            and 0 <= value <= 1 for value in [*probabilities.values(), confidence])
        and math.isclose(sum(probabilities.values()), 1., abs_tol=.001))
    if not valid:
        row["route"] = "invalid_jev_metrics"
        return row
    selected = probabilities[first["action"]]
    other = max((value for action, value in probabilities.items() if action != first["action"]), default=0.)
    margin = Decimal(str(selected))-Decimal(str(other))
    low = Decimal(str(confidence)) < Decimal(str(policy["confidence_floor"]))
    small = margin < Decimal(str(policy["selected_action_margin_floor"]))
    row.update(route="rules_uncertainty_review" if low or small else "jev_final", review_required=low or small,
        confidence=confidence, selected_action_probability=selected, maximum_other_probability=other,
        selected_action_margin=float(margin), low_confidence=low, small_selected_margin=small)
    return row


def _return_context_facts(snapshot, candidates):
    """Expected tool projection from actual reservation and rebuilt admissions."""
    current = snapshot["binding"]
    identity = current["candidate_id"]
    _require(identity in ("nominal", "next_orbit") and admission._number(current["return_time_s"]),
        "native_return_context_actual_reservation_invalid")
    facts = {RETURN_CONTEXT_VERSION: 1, "reservation.nominal": identity == "nominal",
        "reservation.next_orbit": identity == "next_orbit", "reservation.return_time_s": current["return_time_s"]}
    for name in ("nominal", "next_orbit"):
        _candidate, check = candidates[name]
        reasons = check["reasons"]
        _require(type(reasons) is list and reasons == sorted(set(reasons)) and set(reasons) <= RETURN_CONTEXT_REASONS,
            "native_return_context_unknown_admission_reason")
        facts.update({f"notice.{name}_available": bool(check["matched_synthetic_site_ids"]),
            f"candidate.{name}.numerical_contact_capability_met": check["numerical_contact_capability_met"],
            f"candidate.{name}.admission_reason_count": len(reasons)})
        facts.update({f"candidate.{name}.reason.{reason}": True for reason in reasons})
    return facts


def _context_fact_key(name):
    return name == RETURN_CONTEXT_VERSION or name.startswith("reservation.") or name in (
        "notice.nominal_available", "notice.next_orbit_available") or any(name.startswith(f"candidate.{key}.reason.")
        or name in (f"candidate.{key}.numerical_contact_capability_met", f"candidate.{key}.admission_reason_count")
        for key in ("nominal", "next_orbit"))


def _context_request(request):
    """Keep the existing wire decoder for legacy fields, independently check new ones."""
    _require(_has_return_context(request) and request.get("schema") == "missionos.starship_restored_decision_request.v1"
        and request.get("phase") == "return", "native_return_context_requires_v1_return_request")
    facts = request["facts"]
    legacy = {**request, "facts": {key: value for key, value in facts.items() if not _context_fact_key(key)}}
    pipe._request(legacy)
    required = {RETURN_CONTEXT_VERSION, "reservation.nominal", "reservation.next_orbit", "reservation.return_time_s"}
    _require(type(facts[RETURN_CONTEXT_VERSION]) is int and facts[RETURN_CONTEXT_VERSION] == 1
        and type(facts.get("reservation.nominal")) is bool and type(facts.get("reservation.next_orbit")) is bool
        and facts["reservation.nominal"] is not facts["reservation.next_orbit"]
        and admission._number(facts.get("reservation.return_time_s")) and facts["reservation.return_time_s"] >= 0
        and all(type(facts.get(key)) is bool for key in (
            "authority.nominal_allowed", "authority.next_orbit_allowed", "authority.can_request_amendment")),
        "native_return_context_marker_or_reservation_invalid")
    for name in ("nominal", "next_orbit"):
        prefix = f"candidate.{name}.reason."
        reasons = {key[len(prefix):] for key in facts if key.startswith(prefix)}
        _require(reasons <= RETURN_CONTEXT_REASONS and all(facts[prefix+reason] is True for reason in reasons)
            and type(facts.get(f"candidate.{name}.admission_reason_count")) is int
            and facts[f"candidate.{name}.admission_reason_count"] == len(reasons)
            and type(facts.get(f"candidate.{name}.feasibility_passed")) is bool
            and admission._number(facts.get(f"candidate.{name}.return_time_s")) and facts[f"candidate.{name}.return_time_s"] >= 0
            and facts[f"candidate.{name}.feasibility_passed"] is (not reasons)
            and type(facts.get(f"candidate.{name}.numerical_contact_capability_met")) is bool
            and facts[f"candidate.{name}.numerical_contact_capability_met"] is ("numerical_contact_capability_unmet" not in reasons)
            and type(facts.get(f"notice.{name}_available")) is bool
            and facts[f"notice.{name}_available"] is ("synthetic_area_or_contact_availability_window" not in reasons),
            "native_return_context_sparse_reason_or_availability_invalid")
        required.update((f"notice.{name}_available", f"candidate.{name}.numerical_contact_capability_met",
            f"candidate.{name}.admission_reason_count"))
        required.update(prefix+reason for reason in reasons)
    current = "nominal" if facts["reservation.nominal"] else "next_orbit"
    _require({key for key in facts if _context_fact_key(key)} == required
        and facts["reservation.return_time_s"] == facts.get(f"candidate.{current}.return_time_s")
        and len(_wire(request)) <= 24000, "native_return_context_shape_or_reserved_time_changed")
    return request


def _context_legacy(request):
    return {**request, "facts": {key: value for key, value in request["facts"].items() if not _context_fact_key(key)}}


def _return_context_public_input(request, provider):
    """Independent public projection; no production return-context helper."""
    from src.intelligence.starship_restored_agent import public_input
    public = public_input(_context_legacy(request), provider=provider)
    public["evidence"] = request["facts"]
    facts, rows = request["facts"], {}
    for name in ("nominal", "next_orbit"):
        prefix = f"candidate.{name}.reason."
        rows[name] = {"candidate_id": name, "return_time_s": facts[f"candidate.{name}.return_time_s"],
            "independent_tools_accepted": facts[f"candidate.{name}.feasibility_passed"],
            "numerical_contact_capability_met": facts[f"candidate.{name}.numerical_contact_capability_met"],
            "operationally_available_at_predicted_contact": facts[f"notice.{name}_available"],
            "admission_reasons": sorted(key[len(prefix):] for key in facts if key.startswith(prefix)),
            "current_scope_allows_candidate": facts[f"authority.{name}_allowed"]}
    reserved = "nominal" if facts["reservation.nominal"] else "next_orbit"
    roles = {"select_nominal": "propose_candidate", "select_next_orbit": "propose_candidate",
        "keep_reserved_return": "preserve_current_reservation", "wait": "wait_with_current_reservation",
        "request_scope_amendment": "request_operator_review_for_candidate",
        "request_deep_reasoning": "delegate_judgment_without_reservation_change"}
    effects = {}
    for action in request["allowed_actions"]:
        name = "nominal" if action == "select_nominal" else "next_orbit" if action in (
            "select_next_orbit", "request_scope_amendment") else reserved
        effects[action] = {"action_role": roles[action], **rows[name],
            "would_preserve_current_reservation": action in ("keep_reserved_return", "wait", "request_deep_reasoning"),
            "operator_scope_amendment_required": not rows[name]["current_scope_allows_candidate"],
            "operator_amendment_request_currently_available": facts["authority.can_request_amendment"],
            "answer_alone_changes_reservation": False, "dispatch_permission_granted": False, "deadline_extended": False}
    public.update(return_context={"schema": "missionos.starship_restored_public_return_context.v1",
        "current_reservation": rows[reserved], "candidates": rows,
        "metric_semantics": {"candidate.*.power_kwh": "predicted energy remaining at return in kWh, not consumption",
            "resources.power_kwh": "current modeled energy remaining at decision time in kWh",
            "feasibility_passed": "independent current tool admission; not scope authority or a success guarantee"},
        "operations_are_synthetic": True, "mission_authority_granted": False,
        "future_contact_is_not_an_observed_outcome": True}, conditional_return_effects=effects)
    return public


def _checked_request(request, source):
    if _has_return_context(request):
        return _context_request(request)
    return _intent_request(request, source) if request.get("schema") == INTENT_REQUEST_SCHEMA else pipe._request(request)


def _checked_deep_request(request, remaining, *, return_review_policy=None):
    if not _has_return_context(request):
        return pipe._deep_request(request, remaining, **({"return_review_policy": return_review_policy}
            if return_review_policy is not None else {}))
    selected = _json_read(_wire(request))
    if return_review_policy is None or "request_deep_reasoning" in selected["allowed_actions"]:
        selected["allowed_actions"].remove("request_deep_reasoning")
    selected["deadline_s"] = min(request["deadline_s"], request["time_s"]+remaining)
    return _context_request(selected)


def _checked_review_exchange(result, request, policy):
    """V2 checked directly against original hashes and independent routing."""
    _require(type(result) is dict and result.get("schema") == "missionos.starship_restored_decision_exchange.v2"
        and result.get("request_id") == request["request_id"] and result.get("request_sha256") == _hash(request)
        and type(result.get("attempts")) is list and len(result["attempts"]) <= 2,
        "native_return_review_exchange_binding_invalid")
    attempts = result["attempts"]
    first = attempts[0].get("judgment") if attempts else None
    route = _review_route(request, first, policy)
    _require(_same(result.get("return_review"), route), "native_return_review_route_or_metrics_forged")
    previous_remaining = request["deadline_s"]-request["time_s"]
    for number, attempt in enumerate(attempts):
        provider = "typesafe" if not number else "deepseek"
        remaining = attempt.get("remaining_s")
        _require(type(remaining) in (int, float) and math.isfinite(remaining)
            and (remaining > 0 if not number else remaining >= 150.) and remaining <= previous_remaining,
            "native_return_review_original_budget_unreserved")
        previous_remaining = remaining
        if number:
            _require(route["review_required"] and request["phase"] == "return",
                "native_return_review_secondary_without_registered_trigger")
        selected = request if not number else _checked_deep_request(request, remaining, return_review_policy=policy)
        _require(attempt.get("provider") == provider and attempt.get("request_sha256") == _hash(selected)
            and attempt.get("decision_deadline_s") == selected["deadline_s"],
            "native_return_review_attempt_request_or_epoch_changed")
        if attempt.get("judgment") is not None:
            pipe._judgment(attempt["judgment"], selected, provider)
        else:
            _require(type(attempt.get("error_type")) is str, "native_return_review_attempt_error_missing")
    final = result.get("judgment")
    _require(final == (attempts[-1].get("judgment") if attempts else None),
        "native_return_review_original_judgment_replaced_or_silent_fallback")
    reason = result.get("reason")
    _require(reason is None or type(reason) is str, "native_return_review_reason_type")
    if reason is None and route["route"] == "invalid_jev_metrics":
        reason = "invalid_jev_review_metrics"
    if reason is None and route["review_required"] and len(attempts) < 2:
        reason = "required_return_review_not_completed"
    reviewed = not route["review_required"] or len(attempts) == 2
    valid = (final is not None and final.get("status") == "valid"
        and final.get("action") != "request_deep_reasoning" and reason is None and reviewed
        and route["route"] != "invalid_jev_metrics")
    invoked = [attempt["judgment"]["model_inference_invoked"] for attempt in attempts
        if attempt.get("judgment") is not None]
    unknown = any(attempt.get("judgment") is None for attempt in attempts) or bool(reason and reason.startswith("original_deadline"))
    expected = {"schema": "missionos.starship_restored_decision_exchange.v2", "request_id": request["request_id"],
        "request_sha256": _hash(request), "status": "valid" if valid else "no_valid_judgment",
        "action": final["action"] if valid else None, "judgment": final, "attempts": attempts, "reason": reason,
        "model_inference_invoked": True if any(invoked) else None if unknown else False,
        "execution_permission_granted": False, "delegation_is_approval": False,
        "synthetic_transport_is_model_evidence": False, "return_review": route}
    _require(_same(result, expected), "native_return_review_exchange_action_or_claim_changed")
    return result


def _checked_exchange(result, request, expected_return_review_policy=None):
    policy = _review_policy(expected_return_review_policy)
    if policy is not None:
        return _checked_review_exchange(result, request, policy)
    if not _has_return_context(request):
        return pipe._validate_result(result, request)
    # Use the original protocol validator on an equivalent legacy projection.
    # Only request digests are translated after verifying their full original
    # values. No action, inference flag, deadline or provider receipt is changed.
    _require(type(result) is dict and result.get("request_sha256") == _hash(request)
        and type(result.get("attempts")) is list and len(result["attempts"]) <= 2,
        "native_return_context_exchange_binding_invalid")
    translated = _json_read(_wire(result))
    translated["request_sha256"] = _hash(_context_legacy(request))
    for index, (original, row) in enumerate(zip(result["attempts"], translated["attempts"], strict=True)):
        selected = request if not index else _checked_deep_request(request, original["remaining_s"])
        _require(original.get("request_sha256") == _hash(selected), "native_return_context_attempt_digest_changed")
        wanted = _hash(_context_legacy(selected))
        row["request_sha256"] = wanted
        if row.get("judgment") is not None:
            _require(row["judgment"].get("request_sha256") == _hash(selected), "native_return_context_judgment_digest_changed")
            row["judgment"]["request_sha256"] = wanted
    if result.get("judgment") is not None:
        _require(bool(result["attempts"]) and _same(result["judgment"], result["attempts"][-1].get("judgment")),
            "native_return_context_final_not_last_original_attempt")
        translated["judgment"] = translated["attempts"][-1]["judgment"]
    pipe._validate_result(translated, _context_legacy(request))
    return result


def _artifact(reference, artifacts, *, packed=False):
    keys = {"path", "sha256", "raw_sha256"} if packed else {"path", "sha256"}
    _require(type(reference) is dict and set(reference) == keys
        and type(reference["path"]) is str, "native_artifact_reference_shape")
    path = PurePosixPath(reference["path"])
    _require(path.parts and not path.is_absolute() and ".." not in path.parts
        and contract._hash(reference["sha256"]), "native_artifact_reference_not_portable")
    raw = artifacts.get(reference["sha256"])
    _require(type(raw) is bytes and len(raw) <= MAX_ARTIFACT_BYTES
        and sha256(raw).hexdigest() == reference["sha256"], "native_artifact_missing_or_changed")
    if packed:
        with gzip.GzipFile(fileobj=BytesIO(raw)) as stream:
            restored = stream.read(MAX_RAW_BYTES+1)
        _require(len(restored) <= MAX_RAW_BYTES and contract._hash(reference["raw_sha256"])
            and sha256(restored).hexdigest() == reference["raw_sha256"], "native_prediction_restore_or_raw_hash_changed")
        raw = restored
    return raw


def _origin(reference, artifacts, profile_raw, source):
    raw = _artifact(reference, artifacts)
    return contract.ActualRestoredOrigin(reference["sha256"], raw, profile_raw, source)


def _snapshot(snapshot, origin, *, binding=None):
    row = origin.record()
    _require(type(snapshot) is dict and snapshot.get("phase") == "orbital_coast"
        and snapshot.get("return_controller") is None and snapshot.get("completed") is False
        and _same(snapshot.get("vehicle"), row["vehicle"]), "native_witness_not_actual_precontroller_coast")
    outages = {value["engine_name"] for value in row["ascent_settings"]["ascent"]["shutdowns"]
        if value["engine_name"].startswith("ship_")}
    state = contract._state(snapshot["state"], dyn.vehicle_from_dict(row["vehicle"]), outages)
    _require(state.time_s >= row["state"]["time_s"] and state.propellant_kg <= row["state"]["propellant_kg"]+1e-6,
        "native_witness_regressed_clock_or_added_fuel")
    if binding is not None:
        _require(_same(snapshot.get("binding"), binding), "native_witness_reservation_binding_changed")
    return snapshot["state"]


def _continued_origin(current, original):
    """Actual saved coast extension, never a predicted checkpoint installation."""
    new, old = current.record(), original.record()
    _require(all(_same(new[key], old[key]) for key in ("source_sha256", "profile_sha256", "ascent_settings",
        "booster_control_settings", "inventory_receipt_chain_sha256", "inventory_generation", "release_generation",
        "released_count", "retained_count", "schedule", "vehicle")), "native_current_origin_changed_family")
    new_raw, old_raw = _read(current.source_record_bytes()), _read(original.source_record_bytes())
    history, prefix = new_raw["state_history"], old_raw["state_history"]
    _require(len(history) >= len(prefix) and _same(history[:len(prefix)], prefix)
        and new["state"]["time_s"] >= old["state"]["time_s"]
        and new["state"]["propellant_kg"] <= old["state"]["propellant_kg"]+1e-6,
        "native_current_origin_lost_actual_prefix_or_added_fuel")


def _forecast_batches(rows, artifacts, profile_raw, source, policy, original, host):
    """Reconstruct exact source/origin/opportunity and complete worker receipts."""
    _require(type(rows) is list and len(rows) <= len(artifacts), "native_forecast_batch_budget")
    found, jobs = {}, set()
    for batch in rows:
        _require(type(batch) is dict and set(batch) == {"job", "batch_claim", "profile", "origins", "candidates"},
            "native_forecast_descriptor_shape")
        job_raw = _artifact(batch["job"], artifacts)
        job, token = _read(job_raw), sha256(job_raw).hexdigest()
        _require(token not in jobs and job.get("schema") == "missionos.starship_restored_forecast_batch.v1"
            and _same(job.get("source_sha256"), source) and _same(job.get("integration_policy"), policy.record())
            and job.get("prediction_only") is True and job.get("execution_initialized") is False
            and type(job.get("model_calls")) is int and job["model_calls"] == 0
            and job.get("maximum_workers") == 2 and type(job.get("maximum_workers")) is int
            and job.get("origin_proves_numerical_policy") is False
            and job.get("matching_actual_policy_requires_outer_registered_execution_binding") is True,
            "native_forecast_source_scope_or_policy_changed")
        jobs.add(token)
        if "environment" in host:
            _require(_same(job.get("environment"), host["environment"]), "native_forecast_environment_changed")
        _require(_same(_read(_artifact(batch["batch_claim"], artifacts)), {"job_sha256": token, "attempt": 1})
            and _artifact(batch["profile"], artifacts) == profile_raw
            and batch["profile"]["sha256"] == job["input_sha256"][job["profile_file"]]
            and type(batch["origins"]) is dict and set(batch["candidates"]) == {"nominal", "next_orbit"}
            and type(job.get("requests")) is list and len(job["requests"]) == 2,
            "native_forecast_profile_claim_or_pair_changed")
        specs = {row["candidate_id"]: row for row in job["requests"]}
        _require(set(specs) == {"nominal", "next_orbit"}, "native_forecast_duplicate_candidate")
        for candidate_id, descriptor in batch["candidates"].items():
            spec, schedule = specs[candidate_id], None
            _require(type(spec.get("schedule_chain")) is list and 1 <= len(spec["schedule_chain"]) <= 64,
                "native_forecast_missing_anchor_chain")
            for node in spec["schedule_chain"]:
                reference = batch["origins"][node["origin_file"]]
                _require(reference["sha256"] == node["origin_sha256"] == job["input_sha256"][node["origin_file"]],
                    "native_forecast_origin_pin_changed")
                current = _origin(reference, artifacts, profile_raw, source)
                _continued_origin(current, original)
                schedule = contract.RestoredCandidateSchedule(current, node["maximum_simulation_time_s"], anchor=schedule)
                _require(_same(schedule.record(), node["schedule"]), "native_forecast_retimed_schedule")
            _require(_same(schedule.opportunity_anchor_record(), contract.RestoredCandidateSchedule(original).opportunity_anchor_record()),
                "native_forecast_borrowed_another_opportunity")
            request = RestoredReturnRequest(schedule.origin, schedule, candidate_id, profile_raw, source)
            _require(_same(request.record(), spec["request"]), "native_forecast_request_changed")
            _require(type(descriptor) is dict and set(descriptor) == {"prediction", "result", "integration", "started", "worker_claim"},
                "native_forecast_candidate_descriptor_shape")
            prediction_raw = _artifact(descriptor["prediction"], artifacts, packed=True)
            prediction = RestoredReturnResult(prediction_raw)
            raw = prediction.record()
            _require(raw["prediction_only"] is True and _same(raw["initial_binding"], request.record())
                and _same(raw["binding"], request.record()), "native_forecast_prediction_request_changed")
            integration = _read(_artifact(descriptor["integration"], artifacts))
            physical._integration_receipt({"numerical_integration": integration}, policy)
            claim = {"job_sha256": token, "candidate_id": candidate_id,
                "request_sha256": _hash(request.record()), "attempt": 1}
            _require(_same(_read(_artifact(descriptor["started"], artifacts)), claim)
                and _same(_read(_artifact(descriptor["worker_claim"], artifacts)), claim), "native_forecast_one_use_claim_changed")
            expected = {"schema": "missionos.starship_restored_forecast_worker_result.v1", **claim,
                "raw_sha256": sha256(prediction_raw).hexdigest(), "gzip_sha256": descriptor["prediction"]["sha256"],
                "integration_execution": integration, "prediction_only": True, "model_calls": 0, "execution_initialized": False}
            _require(_same(_read(_artifact(descriptor["result"], artifacts)), expected), "native_forecast_worker_result_changed")
            hashed = sha256(prediction_raw).hexdigest()
            _require(hashed not in found, "native_duplicate_prediction_record")
            found[hashed] = (prediction, schedule.origin, schedule)
    return found


def _candidate(row, forecasts, profile_raw, source, original):
    _require(type(row) is dict and row.get("prediction_record_sha256") in forecasts, "native_candidate_has_no_complete_forecast")
    prediction, origin, _ = forecasts[row["prediction_record_sha256"]]
    policy = admission.RestoredAdmissionPolicy()
    _require(_same(row.get("policy"), policy.record()) and type(row.get("sites")) is list, "native_candidate_policy_changed")
    sites = tuple(admission.RestoredSitePolicy(**{key: site[key] for key in (
        "site_id", "latitude_deg", "longitude_deg", "radius_m", "opens_s", "closes_s")}) for site in row["sites"])
    notice_row, resource_row = row["notice"], row["resources"]
    notice = admission.RestoredOperationalNotice(notice_row["generation"], notice_row["issued_s"],
        notice_row["expires_s"], tuple(notice_row["available_site_ids"]))
    resources = admission.RestoredResourceState(resource_row["generation"], resource_row["observed_s"],
        resource_row["energy_kwh"], resource_row["temperature_k"], resource_row["modeled_propellant_loss_kg"])
    rebuilt = admission.RestoredCandidate(prediction, origin, contract.RestoredCandidateSchedule(original),
        profile_raw, source, policy, sites, notice, resources)
    _require(_same(rebuilt.record(), row), "native_candidate_saved_proof_changed")
    return rebuilt, notice, resources


def _saved_admission(candidate, snapshot, original, notice, resources, stage):
    """Independent current saved-state admission; does not construct an actor."""
    row, before = candidate.record(), original.record()
    actual = _snapshot(snapshot, original)
    proposal, policy = row["proposal"], row["policy"]
    when, anchor, now = proposal["return_time_s"], proposal["opportunity_anchor"], actual["time_s"]
    reasons = []
    if not _same(anchor, contract.RestoredCandidateSchedule(original).opportunity_anchor_record()):
        reasons.append("current_source_inventory_vehicle_or_frozen_opportunity_mismatch")
    if not notice.issued_s <= now <= notice.expires_s:
        reasons.append("notice_stale")
    if stage == "selection":
        if now > anchor["nominal_s"]-policy["booking_guard_s"]:
            reasons.append("original_nominal_booking_guard")
    elif not (0 <= now-when <= snapshot["binding"]["maximum_start_clock_slip_s"]+1e-7
            and snapshot["binding"]["candidate_id"] == proposal["candidate_id"]
            and snapshot["binding"]["return_time_s"] == when):
        reasons.append("dispatch_not_first_due_reserved_tick")
    prediction = _read(candidate._prediction_json)
    freshness, fresh = admission._fresh(prediction, actual, policy["fresh_state_match"])
    reasons.extend(freshness)
    metrics = row["numerical_contact"]
    if not metrics["capability_met"]:
        reasons.append("numerical_contact_capability_unmet")
    matched = []
    if metrics["contact_observed"]:
        matched = [site["site_id"] for site in row["sites"] if site["site_id"] in notice.available_site_ids
            and admission._site_distance(metrics, site) <= site["radius_m"]
            and site["opens_s"] <= metrics["event_time_s"] <= site["closes_s"]
            and metrics["event_time_s"] <= notice.expires_s]
    if not matched:
        reasons.append("synthetic_area_or_contact_availability_window")
    waiting = max(0., when-resources.observed_s)
    accounting = {"prospective_orbital_wait_s": waiting,
        "energy_at_return_kwh": resources.energy_kwh-waiting*policy["waiting"]["power_kw"]/3600.,
        "temperature_at_return_k": resources.temperature_k+waiting*policy["waiting"]["warming_k_per_s"],
        "modeled_loss_budget_kg": resources.modeled_propellant_loss_kg+waiting*policy["waiting"]["modeled_loss_kg_per_s"],
        "accounting_assumptions_not_physical_state": True}
    # Dispatch may observe the actual first due tick after its reserved epoch;
    # that clock slip is independently bounded above. Selection cannot borrow
    # an observation after the reserved epoch, and neither stage borrows future.
    if resources.observed_s > now or stage == "selection" and resources.observed_s > when:
        reasons.append("resource_observation_is_future")
    if accounting["energy_at_return_kwh"] < policy["waiting"]["minimum_energy_kwh"]:
        reasons.append("synthetic_energy_budget")
    if accounting["temperature_at_return_k"] > policy["waiting"]["maximum_temperature_k"]:
        reasons.append("synthetic_temperature_budget")
    accounting["reserve_after_modeled_loss_kg"] = metrics.get("propellant_kg", 0.)-accounting["modeled_loss_budget_kg"]
    if accounting["reserve_after_modeled_loss_kg"] < policy["contact"]["minimum_propellant_kg"]:
        reasons.append("synthetic_reserve_after_modeled_loss_budget")
    current = {"schema": "missionos.starship_restored_current_observation.v1",
        "closed_origin_sha256": _hash(before), "source_sha256": before["source_sha256"],
        "profile_sha256": before["profile_sha256"], "inventory_receipt_chain_sha256": before["inventory_receipt_chain_sha256"],
        "release_generation": before["release_generation"], "retained_count": before["retained_count"],
        "state": actual, "vehicle": snapshot["vehicle"], "phase": "orbital_coast", "binding": snapshot["binding"],
        "opportunity_anchor": anchor, "actual_state_assigned": False,
        "provenance_is_not_authenticated_worker_receipt": True, "mission_authority_granted": False}
    return {"schema": "missionos.starship_restored_candidate_admission.v1", "accepted": not reasons,
        "reasons": sorted(set(reasons)), "stage": stage, "time_s": now,
        "proposal_sha256": row["proposal_sha256"], "candidate_sha256": row["candidate_sha256"],
        "forecast_proof_sha256": row["forecast_proof_sha256"], "prediction_record_sha256": row["prediction_record_sha256"],
        "observation_sha256": _hash(current), "notice_sha256": _hash(notice.record()), "resource_sha256": _hash(resources.record()),
        "matched_synthetic_site_ids": sorted(matched), "fresh_state_match": fresh, "resource_accounting": accounting,
        "numerical_contact_capability_met": metrics["capability_met"], "uncertainty_envelope_verified": False,
        "one_copied_current_deterministic_forecast_only": True, "old_certificate_admitted": False,
        "mission_authority_granted": False, "dispatch_authorized": False, "physical_state_modified": False}


def _provider_evidence(judgment, request, provider, mode):
    """Valid JSON choice alone never confirms hosted inference."""
    from src.intelligence.starship_restored_agent import public_input
    receipt = judgment.get("provider_receipt")
    public = (_return_context_public_input(request, provider) if _has_return_context(request)
        else _intent_public_input(request, provider) if request.get("schema") == INTENT_REQUEST_SCHEMA
        else public_input(request, provider=provider))
    _require(type(receipt) is dict and receipt.get("provider") == provider
        and receipt.get("request_input_sha256") == _hash(public)
        and type(receipt.get("model_inference_invoked")) is bool
        and judgment.get("model_inference_invoked") is receipt["model_inference_invoked"]
        and type(receipt.get("retries")) is int and receipt["retries"] == 0
        and all(receipt.get(key) is False for key in ("approval_granted", "execution_authorized", "dispatch",
            "physical_execution", "raw_prompt_recorded", "raw_response_recorded", "raw_reasoning_recorded")),
        "native_provider_scope_input_or_inference_claim_changed")
    flags = ("call_attempted", "call_succeeded", "response_received", "complete_response_observed")
    _require(all(type(receipt.get(key)) is bool for key in flags), "native_provider_attempt_flags_missing")
    if mode == "fixture":
        _require(receipt.get("mode") in ("fixture", "synthetic_fixture")
            and receipt["model_inference_invoked"] is False
            and receipt["call_attempted"] is receipt["call_succeeded"] is False,
            "native_fixture_borrowed_live_inference")
        return 0, 0
    _require(receipt.get("mode") == "live", "native_live_request_borrowed_fixture_receipt")
    confirmed = receipt["model_inference_invoked"]
    if confirmed:
        expected_model = "deepseek-flash" if provider == "deepseek" else "jev-1.13.0"
        _require(judgment["status"] == "valid" and receipt.get("status") == "succeeded"
            and all(receipt[key] is True for key in flags)
            and receipt.get("model_id") == receipt.get("request_model") == expected_model
            and receipt.get("model_inference_status") == "confirmed_by_valid_response"
            and contract._hash(receipt.get("response_sha256")) and contract._hash(receipt.get("request_sha256"))
            and type(receipt.get("request_bytes")) is int and receipt["request_bytes"] > 0
            and type(receipt.get("response_bytes_read")) is int and receipt["response_bytes_read"] > 0,
            "native_confirmed_inference_lacks_native_attempt_and_response_evidence")
    else:
        _require(receipt["call_succeeded"] is False, "native_success_without_confirmed_inference")
    return int(receipt["call_attempted"]), int(confirmed)


def _judgments(client, host, *, context, plan_sha256, source, mode, expected_return_review_policy=None):
    policy = _review_policy(expected_return_review_policy)
    _review_binding(client, policy)
    _review_binding(host, policy)
    wanted = {"context": context, "plan_sha256": plan_sha256, "source_sha256": _hash(source)}
    if policy is not None:
        wanted["return_review_policy_sha256"] = _hash(policy)
    suffix = ".v1" if policy is None else ".v2"
    _require(type(client) is dict and client.get("schema") == "missionos.starship_restored_decision_client"+suffix
        and type(host) is dict and host.get("schema") == "missionos.starship_restored_decision_host"+suffix
        and all(_same(client.get(key), value) and _same(host.get(key), value) for key, value in wanted.items())
        and client.get("pending_request") is None and client.get("provider_calls_by_worker") == 0
        and type(client.get("provider_calls_by_worker")) is int
        and client.get("execution_permission_granted") is host.get("execution_permission_granted") is False
        and type(client.get("results")) is list and type(host.get("requests")) is list,
        "native_judgment_host_worker_or_source_binding_changed")
    sessions, attempted, confirmed, unknown = {}, 0, 0, False
    for index, session in enumerate(host["requests"]):
        frame = session["frame"]
        request = _checked_request(frame["request"], source)
        request_id = request["request_id"]
        _require(request_id not in sessions and all(_same(frame.get(key), value) for key, value in wanted.items())
            and frame.get("sequence") == index+1 and type(frame.get("sequence")) is int
            and frame.get("schema") == "missionos.starship_restored_decision_pipe_request"+suffix
            and (policy is not None or "return_review_policy_sha256" not in frame)
            and frame.get("request_id") == request_id and frame.get("request_sha256") == _hash(request)
            and admission._number(frame.get("started_wall_s")) and frame["started_wall_s"] >= 0
            and frame.get("wall_deadline_s") == frame["started_wall_s"]+request["deadline_s"]-request["time_s"]
            and type(session.get("attempts")) is list and len(session["attempts"]) <= 2,
            "native_request_epoch_repeated_retimed_or_unbound")
        delivered = session.get("delivered_result")
        thread_result = session.get("thread_result")
        for result in (delivered, thread_result):
            if result is not None:
                _checked_exchange(result, request, policy)
                completed = result["attempts"]
                _require(_same(completed, session["attempts"][:len(completed)]),
                    "native_delivered_attempts_differ_from_retained_host_attempts")
        if thread_result is not None:
            _require(_same(thread_result["attempts"], session["attempts"]),
                "native_completed_thread_lost_attempt_receipts")
        unknown = unknown or len(session.get("started_attempts", [])) > len(session["attempts"])
        if policy is not None:
            started = session.get("started_attempts")
            _require(type(started) is list and len(session["attempts"]) <= len(started) <= 2
                and len(started) <= len(session["attempts"])+1,
                "native_return_review_started_counter_or_attempts_forged")
            for number, attempt in enumerate(started):
                remaining = attempt.get("remaining_s")
                _require(type(remaining) in (int, float) and math.isfinite(remaining)
                    and (0 < remaining <= request["deadline_s"]-request["time_s"] if not number else 150. <= remaining <= started[0]["remaining_s"]),
                    "native_return_review_started_original_budget_unreserved")
                selected = request if not number else _checked_deep_request(request, remaining, return_review_policy=policy)
                expected_started = {"provider": "typesafe" if not number else "deepseek", "remaining_s": remaining,
                    "decision_deadline_s": selected["deadline_s"], "request_sha256": _hash(selected)}
                _require(_same(attempt, expected_started), "native_return_review_started_request_forged")
                if number:
                    first = session["attempts"][0].get("judgment") if session["attempts"] else None
                    _require(_review_route(request, first, policy)["review_required"],
                        "native_return_review_started_without_registered_trigger")
                if number < len(session["attempts"]):
                    complete = session["attempts"][number]
                    _require(_same(attempt, {key: complete.get(key) for key in expected_started}),
                        "native_return_review_started_and_completed_attempts_differ")
        for number, attempt in enumerate(session["attempts"]):
            provider = "typesafe" if number == 0 else "deepseek"
            selected = request if number == 0 else _checked_deep_request(request, attempt["remaining_s"], return_review_policy=policy)
            _require(attempt.get("provider") == provider
                and attempt.get("request_sha256") == _hash(selected)
                and attempt.get("decision_deadline_s") == selected["deadline_s"], "native_provider_request_changed")
            if number:
                first = session["attempts"][0].get("judgment")
                _require(type(first) is dict and first.get("status") == "valid"
                    and (first.get("action") == "request_deep_reasoning" if policy is None else
                        _review_route(request, first, policy)["review_required"])
                    and attempt["remaining_s"] >= 150.,
                    "native_deepseek_not_explicit_or_original_budget_unreserved")
            judgment = attempt.get("judgment")
            if judgment is not None:
                pipe._judgment(judgment, selected, provider)
                sends, invokes = _provider_evidence(judgment, selected, provider, mode)
                attempted += sends
                confirmed += invokes
            else:
                unknown = True
        sessions[request_id] = {"request": request, "frame": frame, "session": session}
    results = {}
    for result in client["results"]:
        request_id = result["request_id"]
        _require(request_id in sessions and request_id not in results, "native_worker_result_without_unique_host_request")
        session = sessions[request_id]
        _checked_exchange(result, session["request"], policy)
        if result["status"] == "valid":
            _require(_same(result, session["session"]["delivered_result"]), "native_worker_changed_valid_host_judgment")
        else:
            _require(result.get("reason") is None or type(result.get("reason")) is str,
                "native_invalid_timeout_reason_type")
        last = result.get("judgment")
        results[request_id] = {**session, "exchange": result,
            "native_choice": result["status"] == "valid" and last is not None
                and last.get("model_inference_invoked") is True}
    _require(set(results) == set(sessions), "native_host_worker_request_history_incomplete")
    return results, None if unknown else attempted, confirmed


def _review_verification(exchanges, policy):
    triggered = explicit = completed = unresolved = 0
    for item in exchanges.values():
        exchange = item["exchange"]
        attempts = exchange["attempts"]
        route = _review_route(item["request"], attempts[0].get("judgment") if attempts else None, policy)
        if route["route"] == "explicit_jev_delegation":
            explicit += 1
        if route["route"] == "rules_uncertainty_review":
            triggered += 1
            adopted = (len(attempts) == 2 and attempts[1]["provider"] == "deepseek"
                and exchange["status"] == "valid" and exchange["judgment"] == attempts[1].get("judgment")
                and exchange["judgment"]["status"] == "valid")
            completed += int(adopted)
            unresolved += int(not adopted)
    return {"schema": "missionos.starship_restored_return_review_verification.v1", "policy_sha256": _hash(policy),
        "policy_triggered_count": triggered, "explicit_jev_delegation_count": explicit,
        "completed_policy_review_count": completed, "unresolved_policy_review_count": unresolved,
        "original_jev_judgments_preserved": True, "review_is_approval": False}


def _intent(value, slot):
    """Independent registered-goal validation; no producer goal/projection API."""
    _require(type(value) is dict and set(value) == {"schema", "intent_id", "generation", "issuer",
        "intent_class", "evaluation_point", "trigger", "objective"}
        and value["schema"] == INTENT_SCHEMA and type(value["intent_id"]) is str
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", value["intent_id"]) is not None
        and type(value["generation"]) is int and 0 <= value["generation"] <= 1000000
        and value["issuer"] == "registered_operations" and value["intent_class"] == "desired_outcome_not_authority"
        and value["evaluation_point"] == "closed_inventory", "native_registered_intent_definition_invalid")
    trigger, objective = value["trigger"], value["objective"]
    _require(type(trigger) is dict and set(trigger) == {"phase", "released_count"}
        and trigger["phase"] == "deployment" and type(trigger["released_count"]) is int
        and trigger["released_count"] == slot, "native_registered_intent_trigger_invalid")
    _require(type(objective) is dict and set(objective) == {"metric", "comparison", "value", "unit"}
        and objective["metric"] in ("inventory.released_count", "inventory.retained_count")
        and objective["comparison"] in ("eq", "gte", "lte") and type(objective["value"]) is int
        and 0 <= objective["value"] <= 26 and objective["unit"] == "count",
        "native_registered_intent_objective_invalid")
    return value


def _intent_met(intent, released, retained):
    objective = intent["objective"]
    actual = released if objective["metric"] == "inventory.released_count" else retained
    target = objective["value"]
    return {"eq": actual == target, "gte": actual >= target, "lte": actual <= target}[objective["comparison"]]


def _intent_facts(intent, checkpoint):
    slot, when = checkpoint["released_count"], checkpoint["time_s"]
    facts = {"time_s": when, "released_count": slot, "retained_count": 26-slot,
        "release_ready": checkpoint["release_ready"], "sequence_closed": False, "intent.active": True,
        "intent.generation": intent["generation"], "intent.target_value": intent["objective"]["value"],
        "intent.observed_value": slot if intent["objective"]["metric"] == "inventory.released_count" else 26-slot}
    for action, released in (("continue_deployment", 26), ("withhold_deployment", slot)):
        facts.update({f"effect.{action}.released_count_if_completed": released,
            f"effect.{action}.retained_count_if_completed": 26-released,
            f"effect.{action}.intent_met_if_completed": _intent_met(intent, released, 26-released)})
    return facts


def _intent_request(request, source):
    """Strict v2 wire checks without production intent/projection functions."""
    _require(type(request) is dict and set(request) == {"schema", "request_id", "phase", "scope_sha256", "time_s",
        "deadline_s", "facts", "notice_summary", "allowed_actions", "operational_intent"}
        and request["schema"] == INTENT_REQUEST_SCHEMA and request["phase"] == "deployment"
        and type(request["request_id"]) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", request["request_id"]) is not None
        and contract._hash(request["scope_sha256"]) and admission._number(request["time_s"]) and request["time_s"] >= 0
        and admission._number(request["deadline_s"]) and request["deadline_s"] == request["time_s"]+35.
        and type(request["notice_summary"]) is str and len(request["notice_summary"]) <= 600
        and request["allowed_actions"] == ["continue_deployment", "withhold_deployment"], "native_intent_request_shape_invalid")
    observation = request["operational_intent"]
    _require(type(observation) is dict and set(observation) == {"schema", "intent", "operations_sha256", "source_sha256",
        "scope_sha256", "checkpoint", "checkpoint_sha256", "authority_granted", "closed_inventory_inferred", "physical_release_inferred"}
        and observation["schema"] == INTENT_OBSERVATION_SCHEMA
        and all(contract._hash(observation[key]) for key in ("operations_sha256", "source_sha256", "scope_sha256", "checkpoint_sha256"))
        and observation["source_sha256"] == _hash(source) and observation["scope_sha256"] == request["scope_sha256"]
        and all(observation[key] is False for key in ("authority_granted", "closed_inventory_inferred", "physical_release_inferred")),
        "native_intent_observation_shape_or_scope_invalid")
    checkpoint = observation["checkpoint"]
    _require(type(checkpoint) is dict and set(checkpoint) == {"phase", "time_s", "released_count", "retained_count", "total_count",
        "release_ready", "sequencer_state"} and checkpoint["phase"] == "deployment"
        and type(checkpoint["released_count"]) is int and checkpoint["released_count"] in (0, 13)
        and type(checkpoint["retained_count"]) is int and checkpoint["retained_count"] == 26-checkpoint["released_count"]
        and type(checkpoint["total_count"]) is int and checkpoint["total_count"] == 26
        and admission._number(checkpoint["time_s"]) and checkpoint["time_s"] == request["time_s"]
        and type(checkpoint["release_ready"]) is bool and type(checkpoint["sequencer_state"]) is str
        and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", checkpoint["sequencer_state"]) is not None
        and observation["checkpoint_sha256"] == _hash(checkpoint), "native_intent_checkpoint_shape_invalid")
    _intent(observation["intent"], checkpoint["released_count"])
    _require(_same(request["facts"], _intent_facts(observation["intent"], checkpoint)) and len(_wire(request)) <= 24000,
        "native_intent_request_numeric_projection_changed")
    return request


def _intent_public_input(request, provider):
    """Reuse legacy text contract only; all new goal/effect math is independent."""
    from src.intelligence.starship_restored_agent import public_input
    base = {key: value for key, value in request.items() if key != "operational_intent"}
    base["schema"] = "missionos.starship_restored_decision_request.v1"
    base["facts"] = {key: request["facts"][key] for key in (
        "time_s", "released_count", "retained_count", "release_ready", "sequence_closed")}
    public = public_input(base, provider=provider)
    public["evidence"] = request["facts"]
    intent = request["operational_intent"]["intent"]
    slot = request["operational_intent"]["checkpoint"]["released_count"]
    public["operational_intent"] = {key: intent[key] for key in (
        "intent_id", "generation", "issuer", "intent_class", "evaluation_point", "objective")} | {
        "authority_granted": False, "arbitrary_notice_prose_is_not_a_registered_goal": True}
    effects = {}
    for action, released in (("continue_deployment", 26), ("withhold_deployment", slot)):
        effects[action] = {"released_count_if_completed": released, "retained_count_if_completed": 26-released,
            "intent_met_if_completed": _intent_met(intent, released, 26-released),
            "conditional_on_all_remaining_releases_passing_independent_gates": action == "continue_deployment",
            "physical_release_or_success_inferred": False, "authority_granted": False}
    public["conditional_inventory_effects"] = effects
    return public


def _deployment_intent(request, actual, prefix, profile, operations, source):
    """Bind a desired goal to the initial actual checkpoint, before any reply."""
    slot = actual["summary"]["released_count"]
    intent = operations["deployment_intents"][str(slot)]
    if intent is None:
        _require(request["schema"] == "missionos.starship_restored_decision_request.v1"
            and "operational_intent" not in request
            and not any(key.startswith(("intent.", "effect.")) for key in request["facts"]),
            "native_unregistered_intent_or_projection_borrowed")
        return
    _intent(intent, slot)
    pending = actual.get("pending")
    when = actual["summary"]["time_s"] if pending is None else pending["started_s"]
    releases = prefix["restored_prefix"]["handoff_record"]["inventory"]["release_receipts"]
    witnesses = [sample for sample in prefix["samples"] if sample["time_s"] == when
        and sample["phase"] == "orbital_coast"]
    _require(witnesses and sum(receipt["time_s"] < when for receipt in releases) == slot,
        "native_intent_checkpoint_has_no_actual_inventory_witness")
    # Prefix physical checks independently bind every sample's pressure to its
    # actual state/craft. This repeats the actual release gate at request time,
    # which can precede the pending decision's resolution sample by 35 seconds.
    ready = []
    for sample in witnesses:
        bound, perigee = saved._orbit(sample)
        ready.append(bool(bound and perigee >= profile["guidance"]["target_perigee_m"]-1000.
            and sample["dynamic_pressure_pa"] < 1
            and env.norm(sample["omega_body_rad_s"]) < profile["guidance"]["release_max_rate_rad_s"]))
    _require(all(value is ready[0] for value in ready), "native_intent_checkpoint_ambiguous_release_gate")
    checkpoint = {"phase": "deployment", "time_s": when, "released_count": slot, "retained_count": 26-slot,
        "total_count": 26, "release_ready": ready[0], "sequencer_state": actual["summary"]["sequencer_state"]}
    expected = {"schema": INTENT_OBSERVATION_SCHEMA, "intent": intent, "operations_sha256": _hash(operations),
        "source_sha256": _hash(source), "scope_sha256": request["scope_sha256"], "checkpoint": checkpoint,
        "checkpoint_sha256": _hash(checkpoint), "authority_granted": False,
        "closed_inventory_inferred": False, "physical_release_inferred": False}
    _require(request["schema"] == INTENT_REQUEST_SCHEMA and request["time_s"] == when
        and request["deadline_s"] == when+35. and _same(request.get("operational_intent"), expected)
        and _same(request["facts"], _intent_facts(intent, checkpoint)), "native_intent_checkpoint_projection_or_provenance_changed")


def _goal_outcomes(operations, prefix, *, closed=False):
    """Report final receipt-derived goal results independently of AI/physics."""
    releases, retained, inventory = None, None, None
    if closed:
        inventory = prefix["restored_prefix"]["handoff_record"]["inventory"]
        receipts = inventory["release_receipts"]
        _require(_same(receipts, [satellite["receipt"] for satellite in prefix["satellites"]]),
            "native_goal_inventory_receipt_chain_changed")
        releases, retained = len(receipts), 26-len(receipts)
        _require(type(inventory["released_count"]) is int and inventory["released_count"] == releases
            and type(inventory["retained_count"]) is int and inventory["retained_count"] == retained,
            "native_goal_final_inventory_counts_not_receipt_derived")
    checkpoints = {row["summary"]["released_count"] for row in prefix["restored_prefix"]["decisions"]} if closed else set()
    outcomes = []
    for slot in (0, 13):
        intent = operations["deployment_intents"][str(slot)]
        observed = slot in checkpoints
        status = "not_applicable" if intent is None else "not_evaluable"
        actual = None
        if intent is not None and closed and observed:
            status = "met" if _intent_met(intent, releases, retained) else "unmet"
            actual = releases if intent["objective"]["metric"] == "inventory.released_count" else retained
        outcomes.append({"slot": slot, "intent": intent, "checkpoint_observed": observed,
            "status": status, "actual_value": actual})
    active = [row for row in outcomes if row["intent"] is not None]
    return {"schema": "missionos.starship_restored_operational_goal_outcomes.v1",
        "operations_sha256": _hash(operations), "evaluation_point": "closed_inventory",
        "closed_inventory_verified": closed, "actual_released_count": releases, "actual_retained_count": retained,
        "inventory_receipt_chain_sha256": None if inventory is None else _hash(inventory["release_receipts"]),
        "all_goals_met": all(row["status"] == "met" for row in active) if active else None,
        "outcomes": outcomes, "mission_authority_granted": False,
        "physical_success_inferred": False, "ai_work_inferred": False}


def _deployment(governance, prefix, exchanges, operations, *, profile=None, source=None, scope_sha256=None,
                model_decisions=None):
    """Join model receipts to the actual model-controlled checkpoints.

    The default remains the complete legacy decision chain. A composed caller
    may pass a subsequence only after independently verifying every omitted
    non-model stop. The actual prefix is never rewritten or normalized.
    """
    actual_decisions = prefix["restored_prefix"]["decisions"]
    if model_decisions is not None:
        _require(type(model_decisions) is list
            and all(row.get("origin") == "external_callback_not_model_or_approval_proof" for row in model_decisions)
            and model_decisions == [row for row in actual_decisions
                if row.get("origin") == "external_callback_not_model_or_approval_proof"],
            "native_deployment_model_subsequence_changed")
        actual_decisions = model_decisions
    evidence = governance["deployment"]
    _require(evidence.get("schema") == "missionos.starship_restored_deployment_judgments.v1"
        and evidence.get("pending_request") is None
        and all(evidence.get(key) is False for key in ("closed_inventory_inferred", "physical_release_inferred", "native_model_calls_inferred"))
        and len(evidence["decisions"]) == len(actual_decisions),
        "native_deployment_judgment_chain_missing_or_pending")
    native, used = [], set()
    for row, actual in zip(evidence["decisions"], actual_decisions):
        request = row["request"]
        record = exchanges[request["request_id"]]
        _require(_same(request, record["request"]) and request["phase"] == "deployment"
            and row["request_sha256"] == _hash(request) and _same(row["exchange"], record["exchange"])
            and row["actual_slot"] == actual["summary"]["released_count"]
            and row["applied_action"] == actual["action"]
            and row["applied_at_actual_time_s"] == actual["summary"]["time_s"]
            and type(row["fallback_used"]) is bool
            and request["facts"]["released_count"] == row["actual_slot"]
            and request["facts"]["retained_count"] == 26-row["actual_slot"]
            and request["notice_summary"] == operations["deployment_notices"][str(row["actual_slot"])],
            "native_deployment_changed_valid_choice_or_actual_inventory")
        _require(request["allowed_actions"] == ["continue_deployment", "withhold_deployment"],
            "native_deployment_choice_set_changed")
        if operations["schema"] == OPS_INTENT_SCHEMA:
            _require(request["scope_sha256"] == scope_sha256 and contract._hash(scope_sha256),
                "native_intent_deployment_scope_not_registered")
            _deployment_intent(request, actual, prefix, profile, operations, source)
        else:
            _require(request["schema"] == "missionos.starship_restored_decision_request.v1"
                and "operational_intent" not in request, "native_intent_requires_registered_operations_v2")
        pacing = row["pacing"]
        _require(pacing["wall_start_s"] == record["frame"]["started_wall_s"]
            and pacing["wall_deadline_s"] == record["frame"]["wall_deadline_s"]
            and pacing["simulation_start_s"] == request["time_s"]
            and pacing["simulation_deadline_s"] == request["deadline_s"], "native_deployment_reset_original_epoch")
        if row["fallback_used"]:
            _require(actual["action"] == "withhold_deployment"
                and (record["exchange"]["status"] != "valid" or pacing["last_wall_s"] >= pacing["wall_deadline_s"]
                    or actual["summary"]["time_s"] >= request["deadline_s"]), "native_deployment_overrode_valid_in_window_judgment")
        else:
            _require(record["exchange"]["status"] == "valid" and record["exchange"]["action"] == actual["action"],
                "native_deployment_action_not_the_valid_model_choice")
        native.append(not row["fallback_used"] and record["native_choice"])
        used.add(request["request_id"])
    return all(native) and bool(native), used


def _handshake(governance, host, *, context, plan_sha256, source, original, initial, policy, binding):
    handshake = governance["handshake"]
    _require(type(handshake) is dict and set(handshake) == {"ready", "ack"}
        and _same(handshake, host["handshake"]), "native_origin_ready_ack_history_changed")
    ready, ack = handshake["ready"], handshake["ack"]
    anchor = contract.RestoredCandidateSchedule(original).opportunity_anchor_record()
    expected = {"schema": "missionos.starship_restored_origin_ready.v1", "context": context,
        "plan_sha256": plan_sha256, "source_sha256": _hash(source), "profile_sha256": contract.PROFILE_SHA256,
        "origin_file": governance["closed_origin"]["raw"]["path"],
        "source_record_sha256": original.source_record_sha256, "origin_sha256": _hash(original.record()),
        "binding_sha256": _hash(binding), "opportunity_anchor_sha256": _hash(anchor),
        "initial_reservation_sha256": _hash(initial), "integration_policy_sha256": _hash(policy.record()),
        "started_wall_s": binding["started_wall_s"], "nonce": ready.get("nonce")}
    _require(type(ready) is dict and _same(ready, expected) and contract._hash(ready.get("nonce"))
        and admission._number(ready.get("started_wall_s")), "native_origin_ready_not_source_inventory_reservation_bound")
    wanted_ack = {"schema": "missionos.starship_restored_origin_ready_ack.v1", "ready_sha256": _hash(ready),
        "nonce": ready["nonce"], "context": context, "plan_sha256": plan_sha256,
        "source_sha256": _hash(source), "binding_sha256": _hash(binding),
        "initial_reservation_sha256": _hash(initial), "ledger_ready": True}
    _require(_same(ack, wanted_ack), "native_origin_ack_not_the_exact_host_ready_response")


def _signed_json(row):
    _require(type(row) is dict and contract._hash(row.get("signature")) and contract._hash(row.get("sha256"))
        and row["sha256"] == _hash({key: value for key, value in row.items() if key not in ("signature", "sha256")}),
        "native_operator_record_content_hash_changed")


def _return_telemetry_state(raw, craft, outages):
    """Validate generic saved return telemetry without an orbital-only guard.

    The origin contract's equatorial-radius guard is deliberately orbital.
    It must not be applied to descent telemetry above the smaller ellipsoid
    radius at nonzero latitude. A negative trajectory can also end inside the
    surface; the physical checker decides contact validity and success.
    """
    _require(type(raw) is dict and set(raw) == contract.STATE_KEYS, "native_return_complete_eight_field_state_required")
    _require(contract._number(raw["time_s"]) and 0 <= raw["time_s"] <= contract.MAXIMUM_SIMULATION_TIME_S
        and contract._number(raw["propellant_kg"]) and 0 <= raw["propellant_kg"] <= craft.propellant_capacity_kg,
        "native_return_invalid_state_scalar")
    for key, count in (("r_eci_m", 3), ("v_eci_mps", 3), ("q_body_to_eci", 4), ("omega_body_rad_s", 3)):
        _require(type(raw[key]) is list and len(raw[key]) == count and all(contract._number(value) for value in raw[key]),
            "native_return_invalid_state_vector")
    _require(abs(sum(value*value for value in raw["q_body_to_eci"])-1.) <= 1e-8
        and env.norm(raw["r_eci_m"]) > 1., "native_return_invalid_attitude_or_degenerate_position")
    _require(type(raw["engine_states"]) is list and len(raw["engine_states"]) == len(craft.engines)
        and type(raw["flap_angles_rad"]) is list and len(raw["flap_angles_rad"]) == len(craft.aero_panels),
        "native_return_full_actuator_inventory_required")
    for engine, actual in zip(craft.engines, raw["engine_states"]):
        _require(type(actual) is dict and set(actual) == {"throttle", "gimbal_x_rad", "gimbal_y_rad", "available"}
            and type(actual["available"]) is bool and actual["available"] is (engine.name not in outages)
            and all(contract._number(actual[key]) for key in ("throttle", "gimbal_x_rad", "gimbal_y_rad")),
            "native_return_engine_availability_reset_or_invalid_actuator")
    _require(all(contract._number(value) for value in raw["flap_angles_rad"]), "native_return_nonfinite_flaps")
    state = dyn.state_from_dict(raw)
    dyn._validate_pair(state, craft, dyn.Command6DOF(tuple(dyn.EngineCommand() for _ in craft.engines), state.flap_angles_rad))
    return state


def _telemetry(host, original, initial, final, changes, *, context, plan_sha256, source, policy, binding):
    rows = host.get("telemetry")
    _require(type(rows) is list, "native_host_accepted_telemetry_missing")
    prior_time = original.time_s
    prior_received = prior_fuel = None
    controller_started = False
    observations = {}
    craft = dyn.vehicle_from_dict(original.record()["vehicle"])
    outages = {row["engine_name"] for row in original.record()["ascent_settings"]["ascent"]["shutdowns"]
        if row["engine_name"].startswith("ship_")}
    for index, row in enumerate(rows):
        frame, received = row["frame"], row["received_wall_s"]
        state, runtime = frame["actual_state"], frame["runtime_state"]
        started = frame.get("return_controller_started")
        _require(type(started) is bool and (started or not controller_started),
            "native_host_telemetry_controller_flag_invalid_or_rolled_back")
        actual = _return_telemetry_state(state, craft, outages) if started else contract._state(state, craft, outages)
        observation = runtime["observation"]
        _require(frame.get("schema") == "missionos.starship_restored_worker_telemetry.v1"
            and _same(frame.get("context"), context) and frame.get("plan_sha256") == plan_sha256
            and frame.get("source_sha256") == _hash(source) and type(frame.get("sequence")) is int
            and frame["sequence"] == index+1 and frame.get("state_assigned") is False
            and frame.get("integration_policy_sha256") == _hash(policy.record())
            and frame.get("vehicle_sha256") == _hash(original.record()["vehicle"])
            and frame.get("binding_sha256") == runtime.get("reservation_sha256")
            and frame["binding_sha256"] in (_hash(initial), _hash(final))
            and runtime.get("restored_origin_sha256") == _hash(original.record())
            and runtime.get("simulation_time_s") == actual.time_s
            and type(runtime.get("revision")) is int and runtime["revision"] in (0, 1)
            and _same(runtime.get("scope"), binding["registry"] if runtime["revision"] else binding["initial_scope"])
            and type(frame.get("return_controller_started")) is bool
            and type(runtime.get("run_active")) is bool
            and actual.time_s >= prior_time and admission._number(received)
            and (prior_received is None or received >= prior_received)
            and (prior_fuel is None or actual.propellant_kg <= prior_fuel+1e-6),
            "native_host_telemetry_not_source_bound_actual_monotone_state")
        _require(type(frame.get("phase")) is str and (started and frame["phase"] in (
                "deorbit_slew", "deorbit_burn", "ballistic_return", "landing_burn") or not started and frame["phase"] == "orbital_coast"),
            "native_host_telemetry_precontroller_phase_changed")
        if started:
            reserved = initial if frame["binding_sha256"] == _hash(initial) else final
            _require(actual.time_s >= reserved["return_time_s"], "native_host_return_telemetry_before_reserved_time")
        if index == 0:
            _require(_same(state, original.record()["state"]) and runtime["run_active"] is True
                and frame["phase"] == "orbital_coast" and frame["return_controller_started"] is False,
                "native_first_host_telemetry_not_exact_closed_origin")
        elif actual.time_s == prior_time:
            _require(_same(state, rows[index-1]["frame"]["actual_state"]), "native_same_clock_telemetry_reset_actual_state")
        _require(set(observation) == {"state_sha256", "simulation_time_s", "wall_time_s"}
            and observation["state_sha256"] == _hash(state)
            and observation["simulation_time_s"] == actual.time_s
            and admission._number(observation["wall_time_s"])
            and 0 <= received-observation["wall_time_s"] <= 5., "native_host_rewrote_or_accepted_stale_observation_epoch")
        if changes:
            changed = changes[0]["actual_snapshot"]["state"]["time_s"]
            if actual.time_s < changed:
                _require(frame["binding_sha256"] == _hash(initial) and runtime["revision"] == 0,
                    "native_host_observed_future_reservation_before_change")
            elif actual.time_s > changed:
                _require(frame["binding_sha256"] == _hash(final), "native_host_observed_reservation_rollback")
        else:
            _require(runtime["revision"] == 0, "native_host_observed_unapproved_revision")
        observations[_hash(observation)] = frame
        prior_time, prior_received, prior_fuel = actual.time_s, received, actual.propellant_kg
        controller_started = started
    return observations



def _snapshot_history(governance, host, original):
    """Tie every governed precontroller witness to separately pinned host FD history.

    The host checks source/phase/vehicle and unchanged finite actuator state at
    receipt time. This comparison establishes saved consistency, not independent
    physics authentication or authorization of the recorded snapshot.
    """
    accepted = {(_hash(row["frame"]["actual_state"]), row["frame"]["binding_sha256"],
        row["frame"]["phase"], row["frame"]["vehicle_sha256"])
        for row in host["telemetry"] if row["frame"]["return_controller_started"] is False}
    snapshots = []
    for row in governance["decisions"]:
        snapshots.extend((row["request_snapshot"], row["actual_snapshot"]))
    for row in governance["current_origin_changes"]:
        snapshots.append(row["actual_snapshot"])
        if row.get("confirmation_snapshot") is not None:
            snapshots.append(row["confirmation_snapshot"])
    snapshots.append(governance["dispatch"]["actual_snapshot"])
    for snapshot in snapshots:
        _snapshot(snapshot, original)
        _require((_hash(snapshot["state"]), _hash(snapshot["binding"]), snapshot["phase"],
            _hash(snapshot["vehicle"])) in accepted,
            "native_governance_snapshot_not_exact_host_accepted_state_and_reservation")


def _approval(governance, host, original, initial, final, changes, *, context, plan_sha256, source, inhibited, observations):
    bound = governance["approval"]["binding"]
    typed = RestoredApprovalBinding(original, context=context, plan_sha256=plan_sha256,
        expected_source_sha256=source, started_wall_s=bound["started_wall_s"])
    _require(_same(bound, typed.record()), "native_approval_binding_changed")
    actor = governance["approval"]["worker"]
    host_approval, ledger = host.get("approval_host"), host.get("operator_ledger")
    _require(type(ledger) is list and len(ledger) <= 1, "native_operator_request_budget_changed")
    if actor is None:
        _require(not changes and inhibited and not ledger, "native_missing_approval_actor_after_reservation_change")
        return bound, False, False
    _require(actor.get("schema") == "missionos.inflight_worker_runtime_record.v1"
        and _same(actor.get("context"), context) and _same(actor.get("restored_binding"), bound)
        and actor.get("initial_revision") == 0 and type(actor.get("initial_revision")) is int
        and _same(actor.get("initial_scope"), bound["initial_scope"])
        and _same(actor.get("bounds"), bound["bounds"])
        and all(actor.get(key) is False for key in ("host_signature_verified_by_worker", "burn_dispatch_issued", "physical_return_verified")),
        "native_approval_worker_scope_or_claim_changed")
    if host_approval is not None:
        _require(_same(host_approval.get("binding"), bound), "native_host_operator_binding_changed")
    request = actor.get("request_frame")
    if request is None:
        _require(not changes and not ledger and actor.get("status") == "idle"
            and actor.get("revision") == 0 and _same(actor.get("scope"), bound["initial_scope"])
            and actor.get("host_result") is None and actor.get("applied_reservation") is None,
            "native_unrequested_scope_or_reservation_changed")
        return bound, True, False
    _require(request.get("schema") == "missionos.inflight_scope_request.v1"
        and _same(request.get("context"), context) and request.get("action") == "select_next_orbit"
        and request.get("revision") == 0 and type(request.get("revision")) is int
        and request.get("scope_sha256") == _hash(bound["initial_scope"])
        and request.get("fallback") == "keep_reserved_return" and contract._hash(request.get("candidate_sha256"))
        and request.get("simulation_deadline_s") == bound["bounds"]["simulation_deadline_s"]
        and request.get("wall_deadline_s") == bound["bounds"]["wall_deadline_s"],
        "native_amendment_request_not_original_bound_next_opportunity")
    _require(_hash(request["observation"]) in observations
        and 0 <= request["simulation_time_s"]-request["observation"]["simulation_time_s"] <= 5.,
        "native_operator_request_not_a_host_accepted_fd_observation")
    grant = None
    if ledger:
        item = ledger[0]
        ledger_request, receipt, consumption = item["request"], item["receipt"], item["consumption"]
        _signed_json(ledger_request)
        _require(ledger_request.get("request_id") == request["request_id"]
            and ledger_request.get("candidate_sha256") == request["candidate_sha256"]
            and ledger_request.get("mission_id") == context["mission_id"]
            and ledger_request.get("vehicle_id") == context["vehicle_id"]
            and ledger_request.get("observed_state_sha256") == request["observation"]["state_sha256"]
            and ledger_request.get("simulation_deadline_s") == request["simulation_deadline_s"]
            and ledger_request.get("wall_deadline_s") == request["wall_deadline_s"],
            "native_operator_request_not_exact_worker_request")
        if receipt is not None:
            _signed_json(receipt)
            _require(receipt.get("request_sha256") == ledger_request["sha256"]
                and receipt.get("candidate_sha256") == request["candidate_sha256"]
                and receipt.get("executor_command_issued") is False
                and receipt.get("sso_identity_verified") is False,
                "native_operator_response_rebound_or_claimed_execution")
        if consumption is not None:
            _signed_json(consumption)
            _require(receipt is not None and consumption.get("request_sha256") == ledger_request["sha256"]
                and consumption.get("receipt_sha256") == receipt["sha256"]
                and consumption.get("candidate_sha256") == request["candidate_sha256"]
                and type(consumption.get("approved")) is bool and consumption.get("previous_revision") == 0
                and all(consumption.get(key) is False for key in ("scope_applied", "executor_command_issued", "physical_execution_authorized"))
                and consumption.get("requires_executor_check") is True and consumption.get("simulation_only") is True,
                "native_operator_consumption_rebound_or_became_dispatch")
            if consumption["approved"]:
                _require(receipt.get("choice") == "approve" and receipt.get("local_operator_authenticated") is True
                    and receipt["simulation_time_s"] <= consumption["simulation_time_s"] < bound["bounds"]["simulation_deadline_s"]
                    and receipt["wall_time_s"] <= consumption["wall_time_s"] < bound["bounds"]["wall_deadline_s"]
                    and consumption.get("revision") == 1 and _same(consumption.get("scope"), bound["registry"])
                    and consumption.get("scope_sha256") == _hash(bound["registry"]),
                    "native_positive_scope_consumption_lacks_in_window_approve")
                grant = consumption
            _require(_hash(consumption["latest_observation"]) in observations,
                "native_operator_consumption_not_a_host_accepted_current_observation")
            result = actor.get("host_result")
            _require(type(result) is dict and _same(result.get("request"), ledger_request)
                and _same(result.get("consumption"), consumption)
                and result.get("request_frame_sha256") == _hash(request)
                and (host_approval is None or _same(result, host_approval.get("runtime", {}).get("result"))),
                "native_worker_host_ledger_consumption_chain_differs")
    if not changes:
        _require(actor.get("revision") == 0 and _same(actor.get("scope"), bound["initial_scope"])
            and actor.get("applied_reservation") is None, "native_scope_applied_without_actual_change")
        return bound, True, False
    _require(grant is not None and len(changes) == 1, "native_reservation_changed_without_operator_consumption")
    change = changes[0]
    _require(request["candidate_sha256"] == change["admission"]["proposal_sha256"]
        and change["admission"]["accepted"] is True, "native_operator_approved_different_proposal_or_failed_admission")
    confirmed = actor.get("status") == "reservation_confirmed"
    if confirmed:
        applied = actor["applied_reservation"]
        _require(actor.get("revision") == 1 and _same(actor.get("scope"), bound["registry"])
            and applied.get("simulation_time_s") == change["actual_snapshot"]["state"]["time_s"]
            and applied.get("reservation_sha256") == _hash(final)
            and applied.get("consumption_sha256") == grant["sha256"]
            and type(change.get("confirmation_snapshot")) is dict,
            "native_scope_change_missing_later_actual_confirmation")
        confirmation = change["confirmation_snapshot"]
        _snapshot(confirmation, original, binding=final)
        _require(confirmation["state"]["time_s"] > change["actual_snapshot"]["state"]["time_s"]
            and confirmation["state"]["propellant_kg"] <= change["actual_snapshot"]["state"]["propellant_kg"]+1e-6,
            "native_scope_confirmation_did_not_observe_later_actual_state")
        events = [row for row in actor["events"] if row.get("event") == "m3_return_reservation_observed"]
        _require(len(events) == 1 and events[0]["time_s"] == confirmation["state"]["time_s"]
            and events[0].get("matched") is True and events[0].get("reservation_sha256") == _hash(final),
            "native_scope_confirmation_event_missing_or_repeated")
    else:
        _require(inhibited, "native_unconfirmed_reservation_was_dispatched")
    return bound, confirmed, confirmed


def _changes(governance, record, original, artifacts, profile_raw, source, forecasts, operations):
    initial = RestoredReturnRequest(original, contract.RestoredCandidateSchedule(original), "nominal", profile_raw, source).record()
    _require(_same(governance.get("initial_reservation"), {"request": initial, "sha256": _hash(initial)})
        and _same(governance.get("opportunity_anchor"), contract.RestoredCandidateSchedule(original).opportunity_anchor_record()),
        "native_initial_nominal_or_frozen_opportunity_changed")
    changes, witnesses = record.get("reservation_changes"), governance["current_origin_changes"]
    _require(type(changes) is list and len(changes) <= 1 and type(witnesses) is list and len(witnesses) == len(changes),
        "native_reservation_change_budget_or_witness_count")
    final = initial
    for change, witness in zip(changes, witnesses):
        current = _origin(witness["origin"], artifacts, profile_raw, source)
        _continued_origin(current, original)
        snapshot = witness["actual_snapshot"]
        state = _snapshot(snapshot, original, binding=final)
        _require(_same(current.record()["state"], state) and witness["reservation_change_sha256"] == _hash(change)
            and _same(change.get("state"), state) and change.get("time_s") == state["time_s"]
            and _same(change.get("from_binding"), final)
            and change.get("actual_state_assigned") is change.get("dispatch_authorized") is False,
            "native_reservation_change_not_exact_actual_current_state")
        schedule = contract.RestoredCandidateSchedule(current, anchor=contract.RestoredCandidateSchedule(original))
        next_request = RestoredReturnRequest(current, schedule, "next_orbit", profile_raw, source).record()
        _require(_same(change.get("to_binding"), next_request)
            and state["time_s"] <= initial["return_time_s"]-60., "native_reservation_retimed_or_changed_after_booking_guard")
        check = witness["admission"]
        _require(check.get("schema") == "missionos.starship_restored_approval_numerical_admission.v1"
            and check.get("action") == "select_next_orbit" and check.get("accepted") is True
            and check.get("candidate_sha256") == check.get("proposal_sha256")
            and type(check.get("numerical_admission")) is dict, "native_change_missing_fresh_numerical_admission")
        proof = next((row for row in governance["decisions"]
            if type(row.get("candidate")) is dict and row["candidate"].get("proposal_sha256") == check["proposal_sha256"]), None)
        _require(proof is not None, "native_scope_change_has_no_selected_fixed_proposal")
        candidate, notice, resources = _candidate(proof["candidate"], forecasts, profile_raw, source, original)
        expected = _saved_admission(candidate, snapshot, original, notice, resources, "selection")
        # A refreshed candidate can be selected for scope review later; preserve
        # its complete row in the change witness when the numerical proof moved.
        _require("candidate" in witness, "native_change_missing_current_candidate_proof")
        candidate, notice, resources = _candidate(witness["candidate"], forecasts, profile_raw, source, original)
        _operations_row(candidate.record(), state["time_s"], operations, original)
        expected = _saved_admission(candidate, snapshot, original, notice, resources, "selection")
        _require(_same(check["numerical_admission"], expected)
            and expected["accepted"] is True and check["proposal_sha256"] == expected["proposal_sha256"]
            and check.get("forecast_proof_sha256") == candidate.record()["forecast_proof_sha256"]
            and check.get("current_candidate_sha256") == candidate.record()["candidate_sha256"],
            "native_amendment_admission_borrowed_stale_or_other_candidate_proof")
        final = next_request
    _require(_same(record.get("initial_binding"), initial) and _same(record.get("binding"), final),
        "native_return_bindings_do_not_follow_actual_reservation_changes")
    return initial, final, witnesses


def _return_decisions(governance, exchanges, original, forecasts, profile_raw, source, binding, operations, *,
        expected_return_context_projection=False):
    decisions = governance["decisions"]
    permitted = governance["notice_generations"]
    expected_generations = [row["notice"]["generation"] for row in operations["notice_schedule"]]
    _require(type(permitted) is list and all(type(value) is int and value >= 0 for value in permitted)
        and permitted == sorted(set(permitted)) and permitted == expected_generations
        and type(decisions) is list and len(decisions) <= len(permitted), "native_return_decision_generations_changed")
    used, generations, native, scope_requests = set(), set(), [], []
    honored_native_choice = False
    final_candidate_id = governance["dispatch"]["actual_snapshot"]["binding"]["candidate_id"]
    for row in decisions:
        exchange = exchanges[row["request_id"]]
        request, answer = exchange["request"], exchange["exchange"]
        _require(_has_return_context(request) is expected_return_context_projection,
            "native_return_context_opt_in_dropped_or_undeclared")
        now = _snapshot(row["actual_snapshot"], original)["time_s"]
        start = _snapshot(row["request_snapshot"], original)["time_s"]
        action = answer["action"] if answer["status"] == "valid" else None
        _require(request["phase"] == "return" and request["request_id"] not in used
            and row["original_action"] == action and start == request["time_s"] and start <= now
            and (answer["status"] != "valid" or now < request["deadline_s"])
            and set(row["request_candidates"]) == {"nominal", "next_orbit"},
            "native_return_repeated_retimed_or_overwritten_judgment")
        used.add(request["request_id"])
        candidates = {}
        shared_notice = shared_resources = None
        for candidate_id, raw in row["request_candidates"].items():
            candidate, notice, resources = _candidate(raw, forecasts, profile_raw, source, original)
            _operations_row(candidate.record(), start, operations, original)
            check = _saved_admission(candidate, row["request_snapshot"], original, notice, resources, "selection")
            _require(candidate.record()["proposal"]["candidate_id"] == candidate_id,
                "native_decision_time_candidate_mislabeled")
            if shared_notice is not None:
                _require(_same(shared_notice, notice.record()) and _same(shared_resources, resources.record()),
                    "native_decision_time_tools_mixed_generations")
            shared_notice, shared_resources = notice.record(), resources.record()
            candidates[candidate_id] = candidate, check
        generation = shared_notice["generation"]
        registered = next(value for value in operations["notice_schedule"] if value["notice"]["generation"] == generation)
        _require(generation in permitted and generation not in generations
            and request["notice_summary"] == registered["summary"], "native_notice_judgment_retried_or_unregistered_summary")
        generations.add(generation)
        anchor = governance["opportunity_anchor"]
        revision = int(row["request_snapshot"]["binding"]["candidate_id"] == "next_orbit")
        allowed = ["keep_reserved_return", "wait", "request_deep_reasoning"] if revision else [
            "select_nominal", "keep_reserved_return", "select_next_orbit", "request_scope_amendment", "wait", "request_deep_reasoning"]
        _require(request["allowed_actions"] == allowed, "native_return_choice_set_changed_for_actual_scope")
        amendment = governance["approval"]["worker"]
        amendment_frame = None if amendment is None else amendment.get("request_frame")
        can_request = amendment_frame is None or amendment_frame["simulation_time_s"] >= start
        facts = {"time_s": start, "released_count": original.record()["released_count"],
            "retained_count": original.record()["retained_count"], "sequence_closed": True,
            "authority.revision": revision, "authority.nominal_allowed": True,
            "authority.next_orbit_allowed": revision == 1, "authority.can_request_amendment": can_request,
            "authority.approval_deadline_s": binding["bounds"]["simulation_deadline_s"],
            "notice.generation": generation, "notice.issued_time_s": shared_notice["issued_s"],
            "notice.expiry_time_s": shared_notice["expires_s"], "resources.power_kwh": shared_resources["energy_kwh"],
            "resources.temperature_k": shared_resources["temperature_k"],
            "resources.propellant_loss_kg": shared_resources["modeled_propellant_loss_kg"]}
        for candidate_id, (candidate, check) in candidates.items():
            raw, contact = candidate.record(), candidate.record()["numerical_contact"]
            facts.update({f"candidate.{candidate_id}.return_time_s": raw["proposal"]["return_time_s"],
                f"candidate.{candidate_id}.additional_wait_s": raw["proposal"]["return_time_s"]-anchor["nominal_s"],
                f"candidate.{candidate_id}.feasibility_passed": check["accepted"],
                f"candidate.{candidate_id}.power_kwh": check["resource_accounting"]["energy_at_return_kwh"],
                f"candidate.{candidate_id}.temperature_k": check["resource_accounting"]["temperature_at_return_k"],
                f"candidate.{candidate_id}.propellant_loss_kg": check["resource_accounting"]["modeled_loss_budget_kg"]})
            for key, label in (("event_time_s", "arrival_time_s"), ("point_speed_mps", "contact_speed_mps"),
                    ("tilt_deg", "tilt_deg"), ("propellant_kg", "remaining_propellant_kg"),
                    ("latitude_deg", "latitude_deg"), ("longitude_deg", "longitude_deg")):
                if key in contact:
                    facts[f"candidate.{candidate_id}.{label}"] = contact[key]
        if expected_return_context_projection:
            facts.update(_return_context_facts(row["request_snapshot"], candidates))
        scope = binding["registry"] if revision else binding["initial_scope"]
        _require(_same(request["facts"], facts) and request["scope_sha256"] == _hash(scope),
            "native_model_input_not_decision_time_numeric_tools_or_scope")
        disposition = row["disposition"]
        if action == "wait":
            _require(disposition == "valid_wait_reservation_unchanged" and row["candidate"] is None
                and row["admission"] is None, "native_valid_wait_overwritten_or_dispatched")
            honored_native_choice = honored_native_choice or exchange["native_choice"]
        else:
            candidate, notice, resources = _candidate(row["candidate"], forecasts, profile_raw, source, original)
            _operations_row(candidate.record(), now, operations, original)
            check = _saved_admission(candidate, row["actual_snapshot"], original, notice, resources, "selection")
            _require(_same(check, row["admission"]), "native_choice_admission_not_fresh_actual_state")
            selected = candidate.record()["proposal"]["candidate_id"]
            if action is None:
                _require(selected == "nominal" and disposition in ("unavailable_preserved_preapproved_nominal", "unavailable_not_admitted")
                    and (disposition != "unavailable_preserved_preapproved_nominal" or check["accepted"] and not revision),
                    "native_unavailable_response_manufactured_another_choice")
            else:
                expected = "next_orbit" if action in ("select_next_orbit", "request_scope_amendment") else row["actual_snapshot"]["binding"]["candidate_id"]
                _require(selected == expected, "native_valid_choice_replaced_with_preferred_candidate")
                if not check["accepted"]:
                    _require(disposition == "valid_choice_rejected_not_overwritten", "native_failed_choice_not_retained")
                elif selected == "next_orbit" and not revision:
                    _require(disposition == "scope_amendment_requested", "native_out_of_scope_choice_skipped_operator_review")
                    scope_requests.append(row)
                else:
                    _require(disposition == "valid_keep_reservation_unchanged", "native_valid_current_choice_overridden")
                if check["accepted"] and selected == final_candidate_id:
                    honored_native_choice = honored_native_choice or exchange["native_choice"]
        native.append(exchange["native_choice"])
    _require(len(scope_requests) <= 1, "native_operator_request_repeated")
    return used, bool(native) and all(native), honored_native_choice


def _operations(value):
    intent_mode = type(value) is dict and value.get("schema") == OPS_INTENT_SCHEMA
    keys = {"schema", "sites", "notice_schedule", "resource_accounting", "deployment_notices"}
    _require(type(value) is dict and set(value) == keys | ({"deployment_intents"} if intent_mode else set())
        and value["schema"] in ("missionos.starship_restored_native_operations.v1", OPS_INTENT_SCHEMA)
        and type(value["sites"]) is list and 1 <= len(value["sites"]) <= 16
        and type(value["notice_schedule"]) is list and value["notice_schedule"]
        and type(value["deployment_notices"]) is dict and set(value["deployment_notices"]) == {"0", "13"}
        and all(type(text) is str and len(text) <= 600 for text in value["deployment_notices"].values()),
        "native_expected_operations_not_registered")
    sites = []
    for row in value["sites"]:
        typed = admission.RestoredSitePolicy(**{key: row[key] for key in (
            "site_id", "latitude_deg", "longitude_deg", "radius_m", "opens_s", "closes_s")})
        _require(_same(row, typed.record()), "native_registered_site_policy_shape_changed")
        sites.append(row["site_id"])
    _require(len(sites) == len(set(sites)), "native_registered_site_ids_duplicate")
    generations, clocks = [], []
    for row in value["notice_schedule"]:
        _require(type(row) is dict and set(row) == {"notice", "summary"}
            and type(row["summary"]) is str and len(row["summary"]) <= 600, "native_registered_notice_shape")
        notice = row["notice"]
        typed = admission.RestoredOperationalNotice(notice["generation"], notice["issued_s"], notice["expires_s"],
            tuple(notice["available_site_ids"]))
        _require(_same(typed.record(), notice) and set(notice["available_site_ids"]) <= set(sites),
            "native_registered_notice_borrowed_unknown_area")
        generations.append(notice["generation"])
        clocks.append(notice["issued_s"])
    _require(generations == sorted(set(generations)) and clocks == sorted(set(clocks)),
        "native_registered_notice_generations_or_issue_times_not_increasing")
    policy = admission.RestoredAdmissionPolicy().record()["waiting"]
    resources = {"epoch": "closed_origin", "initial_energy_kwh": policy["initial_energy_kwh"],
        "power_kw": policy["power_kw"], "initial_temperature_k": policy["initial_temperature_k"],
        "warming_k_per_s": policy["warming_k_per_s"], "initial_modeled_propellant_loss_kg": 0.,
        "modeled_loss_kg_per_s": policy["modeled_loss_kg_per_s"]}
    _require(_same(value["resource_accounting"], resources), "native_registered_accounting_changed_fixed_assumptions")
    if intent_mode:
        intents = value["deployment_intents"]
        _require(type(intents) is dict and set(intents) == {"0", "13"}, "native_registered_intent_slots_invalid")
        identities = []
        for slot, goal in intents.items():
            if goal is not None:
                _intent(goal, int(slot))
                identities.append((goal["intent_id"], goal["generation"]))
        _require(len(identities) == len(set(identities)), "native_registered_intent_generation_duplicate")
    return value


def _operations_row(row, now, operations, original):
    _require(_same(row["sites"], sorted(operations["sites"], key=lambda value: value["site_id"])),
        "native_candidate_used_unregistered_sites")
    eligible = [value for value in operations["notice_schedule"] if value["notice"]["issued_s"] <= now]
    _require(eligible and _same(row["notice"], eligible[-1]["notice"]), "native_candidate_used_wrong_notice_generation_or_content")
    policy = operations["resource_accounting"]
    elapsed = now-original.time_s
    _require(elapsed >= 0, "native_resource_clock_before_closed_origin")
    expected = admission.RestoredResourceState(row["notice"]["generation"], now,
        policy["initial_energy_kwh"]-elapsed*policy["power_kw"]/3600.,
        policy["initial_temperature_k"]+elapsed*policy["warming_k_per_s"],
        policy["initial_modeled_propellant_loss_kg"]+elapsed*policy["modeled_loss_kg_per_s"])
    _require(_same(row["resources"], expected.record()), "native_candidate_synthetic_resource_accounting_not_actual_epoch")


def verify_restored_native_mission(record, profile, *, expected_context, expected_plan_sha256,
        expected_case_id, expected_source_sha256, expected_settings, expected_control_settings,
        expected_site, expected_goal, expected_return_sites, expected_prefix_horizon_s,
        expected_total_horizon_s, expected_integration_policy, artifacts, host_record_raw,
        expected_host_record_sha256, expected_mode="live", expected_operations=None,
        maximum_host_record_bytes=MAX_RAW_BYTES, expected_return_context_projection=False,
        expected_return_review_policy=None):
    """Validate retained evidence without opening paths or running dynamics.

    The host raw JSON alone may explicitly use an integer input budget up to
    512 MiB. The default remains 128 MiB; artifact, prediction restoration and
    every other saved JSON budget are unchanged. This is not a physical gate.
    """
    result = {"schema": "missionos.starship_restored_native_verification.v1", "passed": False, "issues": [],
        "physical_success": False, "ship_physical_success": False, "booster_physical_success": False,
        "judgment_chain_verified": False, "operator_amendment_consistency_verified": False,
        "confirmed_native_inferences": 0, "hosted_model_attempts": 0, "ai_control_completed": False,
        "all_features_completed": False, "host_signature_verified_by_saved_checker": False,
        "operator_identity_authenticated_by_saved_checker": False, "old_certificate_admitted": False,
        "qualification_refreshed": False, "physical_reexecution": False, "physical_execution": False,
        "scope": "saved physical and orchestration consistency; sparse dynamics and host authentication not independently proved"}
    try:
        _host_record_budget(maximum_host_record_bytes)
        _require(type(expected_return_context_projection) is bool, "native_return_context_opt_in_must_be_bool")
        review_policy = _review_policy(expected_return_review_policy)
        _context(expected_context)
        operations = _operations(expected_operations)
        contract._manifest(expected_source_sha256)
        _require(type(expected_integration_policy) is RestoredIntegrationPolicy
            and expected_integration_policy.subdivisions == 2 and expected_mode in ("live", "fixture")
            and contract._hash(expected_plan_sha256) and contract._hash(expected_host_record_sha256)
            and type(expected_case_id) is str and 0 < len(expected_case_id) <= 100
            and type(expected_prefix_horizon_s) in (int, float) and 3336. < expected_prefix_horizon_s <= 7500.
            and type(expected_total_horizon_s) in (int, float) and expected_total_horizon_s == 16000.,
            "native_expected_scope_or_explicit_policy2_missing")
        _require({"src/runtime/starship_restored_native_verifier.py", "src/runtime/starship_restored_verifier.py",
            "src/runtime/starship_restored_coordinator.py", "src/runtime/starship_restored_integration.py"}
            <= expected_source_sha256.keys(), "native_required_source_manifest_missing")
        if operations["schema"] == OPS_INTENT_SCHEMA:
            _require("src/runtime/starship_restored_intent.py" in expected_source_sha256,
                "native_intent_source_manifest_missing")
        if expected_return_context_projection:
            _require("src/runtime/starship_restored_return_context.py" in expected_source_sha256,
                "native_return_context_source_manifest_missing")
        if review_policy is not None:
            _require(RETURN_REVIEW_SOURCE in expected_source_sha256, "native_return_review_source_manifest_missing")
        _require(type(artifacts) is dict and all(contract._hash(key) and type(value) is bytes for key, value in artifacts.items())
            and sum(len(value) for value in artifacts.values()) <= MAX_ARTIFACT_BYTES,
            "native_artifact_map_budget_or_type")
        profile_raw = artifacts.get(contract.PROFILE_SHA256)
        _require(type(profile_raw) is bytes and sha256(profile_raw).hexdigest() == contract.PROFILE_SHA256
            and _same(_read(profile_raw), profile), "native_fixed_profile_raw_bytes_missing_or_changed")
        _require(type(host_record_raw) is bytes and sha256(host_record_raw).hexdigest() == expected_host_record_sha256,
            "native_external_host_record_pin_changed")
        host = _read_host_record(host_record_raw, maximum_host_record_bytes)
        _review_binding(host, review_policy)
        _require(host.get("schema") == HOST_SCHEMA and _same(host.get("context"), expected_context)
            and host.get("plan_sha256") == expected_plan_sha256
            and host.get("source_sha256") == _hash(expected_source_sha256) and host.get("mode") == expected_mode
            and _same(host.get("integration_policy"), expected_integration_policy.record()),
            "native_host_record_context_source_mode_or_policy_changed")
        settings = state_checks._settings(expected_settings, profile)
        controls = booster_checks._settings(expected_control_settings)
        wanted = {"schema": EXECUTION_SCHEMA, "context": expected_context, "plan_sha256": expected_plan_sha256,
            "case_id": expected_case_id, "source_sha256": expected_source_sha256,
            "profile_sha256": contract.PROFILE_SHA256, "settings_sha256": _hash(expected_settings),
            "control_settings_sha256": _hash(controls), "prefix_horizon_s": expected_prefix_horizon_s,
            "total_horizon_s": expected_total_horizon_s, "initial_return_candidate_id": "nominal",
            "integration_policy": expected_integration_policy.record(), "mode": expected_mode}
        if review_policy is not None:
            wanted.update(return_review_policy=review_policy, return_review_policy_sha256=_hash(review_policy))
        _require(type(record) is dict and set(record) == {"schema", "execution", "prefix", "return", "numerical_integration", "governance"}
            and record["schema"] == SCHEMA and _same(record["execution"], wanted), "native_wrapper_or_execution_binding_changed")
        physical._integration_receipt(record, expected_integration_policy)
        launch = record["prefix"]
        reached, commands, outcome, booster_success, water = physical._prefix_physical(launch, profile,
            settings=settings, controls=controls, expected_settings=expected_settings, expected_site=expected_site,
            expected_goal=expected_goal, expected_return_sites=expected_return_sites,
            expected_prefix_horizon_s=expected_prefix_horizon_s)
        if water is not None:
            result["booster"] = water
        result.update(booster_physical_success=booster_success, verified_ascent_command_samples=commands,
            measured_final_insertion_gate_reached=reached, integration_policy=expected_integration_policy.record())
        if outcome["termination"] != "restored_deployment_handoff":
            _require(record["return"] is record["governance"] is None and launch["restored_prefix"].get("closed") is False
                and not host.get("decision_host", {}).get("requests"), "native_failed_prefix_fabricated_return_or_model_work")
            result.update(passed=True, termination=outcome["termination"])
            if operations["schema"] == OPS_INTENT_SCHEMA:
                result["operational_goal_outcomes"] = _goal_outcomes(operations, launch)
            return result
        original = physical._origin(launch, profile_raw, expected_source_sha256)
        physical._decisions(launch, launch["restored_prefix"]["handoff_record"], profile)
        governance = record["governance"]
        _require(type(governance) is dict and governance.get("schema") == GOVERNANCE_SCHEMA
            and _same(governance.get("integration_policy"), expected_integration_policy.record())
            and all(governance.get(key) is False for key in ("qualification_refreshed", "mission_authority_granted", "physical_execution")),
            "native_governance_scope_or_policy_changed")
        retained_origin = _origin(governance["closed_origin"]["raw"], artifacts, profile_raw, expected_source_sha256)
        _require(_same(retained_origin.record(), original.record())
            and governance["closed_origin"]["origin_sha256"] == _hash(original.record()), "native_closed_origin_does_not_match_actual_prefix")
        forecasts = _forecast_batches(governance["forecast_batches"], artifacts, profile_raw, expected_source_sha256,
            expected_integration_policy, original, host)
        continuation = record["return"]
        _require(type(continuation) is dict, "native_closed_prefix_lost_actual_continuation")
        initial, final, changes = _changes(governance, continuation, original, artifacts, profile_raw, expected_source_sha256, forecasts, operations)
        inhibited = continuation["outcome"]["termination"] == "return_inhibited_by_governance"
        observations = _telemetry(host, original, initial, final, changes, context=expected_context,
            plan_sha256=expected_plan_sha256, source=expected_source_sha256, policy=expected_integration_policy,
            binding=governance["approval"]["binding"])
        _snapshot_history(governance, host, original)
        bound, authority_confirmed, amendment = _approval(governance, host, original, initial, final, changes,
            context=expected_context, plan_sha256=expected_plan_sha256, source=expected_source_sha256, inhibited=inhibited,
            observations=observations)
        _handshake(governance, host, context=expected_context, plan_sha256=expected_plan_sha256, source=expected_source_sha256,
            original=original, initial=initial, policy=expected_integration_policy, binding=bound)
        exchanges, attempted, confirmed = _judgments(governance["decision_client"], host["decision_host"],
            context=expected_context, plan_sha256=expected_plan_sha256, source=expected_source_sha256, mode=expected_mode,
            expected_return_review_policy=review_policy)
        if review_policy is not None:
            result["return_review"] = _review_verification(exchanges, review_policy)
        deployment_native, used = _deployment(governance, launch, exchanges, operations,
            profile=profile, source=expected_source_sha256,
            scope_sha256=_hash({"context": expected_context, "plan_sha256": expected_plan_sha256,
                "source_sha256": expected_source_sha256, "phase": "deployment"}))
        return_used, return_native, honored_return_choice = _return_decisions(governance, exchanges, original, forecasts,
            profile_raw, expected_source_sha256, bound, operations,
            expected_return_context_projection=expected_return_context_projection)
        _require(used.isdisjoint(return_used) and used | return_used == set(exchanges), "native_unaccounted_or_repeated_model_request")
        dispatch = governance["dispatch"]
        _require(type(dispatch) is dict and type(dispatch.get("gate_met")) is bool,
            "native_final_dispatch_gate_missing")
        snapshot = dispatch["actual_snapshot"]
        state = _snapshot(snapshot, original, binding=final)
        if dispatch.get("candidate") is not None:
            candidate, notice, resources = _candidate(dispatch["candidate"], forecasts, profile_raw, expected_source_sha256, original)
            _operations_row(candidate.record(), state["time_s"], operations, original)
            admission_result = _saved_admission(candidate, snapshot, original, notice, resources, "dispatch")
            _require(_same(dispatch.get("admission"), admission_result)
                and candidate.record()["proposal"]["candidate_id"] == final["candidate_id"],
                "native_final_dispatch_admission_borrowed_other_candidate_or_state")
        else:
            _require(dispatch.get("admission") is None, "native_dispatch_without_forecast_claimed_admission")
            admission_result = None
        if dispatch["gate_met"]:
            _require(not inhibited and dispatch.get("refusal_reason") is None and authority_confirmed
                and admission_result is not None and admission_result["accepted"] is True
                and _same(continuation.get("return_start_state"), state)
                and len([event for event in continuation["events"] if event["event"] == "return_requested"
                    and event["time_s"] == state["time_s"]]) == 1,
                "native_return_started_without_actual_first_due_admission_or_confirmed_scope")
        else:
            _require(inhibited and type(dispatch.get("refusal_reason")) is str and dispatch["refusal_reason"]
                and _same(continuation["final_state"], state) and continuation.get("return_controller") is None
                and continuation.get("return_start_state") is None
                and not any(event["event"] == "return_requested" for event in continuation["events"])
                and any(event["event"] == "return_inhibited_by_governance"
                    and event.get("reason") == dispatch["refusal_reason"] for event in continuation["events"]),
                "native_refusal_did_not_preserve_actual_unstarted_negative")
        contact = physical._return_physical(continuation, original, profile, settings, expected_total_horizon_s,
            initial_binding=initial, final_binding=final, reservation_changes=continuation["reservation_changes"],
            allow_governance_stop=inhibited)
        success = contact["contact_envelope_met"]
        chain = deployment_native and return_native
        result.update(passed=True, physical_success=bool(success and booster_success), ship_physical_success=success,
            ship_contact=contact, inventory_closed=True, retained_count=original.record()["retained_count"],
            payload_released_count=original.record()["released_count"], return_candidate_id=final["candidate_id"],
            reservation_change_count=len(changes), judgment_chain_verified=chain,
            operator_amendment_consistency_verified=amendment, hosted_model_attempts=attempted,
            confirmed_native_inferences=confirmed, honored_native_return_choice=honored_return_choice,
            ai_control_completed=bool(chain and honored_return_choice and success and booster_success),
            dispatch_admission_checked=dispatch["gate_met"], termination=continuation["outcome"]["termination"])
        if operations["schema"] == OPS_INTENT_SCHEMA:
            result["operational_goal_outcomes"] = _goal_outcomes(operations, launch, closed=True)
    except saved._Invalid as error:
        result["issues"].append(error.issue)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, ZeroDivisionError, StopIteration, OSError, EOFError) as error:
        result["issues"].append({"code": "invalid_restored_native_record", "detail": str(error)})
    return result
