"""Mutate saved operational evidence without importing its producer."""
from copy import deepcopy
import json
import math

import pytest

from src.runtime.starship_flight_supervision_verifier import verify_supervision


MU, RADIUS = 3.986004418e14, 6_628_137.
CONTRACT = {"procedure_id": "local_deployment_missing_effect_v1", "allowed_actions": ["hold", "skip_remaining_deployment"],
            "maximum_commands": 1, "decision_expiry_s": 75., "physical_execution_authorized": False}


def evidence(times=(100., 102., 104., 119., 134.)):
    observations, samples = [], []
    for sequence, when in enumerate(times, 1):
        angle = when*math.sqrt(MU/RADIUS**3)
        speed = math.sqrt(MU/RADIUS)
        state = "inhibited" if sequence <= 3 else "skipped"
        observation = {"observation_id": f"run-1-obs-{sequence:04d}", "sequence": sequence,
                       "time_s": when, "phase": "orbital_coast", "payload_released_count": 0,
                       "release_attempt_count": 1, "release_acknowledged": True, "sequencer_state": state,
                       "perigee_altitude_m": 250_000., "dynamic_pressure_pa": 0., "body_rate_rad_s": .001,
                       "propellant_kg": 50_000., "return_deadline_s": 1000.}
        sample = {"time_s": when, "phase": "orbital_coast",
                  "r_eci_m": [RADIUS*math.cos(angle), RADIUS*math.sin(angle), 0.],
                  "v_eci_mps": [-speed*math.sin(angle), speed*math.cos(angle), 0.],
                  "omega_body_rad_s": [.001, 0., 0.], "propellant_kg": 50_000., "dynamic_pressure_pa": 0.,
                  "flight_supervision": {key: observation[key] for key in ("observation_id", "sequence", "release_attempt_count",
                                           "release_acknowledged", "sequencer_state", "payload_released_count")}}
        observations.append(observation)
        samples.append(sample)
    final = deepcopy(samples[-1])
    final["time_s"] = max(250., times[-1]+1.)
    final["phase"] = "landing_burn"
    final["flight_supervision"].pop("observation_id")
    final["flight_supervision"].pop("sequence")
    samples.append(final)
    latest = observations[2]
    response = {"request_id": "run-1", "observation_id": observations[1]["observation_id"],
                "action": "skip_remaining_deployment", "route": "bounded", "jev_invocation": {}, "llm_invocation": {}}
    command = {"command_id": "run-1-skip-1", "request_id": "run-1", "action": "skip_remaining_deployment",
               "time_s": latest["time_s"], "observation_id": latest["observation_id"], "accepted": True,
               "sequencer_state_before": "inhibited", "sequencer_state_after": "skipped", "payload_released_count": 0}
    record = {"schema": "missionos.starship_flight_supervision.v1", "request_id": "run-1",
              "request": {"schema": "missionos.starship_flight_supervision_request.v1", "request_id": "run-1",
                          "observations": deepcopy(observations[:2]), "allowed_actions": ["hold", "skip_remaining_deployment"]},
              "response": response, "rules": [{"time_s": latest["time_s"], "observation_id": latest["observation_id"],
                                                "action": "skip_remaining_deployment", "allowed": True, "reasons": []}],
              "commands": [command], "observations": observations, "status": "effect_observations_recorded",
              "max_decision_age_s": 75., "observation_interval_s": 2., "effect_observation_interval_s": 15.,
              "physical_execution": False, "final_time_s": final["time_s"], "final_sequencer_state": "skipped"}
    run = {"scenario": "deployment_no_effect", "samples": samples, "supervision": record, "satellites": [],
           "events": [{"time_s": 0., "event": "orbit_cutoff_command"},
                      {"time_s": 99., "event": "payload_release_attempt_acknowledged"},
                      {"time_s": latest["time_s"], "event": "deployment_skip_command", "command_id": "run-1-skip-1"}],
           "outcome": {"payload_released_count": 0}}
    profile = {"payload": {"interval_s": 15.}, "ship": {"return_reserve_kg": 28_000.},
               "guidance": {"coast_before_return_s": 1000.}}
    return run, profile


def verify(run, profile):
    return verify_supervision(run, profile, expected_request_id="run-1", expected_contract=CONTRACT)


