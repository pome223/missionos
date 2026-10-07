"""Bind external forecast files to compact planning records without dynamics.

Hashes, saved controller inputs and same-time material-point arithmetic are
checked here. Persistence-before-analysis is a caller assertion, not an
independently observed clock. This does not replay a plant, admit a prediction
as execution, authenticate source files or establish model value.
"""
from __future__ import annotations

from copy import deepcopy
import gzip
from hashlib import sha256
import io
import json
import math
import os
from pathlib import Path
import re
import stat

from .starship_actual_recovery_planning_verifier import (
    CONFIG, _score_checkpoint, verify_actual_recovery_prediction,
)
from .starship_booster_recovery_verifier import _Invalid, _arrival, _command, _observe_handoff, _state

SCHEMA = "missionos.starship_actual_forecast_corpus_verification.v1"
MAX_STORED_BYTES = 128*1024*1024
MAX_JSON_BYTES = 512*1024*1024
_MANIFEST_FIELDS = {"artifact_id", "format", "relative_path", "sha256", "raw_json_sha256", "bytes",
                    "persisted_before_analysis"}


class _CorpusError(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise _CorpusError(reason)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value):
    return sha256(_canonical(value)).hexdigest()


def _hash(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(_):
    raise _CorpusError("nonfinite_json")


def _inventory(root):
    _require(root.is_dir() and not root.is_symlink(), "invalid_corpus_directory")
    entries = list(root.iterdir())
    _require(len(entries) <= 1+2*CONFIG["maximum_forecast_calls"]
             and all(not path.is_symlink() and path.is_file() for path in entries), "unexpected_corpus_entry")
    return {path.name for path in entries}


def _read(root, manifest):
    _require(type(manifest) is dict and set(manifest) == _MANIFEST_FIELDS, "invalid_artifact_manifest")
    identity, kind, name = manifest["artifact_id"], manifest["format"], manifest["relative_path"]
    _require(type(identity) is str and re.fullmatch(r"[0-9a-f]{32}", identity) is not None
             and kind in ("json", "json.gz") and type(name) is str and name == identity+"."+kind,
             "unsafe_artifact_path")
    _require(type(manifest["bytes"]) is int and 0 < manifest["bytes"] <= MAX_STORED_BYTES
             and _hash(manifest["sha256"]) and _hash(manifest["raw_json_sha256"])
             and manifest["persisted_before_analysis"] is True, "invalid_artifact_digest")
    path = root/name
    _require(not path.is_symlink(), "symlink_artifact")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as file:
        metadata = os.fstat(file.fileno())
        _require(stat.S_ISREG(metadata.st_mode) and metadata.st_size == manifest["bytes"], "artifact_size_mismatch")
        stored = file.read(MAX_STORED_BYTES+1)
    _require(len(stored) == manifest["bytes"] and sha256(stored).hexdigest() == manifest["sha256"],
             "artifact_digest_mismatch")
    if kind == "json.gz":
        with gzip.GzipFile(fileobj=io.BytesIO(stored)) as compressed:
            raw = compressed.read(MAX_JSON_BYTES+1)
    else:
        raw = stored
    _require(len(raw) <= MAX_JSON_BYTES and sha256(raw).hexdigest() == manifest["raw_json_sha256"],
             "artifact_json_digest_mismatch")
    payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_invalid_constant)
    _require(type(payload) is dict and raw == _canonical(payload), "noncanonical_artifact_json")
    return payload


def _physical_view(points):
    return [{key: point[key] for key in ("time_s", "phase", "state", "command", "com_rate_body_mps")} for point in points]


