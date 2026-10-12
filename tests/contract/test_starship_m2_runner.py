"""M2 orchestration and corruption checks; no physics, process or hosted model."""

from copy import deepcopy
from dataclasses import asdict
import json
import math
from types import SimpleNamespace

import pytest

from src.runtime import starship_m2_runner as runner, starship_m2_verifier as verifier
from src.runtime.starship_deployment_handoff import DeploymentTracker
from src.runtime.starship_m2_contract import CHECKPOINT_TIME_S, PROFILE, m2_contract
from src.runtime.starship_mission_director import eligible_actions
from src.runtime.starship_replanning import ROOT, digest
from src.runtime.starship_sixdof import EngineState, State6DOF
from src.runtime.starship_sixdof_mission import vehicle


def clone(value):
    return json.loads(json.dumps(value, allow_nan=False))


def records(case="operations_notice"):
    p = json.loads((ROOT / PROFILE).read_text())
    authority = m2_contract("fixture", case)
    tracker = DeploymentTracker(26, digest(authority))
    trace, events, decisions, satellites = (
        [],
        [{"event": "orbit_cutoff_command", "time_s": 480.7}],
        [],
        [],
    )

    def observe(t, count, sequence):
        row = {
            "time_s": t,
            "released_count": count,
            "sequencer_state": sequence,
            "phase": "orbital_coast",
        }
        row = telemetry(row)
        tracker.observe(row)
        trace.append({"kind": "observation", "row": row})
        return row

    def telemetry(row):
        return {
            **row,
            "release_acknowledged": False,
            "fuel_kg": 100000.0,
            "return_deadline_s": 2280.7,
            "tower_ready": False,
            "operations_notice": "",
            "hold_expires_at_s": 530.0 if row["sequencer_state"] == "held" else None,
            "numerical_tools": {
                "orbit_release_feasible": True,
                "retained_payload_present": True,
                "capture_corridor_certified": False,
                "mechanism_status": "not_collected",
            },
        }

    def command(action, before, sequence):
        t = before["time_s"]
        point = (
            "deployment_start"
            if action == "hold"
            else "deployment_reassessment"
            if action == "continue"
            else "deployment_diagnostic"
            if any(d["dispatch"]["action"] == "collect_status" for d in decisions)
            else "deployment_monitor"
        )
        tracker.command(action, t)
        trace.append({"kind": "command", "action": action, "time_s": t})
        events.append(
            {
                "event": "managed_mission_command",
                "point": point,
                "action": action,
                "time_s": t,
            }
        )
        if action == "continue" and before["sequencer_state"] == "held":
            events.append({"event": "managed_deployment_resumed", "time_s": t})
        later = observe(t + 0.25, before["released_count"], sequence)
        before_dto = telemetry(before)
        request = {
            "schema": "missionos.starship_director_request.v5",
            "request_id": "test-deployment:" + str(len(decisions) + 1),
            "point": point,
            "observation": before_dto,
            "decision_deadline_s": t + 15.0,
            "envelope_sha256": digest(authority["deployment_authority"]),
            "allowed_actions": eligible_actions(
                authority["deployment_authority"],
                point,
                before_dto,
                observation_requests=sum(
                    d["dispatch"]["action"] == "collect_status" for d in decisions
                ),
                hold_used_s=30.0
                if any(d["dispatch"]["action"] == "hold" for d in decisions)
                else 0.0,
            ),
        }
        response = {
            "request_id": request["request_id"],
            "request_sha256": digest(request),
            "action": action,
            "mode": "fixture",
            "jev_invocation": None,
            "llm_invocation": None,
            "model_inference_invoked": False,
        }
        decisions.append(
            {
                "clock_id": "ship",
                "request": request,
                "response": response,
                "dispatch": {
                    "action": action,
                    "time_s": t,
                    "observation": before_dto,
                    "rules_accepted": True,
                    "rejection": None,
                    "fallback_rules_accepted": False,
                    "fallback_rejection": None,
                },
                "later_observation": telemetry(later),
            }
        )
        return later

    row = observe(500.0, 0, "running")
    if case == "hold_resume":
        row = command("hold", row, "held")
        row = observe(505.0, 0, "held")
        row = command("continue", row, "running")
    released = {"normal": 26, "hold_resume": 26, "operations_notice": 1, "release_fault": 0}[case]
    for count in range(1, released + 1):
        event_t = 500.0 + count * 10.0
        events.append({"event": "payload_released", "time_s": event_t})
        satellites.append({"release_state": {"time_s": event_t}})
        row = observe(event_t + 0.25, count, "running")
    if case == "release_fault":
        row = command("collect_status", row, "held")
    if released < 26:
        row = command("stop_deployment", row, "skipped")
    observe(CHECKPOINT_TIME_S - 0.25, released, row["sequencer_state"])
    craft = vehicle(p, payload_count=26 - released)
    state = clone(
        asdict(
            State6DOF(
                CHECKPOINT_TIME_S,
                (6678137.123, 0.0, 0.0),
                (0.0, 7725.0023, 0.0),
                (0.5, -0.5, 0.5, 0.5),
                (0.0010012, 0.0, 0.0),
                100001.5,
                tuple(EngineState(throttle=0.01) for _ in craft.engines),
                tuple(0.003 for _ in craft.aero_panels),
            )
        )
    )
    handoff = tracker.freeze(state, digest(p))
    launch = {
        "scenario": "launch",
        "mission_management_case": authority["deployment_case"],
        "mission_director": {
            "envelope": authority["deployment_authority"],
            "response_fault": authority["deployment_response_fault"],
            "records": decisions,
        },
        "events": events,
        "satellites": satellites,
        "samples": [{**state, "phase": "orbital_coast"}],
        "final_state": state,
        "final_vehicle": clone(asdict(craft)),
        "outcome": {
            "orbit_gate_reached": True,
            "phase": "orbital_coast",
            "termination": "time_limit",
            "duration_s": CHECKPOINT_TIME_S,
            "payload_released_count": released,
        },
    }
    source = {PROFILE: "a" * 64}
    returning = {
        "deployment": {"authority": authority, "handoff": handoff, "launch": launch},
        "launch": launch,
        "execution_origin": state,
        "scheduled_s": 480.7 + p["guidance"]["coast_before_return_s"],
        "execution": {"events": []},
        "decisions": [],
        "dispatch": {"candidate_id": "nominal"},
    }
    observed = handoff["origin"]["state"]
    radius = math.sqrt(sum(x*x for x in observed["r_eci_m"]))
    energy = sum(x*x for x in observed["v_eci_mps"])/2 - 3.986004418e14/radius
    a = -3.986004418e14/(2*energy)
    returning["opportunities"] = {
        "nominal": returning["scheduled_s"],
        "next_orbit": returning["scheduled_s"]+2*math.pi*math.sqrt(a*a*a/3.986004418e14),
    }
    return clone(
        {
            "schema": "missionos.starship_m2_study.v1",
            "run_id": "test",
            "authority": authority,
            "profile": p,
            "deployment_run": launch,
            "trace": trace,
            "handoff": handoff,
            "return_study": returning,
            "source_sha256": source,
            "return_source_sha256": source,
            "budget": runner.budget(authority),
            "runtime": {
                "schema": "missionos.starship_m2_runtime.v1",
                "mode": "fixture",
                "boundary": "local_cli_fixture_continuation",
                "deployment_started": True,
                "continuation_started": True,
                "sources_unchanged": True,
                "retry_performed": False,
                "physical_execution": False,
                "model_calls": 0,
                "human_inflight_commands": 0,
                "wall_time_s": 1.0,
            },
        }
    )