COLLECTION_CONTRACT = {**CONTRACT, "procedure_id": "local_deployment_status_collection_v1",
                       "maximum_observation_requests": 1, "observation_kind": "deployment_status",
                       "collection_does_not_extend_deadline": True, "mode": "fixture",
                       "maximum_jev_calls": 0, "maximum_deepseek_calls": 0}


def collection_evidence():
    run, profile = evidence((100., 102., 104., 106., 108., 123., 138.))
    record = run["supervision"]
    for row, sample in zip(record["observations"][:5], run["samples"][:5]):
        row["sequencer_state"] = sample["flight_supervision"]["sequencer_state"] = "inhibited"
    rows = record["observations"]
    record.update(observation_collection_allowed=True,
        response={**record["response"], "action": "hold", "route": "need_observation"},
        collection={"collection_id": "run-1-status-1", "kind": "deployment_status", "issued_time_s": 104.,
                    "request_observation_id": rows[2]["observation_id"], "accepted": True, "status": "report_received",
                    "received_observation_id": rows[3]["observation_id"], "completed_time_s": 106.,
                    "actuator_operation": False, "physical_execution": False},
        followup_request={**record["request"], "observations": deepcopy(rows[2:4])},
        followup_response={"request_id": "run-1", "observation_id": rows[3]["observation_id"],
                           "action": "skip_remaining_deployment", "route": "bounded", "jev_invocation": {}, "llm_invocation": {}})
    for item in (*record["commands"], *record["rules"]):
        item.update(time_s=108., observation_id=rows[4]["observation_id"])
    run["events"][-1]["time_s"] = 108.
    for response in (record["response"], record["followup_response"]):
        for role in ("jev", "llm"):
            response[role+"_invocation"] = {"schema_version": "runtime_invocation_evidence.v1", "mode": "fixture",
                "call_attempted": False, "call_succeeded": False, "response_received": False,
                "complete_response_observed": False, "model_inference_invoked": False,
                "reserved_call_slot": None, "maximum_instance_calls": 1, "retries": 0,
                "status": "not_routed" if role == "llm" else "fixture_only"}
    return run, profile


def test_collection_is_checked_against_saved_states_and_separate_approval():
    run, profile = collection_evidence()
    result = verify_supervision(run, profile, expected_request_id="run-1", expected_contract=COLLECTION_CONTRACT)
    assert result["passed"] and result["observed_effect"] and result["observation_collection_completed"], result
    assert verify(run, profile)["passed"] is False


@pytest.mark.parametrize("field,value", [("completed_time_s", 104.), ("received_observation_id", "run-1-obs-0003"),
    ("actuator_operation", True), ("kind", "retry_motor"), ("collection_id", "run-1-status-2")])
def test_forged_read_receipt_cannot_claim_a_fresh_report(field, value):
    run, profile = collection_evidence()
    run["supervision"]["collection"][field] = value
    assert verify_supervision(run, profile, expected_contract=COLLECTION_CONTRACT)["passed"] is False


@pytest.mark.parametrize("field,value", [("call_attempted", True), ("call_attempted", 1),
    ("maximum_instance_calls", 2), ("retries", 1), ("model_inference_invoked", True), ("mode", "live")])
def test_collection_provider_evidence_rejects_forged_budget_or_fixture_inference(field, value):
    run, profile = collection_evidence()
    run["supervision"]["followup_response"]["jev_invocation"][field] = value
    result = verify_supervision(run, profile, expected_contract=COLLECTION_CONTRACT)
    assert not result["passed"] and result["issues"][0]["code"] == "provider_budget", result


def test_live_collection_counts_attempts_and_rejects_unrouted_reasoning():
    run, profile = collection_evidence()
    live = {**COLLECTION_CONTRACT, "mode": "live", "maximum_jev_calls": 2, "maximum_deepseek_calls": 1}
    for response in (run["supervision"]["response"], run["supervision"]["followup_response"]):
        for role in ("jev", "llm"):
            response[role+"_invocation"]["mode"] = "live"
        response["jev_invocation"].update(call_attempted=True, call_succeeded=True, response_received=True,
            complete_response_observed=True, model_inference_invoked=True, reserved_call_slot=1, status="succeeded")
    result = verify_supervision(run, profile, expected_contract=live)
    assert result["passed"] and result["recorded_jev_attempt_count"] == 2 and result["recorded_deepseek_attempt_count"] == 0, result
    run["supervision"]["response"]["llm_invocation"].update(call_attempted=True, call_succeeded=True, response_received=True,
        complete_response_observed=True, model_inference_invoked=True, reserved_call_slot=1, status="succeeded")
    assert not verify_supervision(run, profile, expected_contract=live)["passed"]


