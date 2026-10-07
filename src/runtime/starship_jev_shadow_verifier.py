"""Independent record checks for a non-controlling synthetic Jev shadow run.

Public fault frames are derived from verified baseline receipts, without using
worker callback code. Routing records are checked, never queried or replayed.
The saved bundle alone cannot authenticate a provider call or a worker process.
"""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
import math

from .starship_dispenser_verifier import verify_dispenser_experiment


SCHEMA = "missionos.starship_jev_shadow_bundle.v1"
TRIGGER = "first_decision_after_observed_retry_failure"
EVALUATION_COUNT = 60
TRIGGER_COUNT = 22
SCOPE = (
    "independent public-frame extraction and exact rerun comparison against the "
    "independently checked frozen baseline; routing-record consistency only"
)


class _Invalid(ValueError):
    pass


def _require(condition: bool, path: str) -> None:
    if not condition:
        raise _Invalid(path)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: object) -> str:
    return sha256(_json(value).encode()).hexdigest()


def _equal(actual: object, expected: object, path: str) -> None:
    _require(_json(actual) == _json(expected), path)


def _object(value: object, keys: set[str], path: str) -> dict:
    _require(type(value) is dict and set(value) == keys, path)
    return value


def _integer(value: object, low: int, high: int, path: str) -> None:
    _require(type(value) is int and low <= value <= high, path)


def _finite(value: object, low: float, high: float, path: str) -> None:
    _require(type(value) in (int, float) and math.isfinite(value) and low <= value <= high, path)


def expected_fault_frames(runs: list[dict]) -> list[dict]:
    """Walk completed receipts and emit only the following recorded decision.

    A failed retry at a terminal boundary has no subsequent decision and creates
    no frame. No future sensor samples or hidden world fields are selected.
    """
    frames = []
    for run in runs:
        failure_observed = False
        for step in run["steps"]:
            if failure_observed:
                frames.append(
                    {
                        "kind": "fault_observation",
                        "case_ref": sha256(run["world_id"].encode()).hexdigest(),
                        "step_index": step["index"],
                        "public_history": step["public_history"],
                        "public_budget": step["public_budget"],
                    }
                )
                break
            receipt = step["receipt"]
            if receipt and receipt["action"] == "retry" and receipt["success"] is False:
                failure_observed = True
    return frames