def _recorded_objective(observation):
    limits, pins = observation["limits"], observation["pins"]
    speed_center = (limits["pin_vertical_speed_min_mps"]+limits["pin_vertical_speed_max_mps"])/2
    speed_half = (limits["pin_vertical_speed_max_mps"]-limits["pin_vertical_speed_min_mps"])/2
    height_center = (limits["pin_clearance_min_m"]+limits["pin_clearance_max_m"])/2
    height_half = (limits["pin_clearance_max_m"]-limits["pin_clearance_min_m"])/2
    residual = [x/limits["horizontal_position_m"] for x in observation["position_error_enu_m"][:2]]
    residual += [pin["relative_velocity_enu_mps"][axis]/limits["pin_horizontal_speed_mps"] for pin in pins for axis in (0, 1)]
    residual += [(pin["relative_velocity_enu_mps"][2]-speed_center)/speed_half for pin in pins]
    residual += [(height-height_center)/height_half for height in observation["pin_height_above_support_m"]]
    residual += [observation["tilt_deg"]/limits["attitude_angle_deg"], observation["body_x_east_angle_deg"]/limits["attitude_angle_deg"],
                 observation["body_rate_rad_s"]/limits["body_rate_rad_s"],
                 max(0., limits["propellant_reserve_kg"]-observation["propellant_kg"])/limits["propellant_reserve_kg"]]
    _require(len(residual) == 14 and all(_number(x) for x in residual), "invalid_raw_objective")
    return sum(x*x for x in residual)


def _raw_forecast(payload, entry, origin, *, terminal_handoff_score=False):
    _require(set(payload) == {"kind", "attempt_index", "parameters", "origin_context_sha256", "request", "forecast",
                             "error_type", "wall_seconds", "prediction_is_execution", "production_policy_admitted"}
             and payload["kind"] == "raw_forecast" and payload["attempt_index"] == entry["attempt_index"]
             and payload["parameters"] == entry["parameters"] and payload["origin_context_sha256"] == _digest(origin["snapshot"])
             and payload["request"] == entry["request"] and payload["prediction_is_execution"] is False
             and payload["production_policy_admitted"] is False and _number(payload["wall_seconds"])
             and payload["wall_seconds"] >= 0, "raw_request_binding_mismatch")
    forecast = payload["forecast"]
    if forecast is None:
        _require(type(payload["error_type"]) is str and payload["error_type"] and entry["status"] == "failed"
                 and entry["error_type"] == payload["error_type"] and entry["integration_steps"] == 0
                 and entry["termination"] == "exception_before_result" and entry["score"] is None,
                 "failed_raw_disposition_mismatch")
        return None
    _require(type(forecast) is dict and payload["error_type"] is None, "invalid_raw_forecast")
    meta, outcome, record = forecast["forecast_metadata"], forecast["outcome"], forecast["recovery_record"]
    _require(meta["schema"] == "missionos.starship_actual_recovery_forecast.v1"
             and meta["origin_context_sha256"] == entry["candidate_context_sha256"]
             and meta["request"] == entry["request"] and meta["prediction_is_execution"] is False
             and meta["production_policy_admitted"] is False and type(meta["prediction_complete"]) is bool
             and (meta["failure"] is None or type(meta["failure"]) is str and meta["failure"]), "raw_metadata_mismatch")
    status = "failed" if meta["failure"] is not None else "completed" if meta["prediction_complete"] else "partial"
    context_error = meta.get("final_context_error")
    _require(context_error is None or type(context_error) is str and context_error == meta["failure"]
             and meta["prediction_complete"] is False, "invalid_raw_final_context_failure")
    final = forecast["final_state"]
    _require(status == entry["status"] and entry["error_type"] == meta["failure"]
             and type(outcome["integration_steps"]) is int and outcome["integration_steps"] == entry["integration_steps"]
             and outcome["termination"] == entry["termination"]
             and meta["prediction_complete"] is (outcome["termination"] in ("surface_contact", "catch_handoff")
                                                and meta["failure"] is None)
             and entry["forecast_end_time_s"] == final["time_s"]
             and entry["forecast_duration_s"] == final["time_s"]-origin["snapshot"]["state"]["time_s"]
             and forecast["initial_state"] == origin["snapshot"]["state"]
             and record["input_separation_state"] == origin["snapshot"]["state"]
             and forecast["contact"] == entry["contact"] and record["handoff"]["eligible"] is entry["handoff_eligible"],
             "raw_disposition_mismatch")
    points = record["checkpoints"]
    _require(type(points) is list and len(points) == entry["integration_steps"]+1 and points
             and points[0]["state"] == origin["snapshot"]["state"] and points[-1]["state"] == final,
             "raw_state_context_mismatch")
    final_context = forecast["final_controller_context"]
    if context_error is not None:
        _require(final_context is None and status == "failed", "raw_final_context_failure_mismatch")
    else:
        _require(type(final_context) is dict and final_context["state"] == final
                 and all(final_context[key] == origin["snapshot"][key]
                         for key in ("schema", "profile_sha256", "catch_profile_sha256", "guidance_configuration_sha256")),
                 "raw_state_context_mismatch")
    profile, catch = origin["profile"], origin["catch_profile"]
    candidates = []
    previous_time = None
    for index, point in enumerate(points):
        _state(point["state"], profile)
        _require(point["time_s"] == point["state"]["time_s"]
                 and (previous_time is None or point["time_s"] > previous_time)
                 and origin["snapshot"]["state"]["time_s"] <= point["time_s"] <= origin["snapshot"]["state"]["time_s"]+entry["request"]["duration_s"]+1e-7,
                 "raw_checkpoint_clock_mismatch")
        previous_time = point["time_s"]
        phase = ({"recovery_powered_entry_prepare": "recovery_powered_rate_settle",
                  "recovery_entry_shutdown_tail": "recovery_entry_coast"}.get(point["phase"], point["phase"]))
        _command(point["command"], phase, point["state"], profile)
        observation = point["navigation"].get("arrival_observation")
        if observation is not None and min(observation["pin_height_above_support_m"]) <= CONFIG["score_window_max_lowest_pin_clearance_m"]:
            _score_checkpoint(point, profile, catch, observation=observation)
            candidates.append((_recorded_objective(observation), point["time_s"], index, "checkpoint_navigation", observation))
    handoff = record["handoff"]["observation"]
    final_arrival = _arrival(final, profile, catch)
    _observe_handoff(handoff, final_arrival, final, catch)
    _require(type(record["handoff"]["eligible"]) is bool and
             (record["handoff"]["eligible"] is False or
              final_arrival["eligible"] is True and outcome["termination"] == "catch_handoff")
             and (outcome["termination"] != "catch_handoff" or record["handoff"]["eligible"] is True),
             "raw_handoff_not_bound_to_actual_terminal_gate")
    if min(handoff["pin_height_above_support_m"]) <= CONFIG["score_window_max_lowest_pin_clearance_m"]:
        _score_checkpoint(points[-1], profile, catch, observation=handoff, observation_source="terminal_handoff_observation")
        candidates.append((_recorded_objective(handoff), points[-1]["time_s"], len(points)-1, "terminal_handoff_observation", handoff))
    if status == "completed":
        if candidates:
            if record["handoff"]["eligible"] is True and terminal_handoff_score:
                best = next(item for item in candidates if item[3] == "terminal_handoff_observation")
            else:
                best = min(candidates, key=lambda item: (item[0], item[1]))
            score = entry["score"]
            _require(type(score) is dict and score["objective"] == best[0] and score["checkpoint_index"] == best[2]
                     and score["checkpoint"] == points[best[2]] and score["checkpoint_sha256"] == _digest(points[best[2]])
                     and score["observation_source"] == best[3] and score["actual_same_time_observation"] == best[4],
                     "score_not_bound_to_best_raw_checkpoint")
        else:
            _require(entry["score"] is None, "unobserved_raw_score")
    else:
        _require(entry["score"] is None, "partial_raw_score")
    return forecast