@pytest.fixture
def checked(monkeypatch):
    calls = []

    def verify_return(study, **kwargs):
        calls.append(kwargs)
        return {
            "passed": True,
            "issues": [],
            "case_accepted": False,
            "case_issues": ["normal_return_changed"],
            "outcome": "contact_and_recovery_area_met",
        }

    monkeypatch.setattr(verifier, "verify_return", verify_return)
    return calls


def check(study, expected=None):
    expected = study if expected is None else expected
    return verifier.verify(
        study,
        expected_authority=expected["authority"],
        expected_sources=expected["source_sha256"],
        expected_return_sources=expected["return_source_sha256"],
        expected_run_id=expected["run_id"],
    )


@pytest.mark.parametrize("case", ["normal", "operations_notice", "release_fault", "hold_resume"])
def test_independent_observed_binding_passed_to_return_verifier(case, checked):
    study = records(case)
    if case != "normal":
        study["return_study"]["dispatch"]["candidate_id"] = "next_orbit"
    result = check(study)
    assert result["passed"] and result["case_accepted"], result
    assert checked[0]["expected_deployment_binding"] == {
        k: study["handoff"][k] for k in verifier.BINDING_KEYS
    }
    assert checked[0]["expected_run_id"] == "test-return"
    assert result["mission_completed"] is result["physical_execution"] is False


