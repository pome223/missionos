"""Bounded receipt accounting, not inference-quality or fallback-superiority scoring.

Aggregate explicit saved studies only. Hashes identify the evidence and source
manifests; no model prose, raw body, exception text or input path is emitted.
An older invalid response without a recorded cause remains ``unknown``.
"""

from collections import Counter
from hashlib import sha256
import json

PROVIDERS = (("jev", "jev_invocation"), ("deepseek", "llm_invocation"))
STATUSES = frozenset({
    "succeeded", "invalid_response", "request_too_large", "credential_unavailable",
    "not_routed_after_run_end", "transport_timeout", "transport_failed", "cancelled_request",
    "response_too_large", "not_invoked", "not_routed", "invalid_input", "disabled",
    "live_opt_in_required", "procedure_precondition_not_met",
})
VALIDATION_ERRORS = frozenset({
    "response_shape", "json_decode", "model_mismatch", "answer_shape", "answer_type",
    "choice", "probability_keys", "probability_values", "probability_sum", "confidence",
    "completion_shape", "output_limit", "finish_reason", "json", "action",
    "decision_basis", "credential_in_response", "numeric_evidence", "reference_ids",
    "decision_deadline", "response_size", "provider_request_mismatch",
    "decision_input_mismatch", "reference_catalog_mismatch", "unregistered_probe_setting",
    "usage_unavailable", "reasoning_usage_unavailable", "output_budget_exceeded",
    "compared_candidates", "summary", "malformed_response",
})


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()).hexdigest()


def summarize_model_diagnostics(studies):
    """Count receipt facts once per identical study, preserving source identities.

    These counts describe saved receipts, not a fresh verification or a population
    failure-rate estimate. ``valid_responses`` requires all three success flags
    and an attempted call. Missing actor responses are counted separately; they
    do not imply a provider was called. Arbitrary provider labels become unknown.
    """
    evidence = {}
    submitted = duplicates = decisions = missing = malformed = 0
    providers = {
        name: {"receipts": 0, "attempts": 0, "valid_responses": 0,
               "failed_attempts": 0, "status_counts": Counter(),
               "failure_reason_counts": Counter(), "blocked_before_attempt_counts": Counter()}
        for name, _ in PROVIDERS
    }
    for study in studies:
        submitted += 1
        if type(study) is not dict or type(study.get("decisions")) is not list:
            raise ValueError("invalid_model_diagnostics_study")
        fingerprint = _digest(study)
        if fingerprint in evidence:
            duplicates += 1
            continue
        sources = study.get("source_sha256")
        evidence[fingerprint] = {
            "canonical_study_sha256": fingerprint,
            "source_manifest_sha256": _digest(sources) if type(sources) is dict else None,
        }
        for row in study["decisions"]:
            decisions += 1
            if type(row) is not dict:
                malformed += 1
                continue
            response = row.get("response")
            if response is None:
                missing += 1
                continue
            if type(response) is not dict:
                malformed += 1
                continue
            for provider, field in PROVIDERS:
                receipt = response.get(field)
                if receipt is None:
                    continue
                if type(receipt) is not dict:
                    malformed += 1
                    continue
                counts = providers[provider]
                counts["receipts"] += 1
                raw_status = receipt.get("status")
                status = raw_status if type(raw_status) is str and raw_status in STATUSES else "unknown"
                counts["status_counts"][status] += 1
                if receipt.get("call_attempted") is not True:
                    if status not in ("not_invoked", "not_routed"):
                        counts["blocked_before_attempt_counts"][status] += 1
                    continue
                counts["attempts"] += 1
                valid = (status == "succeeded"
                         and receipt.get("call_succeeded") is True
                         and receipt.get("model_inference_invoked") is True)
                if valid:
                    counts["valid_responses"] += 1
                else:
                    counts["failed_attempts"] += 1
                    reason = status
                    if status == "invalid_response":
                        raw_reason = receipt.get("validation_error")
                        reason = (raw_reason if type(raw_reason) is str
                                  and raw_reason in VALIDATION_ERRORS else "unknown")
                    elif status == "succeeded":
                        reason = "inconsistent_success_flags"
                    counts["failure_reason_counts"][reason] += 1
    for counts in providers.values():
        for field in ("status_counts", "failure_reason_counts", "blocked_before_attempt_counts"):
            counts[field] = dict(sorted(counts[field].items()))
    return {
        "schema": "missionos.starship_model_diagnostics.v1",
        "input_studies": submitted,
        "unique_studies": len(evidence),
        "duplicate_studies_ignored": duplicates,
        "decision_records": decisions,
        "missing_decision_responses": missing,
        "malformed_records_or_receipts": malformed,
        "evidence": [evidence[key] for key in sorted(evidence)],
        "providers": providers,
        "scope": "saved_receipt_accounting_only",
        "historical_unknown_causes_inferred": False,
        "raw_response_or_reasoning_retained": False,
        "regression_or_fallback_superiority_score": False,
    }
