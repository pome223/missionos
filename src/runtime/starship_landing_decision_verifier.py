"""Independent bounds and stored-evidence checks for one landing-onset choice.

Exact checkpoint equality and canonical digests bind caller-supplied records.
They do not authenticate their source, replay dynamics or admit arrival. Only
a complete finite prediction with all material-point gates may propose a
frozen live choice; the later actual run still needs its ordinary verification.
No producer, controller or dynamics module is imported.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math
import re

from .starship_booster_recovery_verifier import (
    _Invalid as _RecoveryInvalid,
    _arrival,
    _command,
    _continuity,
    _flow_and_centroid,
    _json,
    _observe_handoff,
    _profile_and_config,
    _same_state,
    _state,
)
from .starship_actual_recovery_planning_verifier import _validate_context

CONFIG = {
    "maximum_local_calls": 4,
    "maximum_horizon_s": 40.0,
    "maximum_steps_per_call": 400,
    "allowed_delays_s": [0.0, 0.5],
    "runtime_preview_calls": 0,
    "baseline_equivalence_required": True,
    "predicted_full_handoff_required": True,
    "source_binding_independently_verified": False,
    "physical_execution": False,
    "clock_roundoff_tolerance_s": 1e-9,
}
BUNDLE_SCHEMA = "missionos.starship_frozen_boostback_bundle.v1"
BASELINE_SCHEMA = "missionos.starship_landing_baseline_binding.v1"
DECISION_SCHEMA = "missionos.starship_landing_onset_decision.v1"
COMPARISON_KIND = "exact_common_timestamps_excluding_final_unexecuted_command"
_BUNDLE_FIELDS = {
    "schema",
    "source_run_sha256",
    "source_map_sha256",
    "profile_sha256",
    "catch_profile_sha256",
    "separation_state_sha256",
    "initial_plan",
    "refreshed_plan",
    "refresh_time_s",
    "refresh_state_sha256",
    "refresh_operative_context_sha256",
    "source_binding_is_caller_assertion",
    "source_binding_independently_verified",
    "runtime_short_forecast_calls",
    "runtime_actual_forecast_calls",
    "prediction_is_execution",
    "physical_execution",
}
_BASELINE_FIELDS = {
    "schema",
    "source_run_sha256",
    "source_map_sha256",
    "profile_sha256",
    "catch_profile_sha256",
    "physical_configuration_sha256",
    "origin_context_sha256",
    "origin_state_sha256",
    "baseline_physical_checkpoints_sha256",
    "saved_physical_checkpoints_sha256",
    "baseline_checkpoint_count",
    "saved_checkpoint_count",
    "matched",
    "comparison_kind",
    "source_binding_is_caller_assertion",
    "source_authentication_independently_verified",
    "dynamics_replayed",
    "prediction_is_execution",
}


class _Invalid(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise _Invalid(reason)


def digest(value):
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and abs(value) <= 1e12


def _hash(value):
    return type(value) is str and re.fullmatch("[0-9a-f]{64}", value) is not None


def _configuration():
    # Import the independent specification lazily; the run checker calls this
    # module lazily too. This never reads a producer's configuration.
    from .starship_constrained_recovery_verifier import _CONFIGURATIONS

    return _CONFIGURATIONS["constrained_return_development_v10"]


def operative_context(snapshot):
    value = deepcopy(snapshot["context"])
    value.pop("plan")
    value["conditioned_reference"].pop("diagnostics")
    value["conditioned_reference"].pop("frame_diagnostics")
    return value


def _source(source_run, profile, catch, source_map):
    _require(
        type(source_map) is dict
        and 1 <= len(source_map) <= 256
        and all(
            type(key) is str
            and 1 <= len(key) <= 256
            and not key.startswith("/")
            and ".." not in key.split("/")
            and _hash(value)
            for key, value in source_map.items()
        ),
        "invalid_bounded_source_map",
    )
    record = source_run.get("recovery_record") if type(source_run) is dict else None
    points = record.get("checkpoints") if type(record) is dict else None
    _require(type(points) is list and 2 <= len(points) <= 12002, "missing_saved_source_checkpoints")
    _json(source_run, maximum_nodes=32_000_000)
    _json(source_map)
    _profile_and_config(profile, catch)
    _require(
        source_run.get("guidance_policy") == "constrained_return_development_v11"
        and source_run.get("scenario") == "booster_return"
        and source_run.get("body_id") == "booster"
        and source_run["recovery_record"].get("policy_id") == source_run["guidance_policy"],
        "unexpected_saved_transport_planning_source",
    )
    return points


def _origin(snapshot, profile, catch):
    _json(snapshot)
    _state(snapshot["state"], profile)
    _validate_context(snapshot, snapshot["state"], profile, catch, _configuration())
    _require(
        snapshot["schema"] == "missionos.starship_actual_recovery_context.v2"
        and snapshot["context"]["phase"] == "recovery_entry_coast",
        "not_actual_coast_origin",
    )
    return snapshot["state"]["time_s"]


def _physical_point(point):
    return {key: point[key] for key in ("time_s", "phase", "state", "command", "com_rate_body_mps")}


def _centroid_rate_arithmetic(state, profile):
    mass, _, _, flow = _flow_and_centroid(state, profile)
    booster = profile["booster"]
    # Differentiate c(f)=c_dry+(c_tank-c_dry)*f/(dry+f), then
    # multiply by measured df/dt. Retain this finite evaluation order:
    # reassociating all factors before division can differ by two ulps.
    derivative = (booster["tank_center_z_m"] - booster["dry_com_z_m"]) * (
        booster["dry_mass_kg"] / mass**2
    )
    return [0.0, 0.0, derivative * (-flow)]


def _forecast(forecast, snapshot, profile, catch, delay):
    record = forecast.get("recovery_record") if type(forecast) is dict else None
    points = record.get("checkpoints") if type(record) is dict else None
    _require(type(points) is list and 2 <= len(points) <= 401, "invalid_finite_forecast_history")
    _json(forecast, maximum_nodes=32_000_000)
    meta, outcome = forecast["forecast_metadata"], forecast["outcome"]
    request = meta["request"]
    now = snapshot["state"]["time_s"]
    _require(
        type(meta) is dict
        and meta.get("schema") == "missionos.starship_actual_recovery_forecast.v1"
        and meta.get("failure") is None
        and meta.get("final_context_error") is None
        and meta.get("controller_error") is None
        and meta.get("prediction_is_execution") is False
        and meta.get("production_policy_admitted") is False
        and meta.get("origin_context_sha256") == digest(snapshot)
        and type(request) is dict
        and set(request)
        == {
            "origin_context_sha256",
            "duration_s",
            "maximum_integration_steps",
            "wall_deadline_monotonic_s",
        }
        and request["origin_context_sha256"] == digest(snapshot)
        and type(request["maximum_integration_steps"]) is int
        and request["maximum_integration_steps"] == 400
        and _number(request["duration_s"])
        and request["duration_s"] == 40.0
        and _number(request["wall_deadline_monotonic_s"])
        and request["wall_deadline_monotonic_s"] > 0
        and type(outcome["integration_steps"]) is int
        and outcome["integration_steps"] == len(points) - 1
        and outcome.get("physical_execution") is False
        and outcome.get("mission_completed") is False,
        "unbound_or_failed_local_forecast",
    )
    _require(
        meta.get("landing_local_clock")
        == {
            "roundoff_tolerance_s": 1e-9,
            "maximum_horizon_s": 40.0,
            "maximum_integration_steps": 400,
            "macrostep_preserved_within_clock_roundoff_only": True,
        },
        "unbounded_local_clock_roundoff",
    )
    _same_state(points[0]["state"], snapshot["state"], "landing_decision")
    _same_state(forecast["initial_state"], snapshot["state"], "landing_decision")
    _same_state(forecast["booster_separation_state"], snapshot["state"], "landing_decision")
    _same_state(
        forecast["recovery_record"]["input_separation_state"], snapshot["state"], "landing_decision"
    )
    expected_configuration = (
        _configuration()
        if delay is None
        else {**_configuration(), "landing_onset_decision": CONFIG}
    )
    _require(
        forecast.get("scenario") == "booster_return"
        and forecast.get("body_id") == "booster"
        and forecast["recovery_record"].get("policy_id") == forecast["guidance_policy"]
        and forecast["recovery_record"].get("guidance_configuration") == expected_configuration,
        "local_forecast_changed_physical_law",
    )
    prior = None
    for point in points:
        state = point["state"]
        _state(state, profile)
        _require(
            point["time_s"] == state["time_s"]
            and now <= state["time_s"] <= now + 40.0 + 1e-9
            and point["phase"] in ("recovery_entry_coast", "recovery_landing_13"),
            "forecast_clock_or_phase",
        )
        _command(point["command"], point["phase"], state, profile)
        rate = _centroid_rate_arithmetic(state, profile)
        _require(point["com_rate_body_mps"] == rate, "forecast_com_rate_arithmetic")
        if prior is not None:
            _require(state["time_s"] - prior["time_s"] <= 0.250000001, "missing_local_macrostep")
            _continuity(prior, state, profile)
        prior = state
    _same_state(points[-1]["state"], forecast["final_state"], "landing_decision")
    _require(
        all(point["command"] is not None for point in points[:-1])
        and points[-1]["command"] is None,
        "unexecuted_terminal_command",
    )
    end = forecast["final_state"]["time_s"]
    _require(
        outcome["end_time_s"] == end
        and outcome["phase"] == points[-1]["phase"]
        and outcome["start_time_s"] == snapshot["context"]["start_time_s"]
        and abs(outcome["duration_s"] - (end - outcome["start_time_s"])) <= 1e-8,
        "continuation_outcome_clock",
    )
    action = forecast["recovery_record"].get("landing_onset_decision")
    if delay is None:
        _require(
            forecast["guidance_policy"] == "constrained_return_development_v10" and action is None,
            "baseline_must_preserve_default_trigger",
        )
    else:
        _require(
            _number(delay)
            and delay in (0.0, 0.5)
            and forecast["guidance_policy"] == "constrained_return_development_v12"
            and type(action) is dict
            and set(action)
            == {
                "schema",
                "delay_s",
                "origin_time_s",
                "scheduled_onset_s",
                "runtime_preview_calls",
                "actual_state_assigned",
            }
            and action["schema"] == "missionos.starship_landing_onset_forecast_action.v1"
            and _number(action["delay_s"])
            and action["delay_s"] == delay
            and action["origin_time_s"] == now
            and action["scheduled_onset_s"] == now + delay
            and type(action["runtime_preview_calls"]) is int
            and action["runtime_preview_calls"] == 0
            and action["actual_state_assigned"] is False,
            "unbounded_local_onset_action",
        )
    return points


def _baseline(binding, snapshot, source_run, baseline, profile, catch, source_map):
    source = _source(source_run, profile, catch, source_map)
    _origin(snapshot, profile, catch)
    _require(
        snapshot["context"]["plan"] == source_run["recovery_record"]["plans"][-1]["plan"],
        "coast_origin_changed_frozen_selected_plan",
    )
    points = _forecast(baseline, snapshot, profile, catch, None)
    actual = [_physical_point(point) for point in points[:-1]]
    times = [point["time_s"] for point in actual]
    _require(len(set(times)) == len(times), "duplicate_baseline_timestamps")
    # Every saved command at the exact interval is included. The final stored
    # observation has no integrated command and is explicitly excluded.
    saved = [_physical_point(point) for point in source if times[0] <= point["time_s"] <= times[-1]]
    expected = {
        "schema": BASELINE_SCHEMA,
        "source_run_sha256": digest(source_run),
        "source_map_sha256": digest(source_map),
        "profile_sha256": digest(profile),
        "catch_profile_sha256": digest(catch),
        "physical_configuration_sha256": digest(_configuration()),
        "origin_context_sha256": digest(snapshot),
        "origin_state_sha256": digest(snapshot["state"]),
        "baseline_physical_checkpoints_sha256": digest(actual),
        "saved_physical_checkpoints_sha256": digest(saved),
        "baseline_checkpoint_count": len(actual),
        "saved_checkpoint_count": len(saved),
        "matched": True,
        "comparison_kind": COMPARISON_KIND,
        "source_binding_is_caller_assertion": True,
        "source_authentication_independently_verified": False,
        "dynamics_replayed": False,
        "prediction_is_execution": False,
    }
    _require(
        type(binding) is dict
        and set(binding) == _BASELINE_FIELDS
        and type(binding["baseline_checkpoint_count"]) is int
        and type(binding["saved_checkpoint_count"]) is int
        and binding["matched"] is True
        and binding["source_binding_is_caller_assertion"] is True
        and all(
            binding[key] is False
            for key in (
                "source_authentication_independently_verified",
                "dynamics_replayed",
                "prediction_is_execution",
            )
        )
        and binding == expected
        and actual == saved
        and 0 < len(actual) <= 400,
        "baseline_not_exact_saved_continuation",
    )
    return expected


def _bundle(bundle, source_run, profile, catch, initial_state, source_map):
    _source(source_run, profile, catch, source_map)
    _json(bundle)
    _state(initial_state, profile)
    record = source_run["recovery_record"]
    receipt, plans = record["actual_planning_receipt"], record["plans"]
    origin = receipt["origin_context"]
    _require(
        receipt["schema"] == "missionos.starship_actual_recovery_shooting.v3"
        and receipt["baseline_equivalence"]["matched"] is True
        and len(plans) == 3,
        "bundle_requires_saved_selected_transport_plan",
    )
    expected = {
        "schema": BUNDLE_SCHEMA,
        "source_run_sha256": digest(source_run),
        "source_map_sha256": digest(source_map),
        "profile_sha256": digest(profile),
        "catch_profile_sha256": digest(catch),
        "separation_state_sha256": digest(initial_state),
        "initial_plan": plans[0]["plan"],
        "refreshed_plan": plans[-1]["plan"],
        "refresh_time_s": origin["state"]["time_s"],
        "refresh_state_sha256": digest(origin["state"]),
        "refresh_operative_context_sha256": digest(operative_context(origin)),
        "source_binding_is_caller_assertion": True,
        "source_binding_independently_verified": False,
        "runtime_short_forecast_calls": 0,
        "runtime_actual_forecast_calls": 0,
        "prediction_is_execution": False,
        "physical_execution": False,
    }
    _same_state(initial_state, record["input_separation_state"], "landing_decision")
    _require(
        type(bundle) is dict
        and set(bundle) == _BUNDLE_FIELDS
        and type(bundle["runtime_short_forecast_calls"]) is int
        and type(bundle["runtime_actual_forecast_calls"]) is int
        and bundle["source_binding_is_caller_assertion"] is True
        and all(
            bundle[key] is False
            for key in (
                "source_binding_independently_verified",
                "prediction_is_execution",
                "physical_execution",
            )
        )
        and bundle == expected,
        "unbound_frozen_boostback_bundle",
    )
    return expected


def _result():
    return {
        "schema": "missionos.starship_landing_decision_verification.v1",
        "passed": False,
        "issues": [],
        "caller_source_record_bound": False,
        "baseline_common_checkpoints_match": False,
        "predicted_full_handoff_consistent": False,
        "source_authentication_independently_verified": False,
        "dynamics_replayed": False,
        "arrival_admitted": False,
        "support_admitted": False,
        "mission_completed": False,
        "physical_execution": False,
        "model_value_established": False,
    }


def verify_frozen_boostback_bundle(
    bundle, profile, catch_config, initial_state, *, source_run, source_map
):
    result = _result()
    try:
        _bundle(bundle, source_run, profile, catch_config, initial_state, source_map)
        result.update(passed=True, caller_source_record_bound=True)
    except (
        _RecoveryInvalid,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        OverflowError,
        RecursionError,
    ):
        result["issues"].append("Invalid frozen source/plan/profile/context binding")
    return result


def verify_landing_baseline(
    binding, snapshot, source_run, baseline, profile, catch_config, *, source_map
):
    result = _result()
    try:
        _baseline(binding, snapshot, source_run, baseline, profile, catch_config, source_map)
        result.update(
            passed=True, caller_source_record_bound=True, baseline_common_checkpoints_match=True
        )
    except (
        _RecoveryInvalid,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        OverflowError,
        RecursionError,
    ):
        result["issues"].append("Invalid exact common-command baseline comparison")
    return result


def _decision(
    decision,
    profile,
    catch,
    *,
    source_run,
    baseline,
    prediction,
    source_map,
    expected_origin_context=None,
):
    _json(decision)
    fields = {
        "schema",
        "origin_time_s",
        "origin_context",
        "origin_state_sha256",
        "origin_operative_context_sha256",
        "delay_s",
        "scheduled_onset_s",
        "predicted_handoff_state_sha256",
        "prediction_sha256",
        "predicted_handoff_observation_sha256",
        "baseline_binding",
        "runtime_preview_calls",
        "prediction_is_execution",
        "arrival_admitted",
        "support_admitted",
        "physical_execution",
    }
    _require(
        type(decision) is dict
        and set(decision) == fields
        and decision["schema"] == DECISION_SCHEMA,
        "invalid_closed_landing_decision",
    )
    snapshot = decision["origin_context"]
    now = _origin(snapshot, profile, catch)
    _require(
        expected_origin_context is None or snapshot == expected_origin_context,
        "decision_origin_changed_expected_capsule",
    )
    delay = decision["delay_s"]
    _require(
        _number(delay)
        and delay in (0.0, 0.5)
        and _number(decision["origin_time_s"])
        and decision["origin_time_s"] == now
        and _number(decision["scheduled_onset_s"])
        and decision["scheduled_onset_s"] == now + delay
        and decision["origin_state_sha256"] == digest(snapshot["state"])
        and decision["origin_operative_context_sha256"] == digest(operative_context(snapshot))
        and type(decision["runtime_preview_calls"]) is int
        and decision["runtime_preview_calls"] == 0
        and all(
            decision[key] is False
            for key in (
                "prediction_is_execution",
                "arrival_admitted",
                "support_admitted",
                "physical_execution",
            )
        ),
        "decision_schedule_origin_or_authority",
    )
    _baseline(
        decision["baseline_binding"], snapshot, source_run, baseline, profile, catch, source_map
    )
    points = _forecast(prediction, snapshot, profile, catch, delay)
    handoff = prediction["recovery_record"]["handoff"]
    actual = _arrival(prediction["final_state"], profile, catch)
    _observe_handoff(handoff["observation"], actual, prediction["final_state"], catch)
    _require(
        prediction["forecast_metadata"].get("prediction_complete") is True
        and prediction["outcome"]["termination"] == "catch_handoff"
        and points[-1]["phase"] == "recovery_landing_13"
        and actual["eligible"] is True
        and handoff["eligible"] is True
        and handoff["time_s"] == prediction["final_state"]["time_s"]
        and handoff["state"] == prediction["final_state"]
        and decision["predicted_handoff_state_sha256"] == digest(prediction["final_state"])
        and decision["predicted_handoff_observation_sha256"] == digest(handoff["observation"])
        and decision["prediction_sha256"] == digest(prediction),
        "decision_requires_complete_same_time_full_handoff",
    )
    return snapshot


def verify_landing_onset_decision(
    decision,
    profile,
    catch_config,
    *,
    source_run,
    baseline,
    prediction,
    source_map,
    expected_origin_context=None,
):
    result = _result()
    try:
        _decision(
            decision,
            profile,
            catch_config,
            source_run=source_run,
            baseline=baseline,
            prediction=prediction,
            source_map=source_map,
            expected_origin_context=expected_origin_context,
        )
        result.update(
            passed=True,
            caller_source_record_bound=True,
            baseline_common_checkpoints_match=True,
            predicted_full_handoff_consistent=True,
        )
    except (
        _RecoveryInvalid,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        OverflowError,
        RecursionError,
    ):
        result["issues"].append(
            "Invalid source-bound one-time landing choice or complete predicted handoff"
        )
    return result