def test_no_admissible_return_is_preserved_as_unresolved(monkeypatch):
    study = records()
    study["return_study"]["dispatch"] = None
    monkeypatch.setattr(
        verifier,
        "verify_return",
        lambda *a, **k: {"passed": True, "issues": [], "outcome": "unresolved"},
    )
    result = check(study)
    assert result["passed"] and not result["case_accepted"]
    assert result["outcome"] == "unresolved"


def test_normal_return_change_is_not_success(checked):
    study = records("normal")
    study["return_study"]["dispatch"]["candidate_id"] = "next_orbit"
    result = check(study)
    assert result["passed"] and not result["case_accepted"]


@pytest.mark.parametrize(
    "change",
    [
        "authority",
        "profile",
        "source",
        "inner_source",
        "state",
        "actuator",
        "origin_count",
        "held",
        "stale",
        "count",
        "boolean_count",
        "generation",
        "ack_only",
        "event",
        "satellite",
        "vehicle",
        "command",
        "later",
        "cutoff",
        "return_time",
        "return_launch",
        "return_reset",
        "post_release",
        "model",
        "retry",
        "failure",
        "budget",
    ],
)
def test_corruption_is_rejected_before_return_verifier(change, checked):
    study = records()
    expected = deepcopy(study)
    if change == "authority":
        study["authority"]["maximum_model_calls"] = 1
    elif change == "profile":
        study["profile"]["payload"]["mass_each_kg"] += 1
    elif change == "source":
        study["source_sha256"][PROFILE] = "b" * 64
    elif change == "inner_source":
        study["return_source_sha256"] = {}
    elif change == "state":
        study["deployment_run"]["final_state"]["r_eci_m"][0] += 1
    elif change == "actuator":
        study["handoff"]["execution_state"]["engine_states"][0]["throttle"] += 0.1
    elif change == "origin_count":
        study["handoff"]["origin"]["retained_count"] = 0
    elif change == "held":
        study["trace"][-1]["row"]["sequencer_state"] = "held"
    elif change == "stale":
        study["trace"] = study["trace"][:-1]
    elif change == "count":
        study["handoff"]["retained_count"] = 24
    elif change == "boolean_count":
        study["handoff"]["released_count"] = True
    elif change == "generation":
        study["handoff"]["generation"] += 1
    elif change == "ack_only":
        study["trace"][1]["row"]["released_count"] = 0
    elif change == "event":
        study["deployment_run"]["events"][1]["time_s"] = 499.0
    elif change == "satellite":
        study["deployment_run"]["satellites"][0]["release_state"]["time_s"] += 1
    elif change == "vehicle":
        study["deployment_run"]["final_vehicle"]["dry_mass_kg"] += 1
    elif change == "command":
        study["trace"] = [r for r in study["trace"] if r["kind"] != "command"]
    elif change == "later":
        study["deployment_run"]["mission_director"]["records"][0]["later_observation"] = None
    elif change == "cutoff":
        study["deployment_run"]["events"][0]["time_s"] = 9999.0
    elif change == "return_time":
        study["return_study"]["scheduled_s"] += 1
    elif change == "return_launch":
        study["return_study"]["launch"]["final_state"]["propellant_kg"] += 1
    elif change == "return_reset":
        study["return_study"]["execution_origin"]["propellant_kg"] += 1
    elif change == "post_release":
        study["return_study"]["execution"]["events"] = [{"event": "payload_released"}]
    elif change == "model":
        study["return_study"]["decisions"] = [{"model_inference_invoked": True}]
    elif change == "retry":
        study["runtime"]["retry_performed"] = True
    elif change == "failure":
        study["runtime_failure"] = "failed"
    elif change == "budget":
        study["budget"]["maximum_forecasts"] += 1
    study["handoff"]["handoff_sha256"] = digest(
        {k: v for k, v in study["handoff"].items() if k != "handoff_sha256"}
    )
    result = check(study, expected)
    assert not result["passed"] and not result["case_accepted"], result
    assert not checked