def _baseline(forecast, reference, receipt, origin):
    reference_policy = ("constrained_return_development_v10" if
                        origin["snapshot"]["schema"] == "missionos.starship_actual_recovery_context.v2"
                        else "constrained_return_development_v8")
    if forecast is None:
        expected = {"checked": False, "matched": False, "reason": "baseline_failed", "reference_is_objective": False}
    else:
        actual = _physical_view(forecast["recovery_record"]["checkpoints"])
        expected = {"checked": False, "matched": False, "reference_is_objective": False,
                    "actual_checkpoint_count": len(actual), "origin_state_sha256": _digest(origin["snapshot"]["state"])}
        if type(reference) is not dict or type(reference.get("recovery_record")) is not dict:
            expected["reason"] = "missing_saved_v8_reference"
        else:
            if reference_policy.endswith("v10"):
                expected.update(expected_reference_policy_id=reference_policy,
                                reference_policy_id=reference.get("guidance_policy"))
            if reference.get("guidance_policy") != reference_policy:
                expected["reason"] = "unexpected_reference_policy"
            else:
                suffix = _physical_view([point for point in reference["recovery_record"]["checkpoints"]
                                         if point["time_s"] >= origin["snapshot"]["state"]["time_s"]])
                actual_hash, expected_hash = _digest(actual), _digest(suffix)
                expected.update(checked=True, matched=actual_hash == expected_hash,
                    reason="exact_physical_checkpoint_suffix" if actual_hash == expected_hash else "baseline_continuation_mismatch",
                    expected_checkpoint_count=len(suffix), actual_checkpoint_sha256=actual_hash,
                    expected_checkpoint_sha256=expected_hash, reference_sha256=_digest(reference))
    _require(receipt["baseline_equivalence"] == expected, "external_baseline_equivalence_mismatch")
    return expected