def _verify_routing(
    routing: object, frame: dict, mode: str, attempted_before: int, path: str
) -> dict:
    from src.intelligence.starship_jev_router import build_starship_jev_request

    route_targets = {
        "bounded": "history_rule",
        "need_observation": "observation_collector",
        "human_review": "human",
        "deep_reasoning": "deepseek_proposal_only",
    }
    row = _object(routing, {"route", "target_role", "invocation"}, path + ".keys")
    invocation = _object(
        row["invocation"],
        {
            "schema_version",
            "invocation_kind",
            "mode",
            "provider",
            "requested_model_id",
            "model_id",
            "status",
            "call_attempted",
            "call_succeeded",
            "response_received",
            "complete_response_observed",
            "model_inference_invoked",
            "model_inference_status",
            "latency_ms",
            "public_input_sha256",
            "request_sha256",
            "response_sha256",
            "request_hash_encoding",
            "response_hash_encoding",
            "request_bytes",
            "response_bytes_read",
            "maximum_calls",
            "reserved_call_slot",
            "maximum_request_bytes",
            "maximum_response_bytes",
            "timeout_seconds",
            "retries",
            "redirects",
            "probabilities",
            "confidence",
            "confidence_calibrated",
            "raw_prompt_recorded",
            "raw_response_recorded",
            "would_route_only",
            "approval_granted",
            "dispatch",
            "executor_influenced",
            "model_value_claim",
        },
        path + ".invocation.keys",
    )
    public = {"public_history": frame["public_history"], "public_budget": frame["public_budget"]}
    # Rebuild only the allowlisted wire material. This pure builder has no model
    # call and receives no case reference, selected action, or hidden world.
    request = build_starship_jev_request(**public)
    wire = _json(request).encode()
    _require(len(wire) <= 16 * 1024, path + ".request_size")
    constants = {
        "schema_version": "runtime_invocation_evidence.v1",
        "mode": mode,
        "provider": "typesafe" if mode == "live" else None,
        "requested_model_id": "jev-latest" if mode == "live" else None,
        "invocation_kind": "decision_api" if mode == "live" else "deterministic_fixture",
        "public_input_sha256": _hash(public),
        "request_sha256": sha256(wire).hexdigest(),
        "request_hash_encoding": "canonical_json_utf8",
        "response_hash_encoding": "raw_bytes",
        "request_bytes": len(wire),
        "maximum_calls": TRIGGER_COUNT,
        "maximum_request_bytes": 16 * 1024,
        "maximum_response_bytes": 64 * 1024,
        "timeout_seconds": 15,
        "retries": 0,
        "redirects": 0,
        "confidence_calibrated": False,
        "raw_prompt_recorded": False,
        "raw_response_recorded": False,
        "would_route_only": True,
        "approval_granted": False,
        "dispatch": False,
        "executor_influenced": False,
        "model_value_claim": False,
    }
    for key, expected in constants.items():
        _equal(invocation[key], expected, path + "." + key)
    status = invocation["status"]
    if mode == "fixture":
        _equal(status, "fixture_only", path + ".fixture_status")
        attempted = succeeded = received = complete = False
        route = "bounded"
    else:
        _require(
            status
            in {
                "succeeded",
                "transport_failed",
                "transport_timeout",
                "response_too_large",
                "invalid_response",
                "live_opt_in_required",
                "credential_unavailable",
            },
            path + ".live_status",
        )
        attempted = status not in {"live_opt_in_required", "credential_unavailable"}
        succeeded = status == "succeeded"
        received = status in {"succeeded", "invalid_response", "response_too_large"}
        complete = status in {"succeeded", "invalid_response"}
        route = row["route"] if succeeded else None
        if succeeded:
            _require(type(route) is str and route in route_targets, path + ".route")
    for key, value in {
        "call_attempted": attempted,
        "call_succeeded": succeeded,
        "response_received": received,
        "complete_response_observed": complete,
        "model_inference_invoked": succeeded,
        "reserved_call_slot": attempted_before + 1 if attempted else None,
        "model_inference_status": (
            "confirmed_by_valid_response"
            if succeeded
            else "unconfirmed_after_attempt"
            if attempted
            else "not_invoked"
        ),
    }.items():
        _equal(invocation[key], value, path + "." + key)
    _equal(row["route"], route, path + ".route")
    _equal(row["target_role"], route_targets.get(route), path + ".target_role")
    _finite(invocation["latency_ms"], 0, math.inf, path + ".latency_ms")
    if not attempted:
        _equal(invocation["latency_ms"], 0.0, path + ".unattempted_latency")
    byte_count = invocation["response_bytes_read"]
    _integer(byte_count, 0, 65537, path + ".response_bytes_read")
    if status == "response_too_large":
        _equal(byte_count, 65537, path + ".oversize_response_bytes")
    elif not received:
        _equal(byte_count, 0, path + ".unreceived_response_bytes")
    else:
        _require(byte_count <= 65536, path + ".complete_response_bytes")
    if complete:
        checksum = invocation["response_sha256"]
        _require(
            type(checksum) is str
            and len(checksum) == 64
            and all(char in "0123456789abcdef" for char in checksum),
            path + ".response_sha256",
        )
    else:
        _equal(invocation["response_sha256"], None, path + ".response_sha256")
    if succeeded:
        _require(byte_count > 0, path + ".successful_response_bytes")
        import re

        _require(
            type(invocation["model_id"]) is str
            and re.fullmatch(r"jev-[A-Za-z0-9_.-]{1,64}", invocation["model_id"]) is not None,
            path + ".model_id",
        )
        probabilities = _object(
            invocation["probabilities"], set(route_targets), path + ".probabilities"
        )
        for value in probabilities.values():
            _finite(value, 0, 1, path + ".probability_value")
        _require(
            math.isclose(sum(probabilities.values()), 1, rel_tol=0, abs_tol=0.01),
            path + ".probability_sum",
        )
        _finite(invocation["confidence"], 0, 1, path + ".confidence")
    else:
        for key in ("model_id", "probabilities", "confidence"):
            _equal(invocation[key], None, path + "." + key)
    return {
        "attempted": int(attempted),
        "observed": int(received),
        "route": route or "unavailable",
        "status": status,
    }


