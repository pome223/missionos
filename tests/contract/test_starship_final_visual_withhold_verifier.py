"""Saved visual-stop partition; no flight, provider, sockets or subprocesses."""

from copy import deepcopy
from dataclasses import asdict, replace
import socket
import subprocess

import pytest

from src.runtime import starship_final_verifier as final
from src.runtime import starship_final_shared as shared
from src.runtime import starship_final_visual as visual_module
from src.runtime import starship_restored_verifier as legacy
from src.runtime import starship_restored_native_verifier as native
from src.runtime import starship_sixdof as dyn, starship_physics as env
from src.runtime import starship_restored_return as returning
from src.runtime.starship_sixdof_verifier import _Invalid
from tests.contract.test_starship_final_visual_counter import make_counter_timeline
from tests.contract.test_starship_final_visual import Plant


@pytest.fixture(autouse=True)
def no_flight_or_model(monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError(
            "Saved visual stop checks cannot advance a plant, call providers or spawn"
        )

    for owner, name in (
        (dyn, "step"),
        (dyn, "_rk4"),
        (dyn, "_rhs"),
        (env, "step"),
        (returning, "advance_plant"),
        (returning, "control"),
        (returning, "forecast_restored_return"),
        (socket, "socket"),
        (socket, "create_connection"),
        (subprocess, "Popen"),
    ):
        monkeypatch.setattr(owner, name, forbidden)


def rebind(case):
    prefix, value, *_ = case
    prefix["restored_prefix"]["decisions"][-1]["final_visual_observation_sha256"] = shared.digest(
        value
    )


@pytest.fixture
def stop_case():
    value, prefix, inventory, inputs = make_counter_timeline(terminal_before_observer=False)
    now = prefix["final_state"]["time_s"]
    episode = {
        "episode_id": "visual-a-test",
        "event_id": "unconfirmed-a-test",
        "release_attempt_id": "a-test",
        "started_s": 128.0,
        "deadline_s": 163.0,
        "started_wall_s": 1000.0,
        "wall_deadline_s": 1035.0,
    }
    request = {"request_id": "visual-a-test-1", "snapshot_time_s": 128.0, "deadline_s": 163.0}
    envelope = {
        "request": request,
        "request_sha256": shared.digest(request),
        "envelope_sha256": "a" * 64,
    }
    binding = {
        "run_id": "final-counter-order-test",
        "plan_sha256": "b" * 64,
        "source_sha256": "c" * 64,
        "mode": "live",
        "policy_sha256": shared.digest(inputs["visual_policy"]),
    }
    value.update(binding)
    result = {"status": "unavailable", "action": None, "model_inference_invoked": False}
    response = {
        "schema": visual_module.HOST_RESULT,
        **binding,
        "request_sha256": envelope["request_sha256"],
        "envelope_sha256": envelope["envelope_sha256"],
        "result": result,
        "result_sha256": shared.digest(result),
    }
    host = {"attempts": [{"envelope_sha256": "a" * 64, "response": response}]}
    rpc = {
        "visual-a-test-1": {"client": {"expired": False, "delivered_result": deepcopy(response)}}
    }
    value.update(
        episode=episode,
        requests=[envelope],
        results=[
            {
                "envelope_sha256": "a" * 64,
                "accepted": False,
                "time_s": now,
                "rejection": "invalid_or_unadmitted_visual_result",
            }
        ],
        closed_reason="invalid_visual_result_no_retry",
        withhold_deployment=True,
        pending_request=None,
    )
    value["events"].append(
        {
            "event": "visual_episode_closed",
            "action": "withhold_deployment",
            "reason": value["closed_reason"],
            "time_s": now,
            "basis": "rules",
            "inventory_assigned": False,
            "plant_state_assigned": False,
        }
    )
    plant = Plant()
    samples = []
    decisions = []
    for count, t in [(0, 102.0), (13, now)]:
        sample = asdict(replace(plant.state, time_s=t))
        sample.update(phase="orbital_coast", dynamic_pressure_pa=0.0)
        samples.append(sample)
        bound, rp = legacy.saved._orbit(sample)
        ready = bool(bound and rp >= plant.profile["guidance"]["target_perigee_m"] - 1000.0)
        decisions.append(
            {
                "action": "continue_deployment" if count == 0 else "withhold_deployment",
                "origin": "external_callback_not_model_or_approval_proof"
                if count == 0
                else "final_visual_observation_withhold_not_approval_proof",
                "pending": None,
                "callback_error_type": None,
                "summary": {
                    "time_s": t,
                    "released_count": count,
                    "retained_count": 26 - count,
                    "release_ready": ready,
                    "sequencer_state": "running" if count == 0 else "held",
                    "notice": ""
                    if count == 0
                    else "Independent observation episode ended with further deployment withheld.",
                    "decision_started_s": None,
                    "decision_deadline_s": None,
                },
            }
        )
    prefix["samples"] = samples
    prefix["restored_prefix"]["decisions"] = decisions
    handoff = prefix["restored_prefix"]["handoff_record"]
    handoff["experimental_mission_ascent"] = {"schedule": {"first_release_time_s": 102.0}}
    handoff["final_state"] = {"time_s": now}
    inputs["mode"] = "live"
    case = (prefix, value, inputs, host, rpc, plant.profile)
    rebind(case)
    return case


def partition(case):
    prefix, value, inputs, host, rpc, _ = case
    return final._visual_deployment_partition(prefix, value, inputs=inputs, host=host, rpc=rpc)


def test_saved_nonmodel_stop_keeps_original_record_and_common_inventory_gate(stop_case):
    before = shared.digest(stop_case)
    prefix, _, _, _, _, profile = stop_case
    ordinary, callback = partition(stop_case)
    assert ordinary == prefix["restored_prefix"]["decisions"][:1]
    legacy._decisions(
        prefix,
        prefix["restored_prefix"]["handoff_record"],
        profile,
        extra_metadata_validator=callback,
    )
    assert shared.digest(stop_case) == before
    with pytest.raises(_Invalid, match="Decision does not match"):
        legacy._decisions(prefix, prefix["restored_prefix"]["handoff_record"], profile)


@pytest.mark.parametrize(
    "change",
    [
        "origin",
        "missing_hash",
        "changed_hash",
        "action",
        "pending",
        "callback_error",
        "wrong_count",
        "state",
        "notice",
        "invented_deadline",
        "withhold_false",
        "pending_visual",
        "missing_closed",
        "extra_closed",
        "closure_time",
        "closure_basis",
        "assign_inventory",
        "unknown_reason",
        "stale_terminal",
        "wrong_tick_count",
        "pre_observer_terminal",
        "missing_result",
        "result_time",
        "false_acceptance",
        "wrong_rejection",
        "missing_request",
        "missing_attempt",
        "duplicate_attempt",
        "expired_rpc",
        "undelivered_rpc",
        "changed_response_hash",
        "response_wrong_scope",
        "valid_response_rejected",
    ],
)
def test_visual_stop_requires_original_host_closure_clock_and_evidence(
    stop_case, change, monkeypatch
):
    prefix, value, _, host, rpc, _ = stop_case
    row = prefix["restored_prefix"]["decisions"][-1]
    summary = row["summary"]
    if change == "origin":
        row["origin"] = "unknown"
    elif change == "missing_hash":
        del row["final_visual_observation_sha256"]
    elif change == "changed_hash":
        row["final_visual_observation_sha256"] = "0" * 64
    elif change == "action":
        row["action"] = "continue_deployment"
    elif change == "pending":
        row["pending"] = {}
    elif change == "callback_error":
        row["callback_error_type"] = "Other"
    elif change == "wrong_count":
        summary["released_count"] = 12
    elif change == "state":
        summary["sequencer_state"] = "running"
    elif change == "notice":
        summary["notice"] = ""
    elif change == "invented_deadline":
        summary["decision_deadline_s"] = 163.0
    elif change == "withhold_false":
        value["withhold_deployment"] = False
    elif change == "pending_visual":
        value["pending_request"] = {}
    elif change == "missing_closed":
        value["events"].pop()
    elif change == "extra_closed":
        value["events"].append(deepcopy(value["events"][-1]))
    elif change == "closure_time":
        value["events"][-1]["time_s"] -= 0.25
    elif change == "closure_basis":
        value["events"][-1]["basis"] = "ai"
    elif change == "assign_inventory":
        value["events"][-1]["inventory_assigned"] = True
    elif change == "unknown_reason":
        value["closed_reason"] = value["events"][-1]["reason"] = "unknown"
    elif change == "stale_terminal":
        prefix["final_state"]["time_s"] += 0.25
    elif change == "wrong_tick_count":
        prefix["concurrent_execution"]["tick_order"][-1]["released_count"] = 12
    elif change == "pre_observer_terminal":
        prefix["events"][-1]["detail"] = (
            "actual inventory already closed; same actual coast state retained"
        )
    elif change == "missing_result":
        value["results"] = []
    elif change == "result_time":
        value["results"][-1]["time_s"] -= 0.25
    elif change == "false_acceptance":
        value["results"][-1]["accepted"] = True
    elif change == "wrong_rejection":
        value["results"][-1]["rejection"] = "some_reason"
    elif change == "missing_request":
        value["requests"] = []
    elif change == "missing_attempt":
        host["attempts"] = []
    elif change == "duplicate_attempt":
        host["attempts"].append(deepcopy(host["attempts"][0]))
    elif change == "expired_rpc":
        rpc["visual-a-test-1"]["client"]["expired"] = True
    elif change == "undelivered_rpc":
        rpc["visual-a-test-1"]["client"]["delivered_result"] = None
    elif change == "changed_response_hash":
        host["attempts"][0]["response"]["result_sha256"] = "0" * 64
    elif change == "response_wrong_scope":
        host["attempts"][0]["response"]["plan_sha256"] = "f" * 64
        rpc["visual-a-test-1"]["client"]["delivered_result"] = deepcopy(
            host["attempts"][0]["response"]
        )
    elif change == "valid_response_rejected":
        monkeypatch.setattr(visual_module, "validate_result", lambda *a: None)
    if change not in ("missing_hash", "changed_hash"):
        rebind(stop_case)
    with pytest.raises((ValueError, KeyError)):
        partition(stop_case)


@pytest.mark.parametrize(
    "change", ["count", "retained", "release_order", "clock", "readiness", "witness"]
)
def test_visual_exception_cannot_skip_shared_numerical_inventory_or_clock_checks(stop_case, change):
    prefix, _, _, _, _, profile = stop_case
    _, callback = partition(stop_case)
    handoff = prefix["restored_prefix"]["handoff_record"]
    if change == "count":
        prefix["restored_prefix"]["decisions"][0]["summary"]["released_count"] = 1
    elif change == "retained":
        prefix["restored_prefix"]["decisions"][0]["summary"]["retained_count"] = 25
    elif change == "release_order":
        handoff["inventory"]["release_receipts"][-1]["time_s"] = (
            prefix["final_state"]["time_s"] + 1.0
        )
    elif change == "clock":
        handoff["experimental_mission_ascent"]["schedule"]["first_release_time_s"] = 1000.0
    elif change == "readiness":
        prefix["restored_prefix"]["decisions"][0]["summary"]["release_ready"] ^= True
    else:
        prefix["samples"].pop()
    with pytest.raises((_Invalid, ValueError)):
        legacy._decisions(prefix, handoff, profile, extra_metadata_validator=callback)


def test_nonmodel_stop_cannot_erase_an_ordinary_model_checkpoint(stop_case):
    prefix, *_ = stop_case
    governance = {
        "deployment": {
            "schema": "missionos.starship_restored_deployment_judgments.v1",
            "pending_request": None,
            "closed_inventory_inferred": False,
            "physical_release_inferred": False,
            "native_model_calls_inferred": False,
            "decisions": [],
        }
    }
    with pytest.raises(ValueError, match="subsequence"):
        native._deployment(governance, prefix, {}, {}, model_decisions=[])
    with pytest.raises(ValueError, match="chain"):
        native._deployment(governance, prefix, {}, {})


def test_saved_accepted_stop_is_separate_from_ordinary_deployment_choice(stop_case):
    prefix, value, inputs, *_ = stop_case
    inputs["mode"] = "fixture"
    value["closed_reason"] = value["events"][-1]["reason"] = "accepted_visual_proposal"
    value["events"][-1]["basis"] = "fixture"
    value["results"][-1].update(
        accepted=True, rejection=None, reply={"result": {"action": "stop_deployment"}}
    )
    rebind(stop_case)
    ordinary, callback = partition(stop_case)
    assert len(ordinary) == 1 and callback(
        prefix["restored_prefix"]["decisions"][-1], 13, "withhold_deployment"
    )


@pytest.mark.parametrize("change", ["before_snapshot", "after_original_margin"])
def test_invalid_result_stop_cannot_precede_snapshot_or_follow_original_deadline(stop_case, change):
    _, value, inputs, *_ = stop_case
    now = value["results"][-1]["time_s"]
    if change == "before_snapshot":
        value["requests"][0]["request"]["snapshot_time_s"] = now + 0.25
    else:
        deadline = now + inputs["visual_policy"]["apply_margin_s"]
        value["episode"]["deadline_s"] = deadline
        value["episode"]["started_s"] = deadline - inputs["visual_policy"]["hold_s"]
        value["requests"][0]["request"]["deadline_s"] = deadline
    rebind(stop_case)
    with pytest.raises(ValueError, match="outside_original_snapshot_deadline"):
        partition(stop_case)