def prepare_runner(monkeypatch, case="operations_notice", fail=None):
    bundle = records(case)
    monkeypatch.setattr(runner, "sources", lambda: bundle["source_sha256"])
    monkeypatch.setattr(runner, "return_sources", lambda: bundle["return_source_sha256"])
    monkeypatch.setattr(runner, "readiness", lambda profile: (None, None, None))
    monkeypatch.setattr(runner.ReturnSites, "from_dict", lambda *a, **k: None)
    monkeypatch.setattr(
        runner,
        "ObservedDeploymentDirector",
        lambda envelope, tracker, **k: SimpleNamespace(tracker=tracker, trace=[], pending={}),
    )
    calls = []

    def simulate(profile, **kwargs):
        calls.append("launch")
        director = kwargs["mission_director"]
        for row in bundle["trace"]:
            if row["kind"] == "observation":
                director.tracker.observe(row["row"])
            else:
                director.tracker.command(row["action"], row["time_s"])
            director.trace.append(row)
        if fail == "launch":
            raise ValueError("injected_launch_failure")
        return deepcopy(bundle["deployment_run"])

    class Executor:
        def __init__(self, profile, envelope, case, output, **kwargs):
            calls.append(kwargs["deployment"])

        def run(self):
            calls.append("return")
            if fail == "return":
                raise ValueError("injected_return_failure")
            return deepcopy(bundle["return_study"])

        def finish(self):
            return deepcopy(bundle["return_study"])

    monkeypatch.setattr(runner, "simulate", simulate)
    monkeypatch.setattr(runner, "ReplanningExecutor", Executor)
    return bundle, calls


def test_runner_passes_actual_state_and_one_outer_authority_once(monkeypatch, tmp_path):
    expected, calls = prepare_runner(monkeypatch)
    output = tmp_path / "flight"
    study = runner.run_fixture(expected["profile"], "operations_notice", output, run_id="test")
    assert calls[0] == "launch" and calls[2:] == ["return"]
    assert calls[1] == {
        "authority": study["authority"],
        "handoff": study["handoff"],
        "launch": expected["deployment_run"],
    }
    assert study["runtime"]["retry_performed"] is False
    assert json.loads((output / "study.json").read_text()) == study
    assert (output / "deployment-run.json").exists() and (output / "return-study.json").exists()
    with pytest.raises(ValueError, match="fresh_output"):
        runner.run_fixture(expected["profile"], "operations_notice", output)


@pytest.mark.parametrize("failure", ["launch", "return"])
def test_runner_saves_failure_and_observation_prefix_without_retry(monkeypatch, tmp_path, failure):
    expected, calls = prepare_runner(monkeypatch, fail=failure)
    output = tmp_path / "flight"
    study = runner.run_fixture(expected["profile"], "operations_notice", output)
    assert study["runtime_failure"]["message"] == "injected_" + failure + "_failure"
    assert study["trace"] == expected["trace"]
    assert (output / "failure.json").exists() and (output / "study.json").exists()
    assert calls.count("launch") == 1 and calls.count("return") == (failure == "return")
