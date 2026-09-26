"""Urban gating must survive replay, HTTP failure and worker-lifetime failures."""

from copy import deepcopy
from dataclasses import replace
import json
import threading

import pytest
from click.testing import CliRunner

from missionos_cli.cli import missionos
from src.runtime.ship_urban_loop import LoopRejected, UrbanLoopPlan, UrbanLoopRuntime
from src.runtime.ship_urban_loop_fixture import (
    FAULTS,
    FixtureAutopilot,
    FixtureRules,
    run_fixture,
)
from src.runtime.ship_urban_loop_verifier import verify_urban_loop


def test_loop_requires_approval_before_creating_processes_or_output(tmp_path):
    with pytest.raises(PermissionError, match="approve-fixture"):
        run_fixture(tmp_path / "no")
    assert not (tmp_path / "no").exists()


@pytest.fixture(scope="module")
def completed(tmp_path_factory):
    return run_fixture(tmp_path_factory.mktemp("urban-control") / "run", approved=True)


def test_two_updates_reach_distinct_model_selected_targets_then_stop(completed):
    assert completed["status"] == "completed", completed["failure"]
    assert completed["completed_updates"] == 2
    assert completed["owned_processes_reaped"] and completed["late_response_rejected"]
    assert completed["fixture_ap_return_handoff"]
    assert verify_urban_loop(completed)["control_sequence_verified"]
    targets = [
        e["permit"]["target_ned_m"]
        for e in completed["events"]
        if e["event"] == "segment_authorized"
    ]
    assert targets == [[1035.0, -5.0, -30.0], [1055.0, 0.0, -30.0]]
    for field in (
        "vla_invoked",
        "wam_invoked",
        "px4_runtime_invoked",
        "gazebo_runtime_invoked",
        "whole_mission_completion_verified",
        "onboard_energy_savings_verified",
    ):
        assert completed[field] is False


@pytest.mark.parametrize("fault", FAULTS[1:])
def test_failures_do_not_fall_through_to_sea_return_and_reap_workers(tmp_path, fault):
    result = run_fixture(tmp_path / fault, approved=True, fault=fault)
    assert result["status"] == "blocked", fault
    assert result["failure"]
    assert result["fixture_ap_return_handoff"] is False
    assert result["owned_processes_reaped"] is True
    assert not verify_urban_loop(result)["control_sequence_verified"]
    if fault in {"offshore_entry", "wrong_phase", "stale_observation"}:
        assert not (tmp_path / fault / "workers").exists()
        assert not any(e["event"] == "model_start_requested" for e in result["events"])
    if fault in {
        "reused_image",
        "unsafe_candidate",
        "rules_rejection",
        "http_failure",
        "timeout",
        "hold_loss",
    }:
        assert result["completed_updates"] == 0
    if fault == "cross_cycle":
        assert result["completed_updates"] == 1


@pytest.mark.parametrize(
    "fault",
    [
        "old_image",
        "target",
        "other_run",
        "approval",
        "missing_stop",
        "reordered",
        "arrival",
    ],
)
def test_offline_verifier_rejects_tampered_completed_claim(completed, fault):
    result = deepcopy(completed)
    events = result["events"]
    if fault == "old_image":
        sent = [e for e in events if e["event"] == "model_requested"]
        sent[2]["request"]["observation"] = deepcopy(sent[0]["request"]["observation"])
    elif fault == "target":
        next(e for e in events if e["event"] == "segment_authorized")["permit"]["target_ned_m"][
            0
        ] = 990
    elif fault == "other_run":
        events[10]["run_id"] = "another-flight"
    elif fault == "approval":
        next(e for e in events if e["event"] == "segment_authorized")["permit"][
            "approval_ref"
        ] = "unapproved"
    elif fault == "missing_stop":
        result["events"] = [e for e in events if e["event"] != "models_stopped"]
    elif fault == "reordered":
        next(e for e in events if e["event"] == "ap_return_handoff")["at_s"] = events[0]["at_s"]
    elif fault == "arrival":
        next(e for e in events if e["event"] == "segment_arrived")["observation"]["position_ned_m"][
            0
        ] += 4
    assert not verify_urban_loop(result)["control_sequence_verified"]


def test_cancel_during_model_start_cannot_enable_late_response_or_return():
    plan = replace(
        UrbanLoopPlan("startup-cancel", "test:fixture", "fixture"),
        settle_s=0.001,
        startup_timeout_s=0.02,
    )
    ap = FixtureAutopilot(plan)

    class SlowStart:
        def __init__(self):
            self.cancelled = threading.Event()

        def start(self):
            self.cancelled.wait(1)
            return {"execution_scope": "fixture"}

        def stop(self):
            self.cancelled.set()
            return {"cancelled": True}

        def stopped(self):
            return self.cancelled.is_set()

    runtime = UrbanLoopRuntime(plan, ap, SlowStart(), FixtureRules(), poll_s=0.005)
    result = runtime.run()
    assert "urban_model_timeout" in result["failure"]
    assert result["model_shutdown_verified"] and not ap.returned
    assert not runtime.active
    with pytest.raises(LoopRejected, match="stale_or_unbound"):
        runtime.accept_response({"request_id": "late", "epoch": 0}, {})
    with pytest.raises(LoopRejected, match="single_use"):
        runtime.run()


@pytest.mark.parametrize(
    "field,value",
    [("reserve_fraction", float("nan")), ("updates", True), ("coast_north_m", 1020)],
)
def test_invalid_or_offshore_plan_is_not_authority(field, value):
    with pytest.raises(ValueError):
        replace(UrbanLoopPlan("validation", "test:fixture", "fixture"), **{field: value})


def test_cli_boundary_creates_reopenable_receipts(tmp_path):
    destination = tmp_path / "cli"
    result = CliRunner().invoke(
        missionos,
        [
            "ship-delivery",
            "urban-loop-smoke",
            "--approve-fixture",
            "--output-dir",
            str(destination),
        ],
    )
    assert result.exit_code == 0, result.output
    receipt = json.loads((destination / "result.json").read_text())
    assert verify_urban_loop(receipt) == json.loads((destination / "verification.json").read_text())