def rejected(run, profile, code=None):
    result = verify(run, profile)
    assert result["passed"] is False, result
    assert result["observed_effect"] is False
    assert result["issues"] and len(result["issues"]) <= 1
    if code:
        assert result["issues"][0]["code"] == code, result
    json.dumps(result, allow_nan=False)


def hold_evidence(reason="hold_requested", status="held", response=True):
    run, profile = evidence()
    record = run["supervision"]
    record["commands"] = []
    record["status"] = status
    record["final_sequencer_state"] = "inhibited"
    if response:
        record["response"]["action"] = "hold"
    else:
        record["response"] = None
    record["rules"][0].update(action="hold", allowed=False, reasons=[reason])
    for observation in record["observations"]:
        observation["sequencer_state"] = "inhibited"
    for sample in run["samples"]:
        sample["flight_supervision"]["sequencer_state"] = "inhibited"
    run["events"] = run["events"][:-1]
    return run, profile


def test_two_subsequent_observations_verify_sequencer_effect_only():
    result = verify(*evidence())
    assert result["passed"] is True, result
    assert result["observed_effect"] is True
    assert result["command_count"] == 1
    assert result["route"] == "bounded"
    assert all(result[field] is False for field in ("physical_execution", "mission_completed", "model_value_demonstrated"))


@pytest.mark.parametrize("reason,status,response", [("hold_requested", "held", True), ("no_broker", "no_broker_hold", False),
                                                    ("decision_expired", "expired", False), ("malformed_response", "rejected", False)])
def test_honest_hold_and_timeout_are_valid_records_without_command_effect(reason, status, response):
    result = verify(*hold_evidence(reason, status, response))
    assert result["passed"] is True, result
    assert result["observed_effect"] is False
    assert result["command_count"] == 0


def test_ack_only_never_establishes_effect():
    run, profile = evidence()
    record = run["supervision"]
    record["observations"] = record["observations"][:3]
    run["samples"] = run["samples"][:3]+run["samples"][-1:]
    record["status"] = "mission_ended_before_evidence"
    result = verify(run, profile)
    assert result["passed"] is True, result
    assert result["observed_effect"] is False
    record["status"] = "effect_observations_recorded"
    rejected(run, profile, "effect_status")


def test_one_post_observation_is_not_two():
    run, profile = evidence()
    run["supervision"]["observations"] = run["supervision"]["observations"][:4]
    run["samples"].pop(4)
    rejected(run, profile, "effect_status")


@pytest.mark.parametrize("post_states", [("inhibited", "inhibited"), ("inhibited", "skipped")])
def test_later_observations_can_truthfully_show_failed_or_delayed_command_effect(post_states):
    run, profile = evidence()
    record = run["supervision"]
    for index, state in zip((3, 4), post_states):
        record["observations"][index]["sequencer_state"] = state
        run["samples"][index]["flight_supervision"]["sequencer_state"] = state
    run["samples"][-1]["flight_supervision"]["sequencer_state"] = post_states[-1]
    record["final_sequencer_state"] = post_states[-1]
    result = verify(run, profile)
    assert result["passed"] is True, result
    assert result["command_count"] == 1
    assert result["observed_effect"] is False


def test_post_observations_must_span_two_release_intervals_after_command():
    rejected(*evidence((100., 102., 104., 110., 125.)), "effect_status")


@pytest.mark.parametrize("target", ["request_id", "observation_id"])
def test_proposal_binding_is_not_inferred_from_an_ack(target):
    run, profile = evidence()
    run["supervision"]["response"][target] = "other"
    rejected(run, profile, "response_binding")


def test_command_cannot_be_replayed():
    run, profile = evidence()
    run["supervision"]["commands"].append(deepcopy(run["supervision"]["commands"][0]))
    rejected(run, profile, "command_budget")


def test_command_after_decision_expiry_is_rejected_even_if_ack_and_later_effect_exist():
    rejected(*evidence((100., 102., 180., 195., 210.)), "rules_constraint")