def verify_forecast_corpus(root, receipt, *, baseline_reference=None, source_sha256=None, executed_run=None):
    """Read an exclusive forecasts directory; return a compact fail-closed verdict."""
    result = {"schema": SCHEMA, "passed": False, "issues": [], "artifact_count": 0,
              "attempted_forecast_count": 0, "corpus_sha256": None, "source_sha256_digest": None,
              "source_binding_verified": False, "hashes_and_canonical_json_verified": False,
              "same_time_score_bound_to_raw_verified": False, "baseline_equivalence_verified": False,
              "selected_complete_forecast_matches_execution": False,
              "execution_comparison_applicable": False,
              "persistence_before_analysis_independently_verified": False, "dynamics_replayed": False,
              "runtime_invocation_independently_verified": False, "physical_execution": False,
              "missionos_dispatch": False, "arrival_admitted": False, "support_admitted": False,
              "model_value_established": False}
    try:
        receipt = json.loads(_canonical(receipt))
        if source_sha256 is not None:
            _require(type(source_sha256) is dict and len(source_sha256) <= 256
                     and all(type(key) is str and _hash(value) for key, value in source_sha256.items()), "invalid_source_digest_map")
            result["source_sha256_digest"] = _digest(source_sha256)
        root = Path(root)
        inventory = _inventory(root)
        entries = receipt["forecasts"]
        _require(type(entries) is list and len(entries) <= CONFIG["maximum_forecast_calls"], "forecast_count_exceeded")
        manifests = [receipt["origin_artifact"]]+[entry[key] for entry in entries for key in ("attempted_artifact", "raw_artifact")]
        identities = [manifest["artifact_id"] for manifest in manifests]
        names = [manifest["relative_path"] for manifest in manifests]
        _require(len(set(identities)) == len(identities) and len(set(names)) == len(names)
                 and inventory == set(names), "corpus_files_missing_duplicated_or_extra")
        origin = _read(root, manifests[0])
        origin_fields = {"kind", "snapshot", "profile", "catch_profile", "guidance_configuration", "baseline_plan",
                         "baseline_reference_sha256", "baseline_reference_is_objective"}
        transported_planning = receipt.get("schema") == "missionos.starship_actual_recovery_shooting.v3"
        if transported_planning:
            origin_fields |= {"baseline_binding", "expected_reference_policy_id", "reference_policy_id",
                              "baseline_binding_is_caller_assertion", "source_binding_independently_verified"}
        _require(set(origin) == origin_fields
                 and origin["kind"] == "origin_context" and origin["baseline_reference_is_objective"] is False
                 and origin["snapshot"] == receipt["origin_context"]
                 and origin["snapshot"]["context"]["plan"] == origin["baseline_plan"]
                 and origin["baseline_reference_sha256"] == receipt["baseline_reference_sha256"]
                 and origin["baseline_reference_sha256"] == (_digest(baseline_reference) if baseline_reference is not None else None),
                 "origin_payload_binding_mismatch")
        if transported_planning:
            _require(origin["baseline_binding"] == receipt["baseline_binding"] == {
                "run_sha256": _digest(baseline_reference), "profile_sha256": _digest(origin["profile"]),
                "catch_profile_sha256": _digest(origin["catch_profile"])}
                and origin["expected_reference_policy_id"] == receipt["expected_reference_policy_id"] ==
                    "constrained_return_development_v10"
                and origin["reference_policy_id"] == receipt["reference_policy_id"] ==
                    baseline_reference["guidance_policy"] == "constrained_return_development_v10"
                and origin["baseline_binding_is_caller_assertion"] is True
                and receipt["baseline_binding_is_caller_assertion"] is True
                and origin["source_binding_independently_verified"] is False
                and receipt["source_binding_independently_verified"] is False,
                "transported_baseline_binding_mismatch")
        previous = None
        if type(baseline_reference) is dict:
            earlier = [point for point in baseline_reference["recovery_record"]["checkpoints"]
                       if point["time_s"] < origin["snapshot"]["state"]["time_s"]]
            previous = earlier[-1] if earlier else None
        selected = None
        baseline_comparison = None
        for index, entry in enumerate(entries, 1):
            _require(type(entry["attempt_index"]) is int and entry["attempt_index"] == index, "attempt_order_mismatch")
            candidate = deepcopy(origin["baseline_plan"])
            _require(type(entry["candidate_request"]) is dict and set(entry["candidate_request"]) == {"burn_axis_enu", "target_velocity_enu_mps"},
                     "invalid_candidate_request")
            candidate.update(entry["candidate_request"])
            snapshot = deepcopy(origin["snapshot"])
            snapshot["context"]["plan"] = candidate
            _require(entry["candidate_plan_sha256"] == _digest(candidate)
                     and entry["candidate_context_sha256"] == _digest(snapshot)
                     and entry["request"]["origin_context_sha256"] == _digest(snapshot), "candidate_context_hash_mismatch")
            attempted = _read(root, entry["attempted_artifact"])
            _require(attempted == {"kind": "attempted", "attempt_index": index, "parameters": entry["parameters"],
                "candidate_plan_sha256": entry["candidate_plan_sha256"], "origin_context_sha256": _digest(origin["snapshot"]),
                "request": entry["request"], "prediction_is_execution": False}, "attempted_payload_binding_mismatch")
            forecast = _raw_forecast(_read(root, entry["raw_artifact"]), entry, origin,
                terminal_handoff_score=receipt["configuration"].get("selection_priority") ==
                    "predicted_handoff_then_same_time_score_then_attempt")
            if index == 1:
                baseline_comparison = _baseline(forecast, baseline_reference, receipt, origin)
            if index == receipt["selected_attempt_index"]:
                selected = candidate
                if executed_run is not None and entry["status"] == "completed":
                    _require(type(executed_run) is dict, "invalid_selected_execution_record")
                    suffix = [point for point in executed_run["recovery_record"]["checkpoints"]
                              if point["time_s"] >= origin["snapshot"]["state"]["time_s"]]
                    _require(_canonical(_physical_view(suffix)) ==
                             _canonical(_physical_view(forecast["recovery_record"]["checkpoints"])),
                             "selected_forecast_execution_mismatch")
                    result["execution_comparison_applicable"] = True
                    result["selected_complete_forecast_matches_execution"] = True
            del forecast
        plan = deepcopy(selected or origin["baseline_plan"])
        plan.update(method="bounded_actual_sixdof_same_time_shooting", actual_recovery_prediction=receipt, admissible=False)
        verify_actual_recovery_prediction(plan, origin["snapshot"]["state"], origin["profile"], origin["catch_profile"],
                                          origin["guidance_configuration"], previous_checkpoint=previous)
        _require(_inventory(root) == inventory, "corpus_inventory_changed")
        result.update(passed=True, artifact_count=len(manifests), attempted_forecast_count=len(entries),
            corpus_sha256=_digest(manifests), hashes_and_canonical_json_verified=True,
            same_time_score_bound_to_raw_verified=True, baseline_equivalence_verified=baseline_comparison is not None,
            baseline_matched=baseline_comparison["matched"] if baseline_comparison is not None else False,
            persistence_before_analysis_asserted=receipt["raw_forecasts_persisted_before_analysis"])
    except _CorpusError as error:
        result["issues"].append(str(error))
    except _Invalid as error:
        result["issues"].append(error.issue["code"])
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError):
        result["issues"].append("invalid_external_forecast_corpus")
    return result