def verify_shadow_bundle(bundle: dict) -> dict:
    """Fail closed on changed runs, hidden/future input, authority or call claims."""
    checks = {}
    summary = {}
    reasons = []
    try:
        _object(bundle, {"schema", "baseline", "execution_runs", "shadow"}, "bundle.keys")
        _equal(bundle["schema"], SCHEMA, "bundle.schema")
        baseline = verify_dispenser_experiment(bundle["baseline"])
        _require(baseline.get("verified") is True, "baseline.independent_verification")
        _equal(baseline.get("runs_checked"), 870, "baseline.runs_checked")
        checks["baseline_independent_verification"] = True
        baseline_runs = [
            run
            for run in bundle["baseline"]["policy_runs"]
            if run["split"] == "eval" and run["policy"] == "history_rule"
        ]
        _equal(len(baseline_runs), EVALUATION_COUNT, "baseline.evaluation_count")
        _equal(bundle["execution_runs"], baseline_runs, "execution_runs.exact_baseline_match")
        checks["all_execution_records_match_baseline"] = True
        summary.update(
            evaluation_count=EVALUATION_COUNT,
            baseline_runs_checked=870,
            reruns_checked=EVALUATION_COUNT,
            terminal_match_count=EVALUATION_COUNT,
        )
        shadow = _object(
            bundle["shadow"],
            {
                "mode",
                "trigger",
                "maximum_calls",
                "records",
                "worker",
                "worker_exit_code",
                "source_sha256",
                "physical_execution",
                "used_for_decision",
                "real_time_guarantee",
            },
            "shadow.keys",
        )
        _require(shadow["mode"] in ("fixture", "live"), "shadow.mode")
        _equal(shadow["trigger"], TRIGGER, "shadow.trigger")
        _equal(shadow["maximum_calls"], TRIGGER_COUNT, "shadow.maximum_calls")
        for key in ("physical_execution", "used_for_decision", "real_time_guarantee"):
            _equal(shadow[key], False, "shadow." + key)
        _equal(shadow["worker_exit_code"], 0, "shadow.worker_exit_code")
        worker = _object(
            shadow["worker"],
            {
                "kind",
                "worker_pid",
                "provider_credentials_present",
                "emitted_count",
                "evaluation_count",
            },
            "shadow.worker.keys",
        )
        _equal(worker["kind"], "complete", "shadow.worker.kind")
        _integer(worker["worker_pid"], 1, 2**31 - 1, "shadow.worker.worker_pid")
        _equal(worker["provider_credentials_present"], False, "shadow.worker.credentials")
        _equal(worker["emitted_count"], TRIGGER_COUNT, "shadow.worker.emitted_count")
        _equal(worker["evaluation_count"], EVALUATION_COUNT, "shadow.worker.evaluation_count")
        checks["worker_record_and_claim_boundary"] = True
        # Import only the pure source map, never broker execution or model calls.
        from .starship_jev_shadow import source_hashes

        _equal(shadow["source_sha256"], source_hashes(), "shadow.source_sha256")
        checks["current_repository_source_binding"] = True
        frames = expected_fault_frames(baseline_runs)
        _equal(len(frames), TRIGGER_COUNT, "expected_trigger_count")
        records = shadow["records"]
        _require(type(records) is list and len(records) == len(frames), "shadow.records.count")
        route_counts = Counter()
        status_counts = Counter()
        attempted = 0
        observed = 0
        for index, (record, frame) in enumerate(zip(records, frames)):
            path = f"shadow.records[{index}]"
            _object(record, {"frame", "routing", "frame_sha256"}, path + ".keys")
            _equal(record["frame"], frame, path + ".public_frame")
            _equal(record["frame_sha256"], _hash(frame), path + ".frame_sha256")
            call = _verify_routing(
                record["routing"], frame, shadow["mode"], attempted, path + ".routing"
            )
            attempted += call["attempted"]
            observed += call["observed"]
            route_counts[call["route"]] += 1
            status_counts[call["status"]] += 1
        _require(attempted <= TRIGGER_COUNT, "shadow.provider_call_budget")
        checks["first_failure_public_frames_only"] = True
        checks["routing_record_consistency_and_budget"] = True
        summary.update(
            mode=shadow["mode"],
            trigger_count=len(frames),
            no_trigger_count=EVALUATION_COUNT - len(frames),
            recorded_calls_attempted=attempted,
            recorded_responses_observed=observed,
            route_counts=dict(sorted(route_counts.items())),
            status_counts=dict(sorted(status_counts.items())),
        )
    except (
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        OverflowError,
        OSError,
        AttributeError,
        RecursionError,
    ) as exc:
        # The verifier reports its own paths; malformed input and filesystem
        # exception text may contain private data and are not copied to reports.
        reason = str(exc) if isinstance(exc, _Invalid) else "bundle.malformed_or_source_unavailable"
        reasons.append(reason)
        checks[reason] = False
    return {
        "schema": "missionos.starship_jev_shadow_verification.v1",
        "verified": not reasons,
        "checks": checks,
        "reasons": reasons,
        "summary": summary,
        "scope": SCOPE,
        "baseline_verifier_uses_disclosed_public_policy_reproduction": True,
        "provider_invocation_authenticated": False,
        "worker_process_authenticated": False,
        "physical_execution_verified": False,
        "model_value_evaluated": False,
        "real_time_guarantee": False,
    }