def test_command_after_return_deadline_is_rejected():
    run, profile = evidence()
    profile["guidance"]["coast_before_return_s"] = 103.
    for observation in [*run["supervision"]["observations"], *run["supervision"]["request"]["observations"]]:
        observation["return_deadline_s"] = 103.
    rejected(run, profile, "rules_constraint")


def test_fuel_reserve_is_checked_at_dispatch_not_just_proposal():
    run, profile = evidence()
    run["supervision"]["observations"][2]["propellant_kg"] = 27_000.
    run["samples"][2]["propellant_kg"] = 27_000.
    rejected(run, profile, "rules_constraint")


@pytest.mark.parametrize("field,value", [("perigee_altitude_m", 1.), ("body_rate_rad_s", 7.),
                                         ("propellant_kg", 123.), ("dynamic_pressure_pa", 3.)])
def test_observations_are_recomputed_or_bound_to_saved_physical_state(field, value):
    run, profile = evidence()
    run["supervision"]["observations"][2][field] = value
    rejected(run, profile, "observation_state_binding")


@pytest.mark.parametrize("target", ["observation_id", "sequence", "time_s"])
def test_duplicate_or_stale_observations_are_rejected(target):
    run, profile = evidence()
    rows = run["supervision"]["observations"]
    rows[4][target] = rows[3][target]
    rejected(run, profile)


def test_future_or_hidden_scenario_data_is_not_part_of_request():
    run, profile = evidence()
    run["supervision"]["request"]["future_recovery_time_s"] = 200.
    rejected(run, profile, "request_binding")


def test_hidden_observation_field_is_not_sent_to_provider():
    run, profile = evidence()
    run["supervision"]["observations"][0]["scenario_name"] = "deployment_no_effect"
    rejected(run, profile, "observation_schema")


def test_missing_physical_samples_cannot_be_replaced_by_telemetry_claims():
    run, profile = evidence()
    run["samples"][2].pop("flight_supervision")
    rejected(run, profile, "observation_sample_binding")


def test_final_state_cannot_resume_payload_sequence_after_two_good_observations():
    run, profile = evidence()
    run["samples"][-1]["flight_supervision"]["sequencer_state"] = "inhibited"
    rejected(run, profile, "final_telemetry")


def test_an_actual_payload_separation_invalidates_no_effect_claim():
    run, profile = evidence()
    run["satellites"] = [{"id": "satellite_01"}]
    rejected(run, profile, "event_binding")


@pytest.mark.parametrize("route", ["need_observation", "human_review"])
def test_escalation_routes_cannot_dispatch_a_skip(route):
    run, profile = evidence()
    run["supervision"]["response"]["route"] = route
    rejected(run, profile, "rules_constraint")


def test_bound_request_id_and_approval_scope_remain_external_requirements():
    run, profile = evidence()
    assert verify_supervision(run, profile, expected_request_id="other")["passed"] is False
    contract = {**CONTRACT, "maximum_commands": 2}
    assert verify_supervision(run, profile, expected_contract=contract)["passed"] is False


@pytest.mark.parametrize("value", [None, [], True, 7, "record", {}])
def test_malformed_outer_input_returns_bounded_findings(value):
    assert verify_supervision(value, {})["passed"] is False


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 10**500])
def test_nonfinite_or_huge_values_fail_closed(value):
    run, profile = evidence()
    run["supervision"]["ignored"] = value
    rejected(run, profile, "invalid_number")


def test_cycles_are_rejected_without_unbounded_traversal():
    run, profile = evidence()
    run["supervision"]["cycle"] = run["supervision"]
    rejected(run, profile, "invalid_json")


def test_mutated_json_shapes_never_raise():
    base, profile = evidence()
    mutations = [None, [], {}, True, "bad", 1]
    paths = [("response", "route"), ("response", "action"), ("rules", 0, "observation_id"),
             ("observations", 0, "sequencer_state"), ("observations", 0, "sequence"),
             ("request", "observations"), ("commands", 0), ("observations", 0)]
    for path in paths:
        for value in mutations:
            run = deepcopy(base)
            target = run["supervision"]
            for key in path[:-1]:
                target = target[key]
            if target[path[-1]] == value and type(target[path[-1]]) is type(value):
                continue
            target[path[-1]] = value
            result = verify(run, profile)
            assert result["passed"] is False, (path, value, result)
