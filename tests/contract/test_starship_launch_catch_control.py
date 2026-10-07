"""Launch continuation is separately approved and cannot substitute terminal setup.

Worker fixtures test the process admission boundary, not flight performance.
The small real launch ends before separation and must preserve that failure.
"""
from copy import deepcopy
import json
import os
import sys
from types import SimpleNamespace

import pytest

from scripts import run_starship_sixdof as cli
from src.intelligence.starship_mission_planner import plan_starship_request
from src.runtime import starship_mission_control as control
from src.runtime import starship_sixdof_verifier as verifier
from src.runtime.starship_sixdof_catalog import (
    BOOSTER_RECOVERY_POLICY, CATCH_PROFILE, LAUNCH_CATCH_SCENARIO, SIXDOF_PROFILE,
    SIXDOF_SOURCES, recovery_contract,
)
from src.runtime.starship_sixdof_mission import simulate


def planned(tmp_path):
    service = control.StarshipMissionService(tmp_path, planner=lambda text: plan_starship_request(text, "fixture"))
    state = service.plan("launch-catch-operator", "Starship " + LAUNCH_CATCH_SCENARIO)
    return service, state, ("launch-catch-operator", state["plan"]["id"], state["plan"]["sha256"])


def test_launch_plan_requires_separate_explicit_continuation_approval(tmp_path):
    service, state, ref = planned(tmp_path)
    plan = state["plan"]
    assert plan["simulation"]["scenario"] == "launch"
    assert plan["simulation"]["booster_policy"] == BOOSTER_RECOVERY_POLICY
    assert plan["simulation"]["maximum_wall_time_s"] == 900
    assert plan["simulation"]["catch_profile_sha256"] == plan["source_sha256"][CATCH_PROFILE]
    assert plan["booster_recovery"] == recovery_contract()
    assert plan["booster_recovery"]["state_reset_allowed"] is False
    assert "booster_catch" not in plan and "flight_supervision" not in plan
    assert state["approval"] is None and state["execution"] == {}
    with pytest.raises(control.StarshipMissionError, match="explicit_plan_approval_required"):
        service.execute(*ref)
    grant = service.approve(*ref)["approval"]
    assert grant["scope"] == "local_launch_connected_booster_catch_simulation"
    assert grant["booster_recovery"] == plan["booster_recovery"]
    assert grant["physical_execution_authorized"] is False


@pytest.mark.parametrize("change", ["unsigned", "old_scope", "missing_contract", "initialized", "reset", "model_authority"])
def test_unsigned_or_wrong_scope_cannot_dispatch_launch_catch(tmp_path, change):
    service, _, ref = planned(tmp_path)
    service.approve(*ref)
    state = service._load(ref[0])
    grant = state["approval"]
    if change == "unsigned":
        grant.pop("signature")
    elif change == "old_scope":
        grant["scope"] = "local_initialized_booster_catch_simulation"
    elif change == "missing_contract":
        grant.pop("booster_recovery")
    elif change == "initialized":
        grant["booster_recovery"]["initialization"] = "terminal_initialized"
    elif change == "reset":
        grant["booster_recovery"]["state_reset_allowed"] = True
    else:
        grant["booster_recovery"]["model_dispatch_authority"] = True
    if change != "unsigned":
        grant["signature"] = service._signature({key: value for key, value in grant.items() if key != "signature"})
    service._save(ref[0], state)
    with pytest.raises(control.StarshipMissionError, match="approval_signature_invalid|approval_binding"):
        service.execute(*ref)


@pytest.mark.parametrize("source", [CATCH_PROFILE, "src/runtime/starship_booster_control.py",
                                   "src/runtime/starship_booster_recovery.py",
                                   "src/runtime/starship_booster_recovery_verifier.py"])
def test_approved_recovery_sources_must_remain_bound(tmp_path, monkeypatch, source):
    service, state, ref = planned(tmp_path)
    service.approve(*ref)
    monkeypatch.setattr(control, "_sources", lambda _: {**state["plan"]["source_sha256"], source: "changed"})
    with pytest.raises(control.StarshipMissionError, match="approved_source_changed"):
        service.execute(*ref)


@pytest.fixture(scope="module")
def short_launch():
    profile = json.loads((control.REPO / SIXDOF_PROFILE).read_text())
    catch = json.loads((control.REPO / CATCH_PROFILE).read_text())
    run = simulate(profile, scenario="launch", duration_s=.2, booster_policy=BOOSTER_RECOVERY_POLICY, catch_config=catch)
    return json.loads(json.dumps({"schema": "missionos.starship_sixdof_study.v1", "profile": profile,
        "catch_profile": catch, "runs": [run], "provenance": {"booster_policy": BOOSTER_RECOVERY_POLICY,
        "physical_execution_invoked": False, "starship_vehicle_validated": False}}))


def test_real_launch_before_separation_never_becomes_initialized_catch(short_launch):
    run = short_launch["runs"][0]
    assert run["booster_run"] is None and run["booster_catch_run"] is None
    result = verifier.verify_study(short_launch, expected_scenario="launch")
    assert result["passed"] is True, result
    assert result["booster_recovery"]["status"] == "separation_not_reached"
    assert result["booster_recovery"]["handoff_reached"] is False
    assert result["launch_connected_catch_supported"] is False
    assert result["mission_completed"] is False


@pytest.mark.parametrize("change", ["initialized_run", "injected_catch", "handoff", "support", "state_reset", "fixed_policy"])
def test_saved_flags_cannot_turn_failed_launch_into_catch(short_launch, change):
    study = deepcopy(short_launch)
    run = study["runs"][0]
    if change == "initialized_run":
        from src.runtime.starship_booster_catch import simulate_catch
        study["runs"] = [json.loads(json.dumps(simulate_catch(study["profile"], study["catch_profile"], duration_s=.2)))]
    elif change == "injected_catch":
        run["booster_catch_run"] = {"catch_record": {"initialization": {"kind": "terminal_initialized"}}}
    elif change == "handoff":
        run["booster_recovery"]["handoff_reached"] = True
    elif change == "support":
        run["booster_recovery"]["launch_connected_catch_supported"] = True
    elif change == "state_reset":
        run["booster_recovery"]["state_reset"] = True
    else:
        study["provenance"]["booster_policy"] = "fixed_v1"
    result = verifier.verify_study(study, expected_scenario="launch")
    assert result["passed"] is False and result["issues"]
    assert result.get("launch_connected_catch_supported", False) is False


@pytest.mark.parametrize("field,value", [
    ("catch_control_policy", "fixed_v1"),
    ("catch_maximum_duration_s", 12.),
])
def test_saved_recovery_summary_cannot_change_approved_contact_scope(short_launch, field, value):
    study = deepcopy(short_launch)
    study["runs"][0]["booster_recovery"][field] = value
    result = verifier.verify_study(study, expected_scenario="launch")
    assert result["passed"] is False
    assert result["issues"] == [{"code": "inconsistent_output", "path": "$.runs[0]",
        "detail": "Recovery contact policy differs from declared scope"}]
    assert result.get("launch_connected_catch_supported", False) is False
    assert result["mission_completed"] is False


@pytest.mark.parametrize("field,value", [
    (None, None),
    ("control_policy", "fixed_v1"),
    ("requested_duration_s", 12.),
    ("integration_dt_s", .02),
])
def test_saved_contact_scope_rejection_is_not_the_no_separation_guard(short_launch, field, value):
    # A synthetic header deliberately precedes separation: it is not a valid
    # contact run or evidence of a feasible flight. With correct scope it reaches
    # the later no-separation guard; each mutation must fail at scope validation.
    study = deepcopy(short_launch)
    capture = {"control_policy": "net_thrust_trim_v1", "requested_duration_s": 30.,
               "integration_dt_s": study["catch_profile"]["integration_dt_s"]}
    if field is not None:
        assert capture[field] != value
        capture[field] = value
    study["runs"][0]["booster_catch_run"] = {"catch_record": capture}
    result = verifier.verify_study(study, expected_scenario="launch")
    reason = ("Catch cannot precede actual stage separation" if field is None else
              "Contact execution differs from approved policy, time or integration step")
    assert result["passed"] is False
    assert result["issues"] == [{"code": "inconsistent_output", "path": "$.runs[0]", "detail": reason}]
    assert result.get("launch_connected_catch_supported", False) is False
    assert result["mission_completed"] is False


def worker_setup(tmp_path, monkeypatch, short_launch):
    service, state, ref = planned(tmp_path)
    service.approve(*ref)
    monkeypatch.setattr(control, "Thread", lambda **kw: type("NoThread", (), {"start": lambda self: None})())
    monkeypatch.setattr(control.subprocess, "Popen", lambda *a, **kw: type("NoProcess", (), {"pid": os.getpid()})())
    running = service.execute(*ref)
    run_id = running["execution"]["run_id"]
    output = service.root / ("run-" + run_id) / "results"
    output.mkdir()
    sources = set(SIXDOF_SOURCES) - {"src/runtime/starship_sixdof_catalog.py", SIXDOF_PROFILE,
        "src/runtime/starship_sixdof_verifier.py", "src/runtime/starship_retained_return_verifier.py",
        "src/runtime/starship_booster_catch_verifier.py", "src/runtime/starship_booster_recovery_verifier.py", "src/runtime/starship_wind_verifier.py"}
    study = deepcopy(short_launch)
    study["provenance"].update(profile_sha256=state["plan"]["simulation"]["profile_sha256"],
        catch_profile_sha256=state["plan"]["simulation"]["catch_profile_sha256"], dt_scale=1.,
        duration_override_s=None, return_policy="fixed_v1",
        source_sha256={name: state["plan"]["source_sha256"][name] for name in sources})
    (output / "manifest.json").write_text('{"files": {}}')
    return service, ref, run_id, output, study


@pytest.mark.parametrize("change,reason", [
    ("policy", "sixdof_execution_input_binding_mismatch"),
    ("missing_policy", "sixdof_execution_input_binding_mismatch"),
    ("source", "sixdof_execution_input_binding_mismatch"),
    ("digest", "recovery_execution_input_binding_mismatch"),
    ("configuration", "recovery_execution_input_binding_mismatch"),
])
def test_worker_rejects_substituted_recovery_inputs_before_verifier(tmp_path, monkeypatch, short_launch, change, reason):
    service, _, run_id, output, study = worker_setup(tmp_path, monkeypatch, short_launch)
    if change == "policy":
        study["provenance"]["booster_policy"] = "fixed_v1"
    elif change == "missing_policy":
        study["provenance"].pop("booster_policy")
    elif change == "source":
        study["provenance"]["source_sha256"]["src/runtime/starship_booster_recovery.py"] = "0"*64
    elif change == "digest":
        study["provenance"]["catch_profile_sha256"] = "0"*64
    else:
        study["catch_profile"]["maximum_support_force_n"] *= 2
    (output / "study.json").write_text(json.dumps(study))
    calls = []
    monkeypatch.setattr(control, "_run_simulator", lambda args, **kw: calls.append(args) or 0)
    monkeypatch.setattr(verifier, "verify_study", lambda *a, **kw: pytest.fail("unbound input reached verifier"))
    assert control.execute_worker(service.root, run_id) == 2
    assert calls[0][calls[0].index("--booster-policy")+1] == BOOSTER_RECOVERY_POLICY
    assert calls[0][calls[0].index("--scenario")+1] == "launch"
    assert json.loads((output.parent / "result.json").read_text())["failure_reason"] == reason


def test_worker_accepts_verified_failure_without_promoting_catch(tmp_path, monkeypatch, short_launch):
    service, ref, run_id, output, study = worker_setup(tmp_path, monkeypatch, short_launch)
    (output / "study.json").write_text(json.dumps(study))
    timeouts = []
    monkeypatch.setattr(control, "_run_simulator", lambda args, *, timeout: timeouts.append(timeout) or 0)
    assert control.execute_worker(service.root, run_id) == 0
    assert timeouts == [900]
    state = service.status(*ref[:2])
    assert state["status"] == "verified"
    result = state["execution"]["verification"]
    assert result["booster_recovery"]["handoff_reached"] is False
    assert result["launch_connected_catch_supported"] is False
    assert result["mission_completed"] is False


def test_worker_requires_new_planning_contract_after_actual_separation(tmp_path, monkeypatch, short_launch):
    service, _, run_id, output, study = worker_setup(tmp_path, monkeypatch, short_launch)
    # Isolate the worker's contract check from the saved-trajectory checker.
    study["runs"][0]["booster_run"] = {}
    (output / "study.json").write_text(json.dumps(study))
    monkeypatch.setattr(control, "_run_simulator", lambda *a, **kw: 0)
    monkeypatch.setattr(verifier, "verify_study", lambda *a, **kw:
                        {"passed": True, "booster_recovery": {"handoff_reached": False}})
    assert control.execute_worker(service.root, run_id) == 2
    receipt = json.loads((output.parent / "result.json").read_text())
    assert receipt["failure_reason"] == "recovery_capture_planning_contract_missing"
    assert receipt["verification"]["passed"] is True


def test_worker_preserves_primary_verification_failure_without_planning_summary(tmp_path, monkeypatch, short_launch):
    service, ref, run_id, output, study = worker_setup(tmp_path, monkeypatch, short_launch)
    study["runs"][0]["booster_run"] = {}
    (output / "study.json").write_text(json.dumps(study))
    monkeypatch.setattr(control, "_run_simulator", lambda *a, **kw: 0)
    # Use the actual independent verifier: malformed recovery must remain the
    # reported cause, rather than a success-only planning-contract failure.
    assert control.execute_worker(service.root, run_id) == 2
    receipt = json.loads((output.parent / "result.json").read_text())
    assert "failure_reason" not in receipt
    assert receipt["verification_passed"] is False
    issues = receipt["verification"]["issues"]
    assert issues and any("recovery" in issue["detail"].lower() or "booster" in issue["detail"].lower()
                          for issue in issues)
    assert json.loads((output / "verification.json").read_text())["issues"] == issues
    assert service.status(*ref[:2])["status"] == "failed"


def test_receipt_window_covers_the_approved_predictor_runtime(tmp_path, monkeypatch, short_launch):
    service, ref, _, _, _ = worker_setup(tmp_path, monkeypatch, short_launch)
    started = service._load(ref[0])["execution"]["started_at_epoch_s"]
    monkeypatch.setattr(service, "clock", lambda: started+901)
    assert service.status(*ref[:2])["status"] == "running"
    monkeypatch.setattr(service, "clock", lambda: started+1201)
    state = service.status(*ref[:2])
    assert state["status"] == "failed"
    assert state["execution"]["failure_reason"] == "worker_receipt_timeout"


@pytest.mark.parametrize("case", ["failure", "failure_with_eligible_flag", "changed_state", "handoff"])
def test_mission_invokes_contact_only_for_consistent_exact_handoff(monkeypatch, short_launch, case):
    # Synthetic immediate separation shortens this dispatch test. It is not
    # evidence of launch, recovery or handoff feasibility under the real profile.
    from src.runtime import starship_booster_catch as catch
    profile = deepcopy(short_launch["profile"])
    profile["booster"]["separation_reserve_kg"] = profile["booster"]["propellant_kg"]
    received, contact_calls = [], []
    def recover(p, separation, config):
        received.append(deepcopy(separation))
        state = deepcopy(separation)
        eligible = case != "failure"
        handoff = deepcopy(state)
        if case == "changed_state":
            handoff["propellant_kg"] -= 1
        return {"final_state": state, "outcome": {"termination": "surface_impact" if "failure" in case else "catch_handoff"},
                "recovery_record": {"handoff": {"eligible": eligible, "state": handoff}}}
    def contact(p, config, *, initial_state, control_policy, duration_s):
        assert control_policy == "net_thrust_trim_v1" and duration_s == 30.
        contact_calls.append(deepcopy(initial_state))
        return {"outcome": {"simulated_catch_supported": False}}
    monkeypatch.setitem(sys.modules, "src.runtime.starship_booster_recovery", SimpleNamespace(simulate_recovery=recover))
    monkeypatch.setattr(catch, "simulate_catch", contact)
    if case in {"failure_with_eligible_flag", "changed_state"}:
        with pytest.raises(ValueError, match="handoff"):
            simulate(profile, scenario="launch", duration_s=.2, booster_policy=BOOSTER_RECOVERY_POLICY,
                     catch_config=short_launch["catch_profile"])
        assert not contact_calls
    else:
        run = simulate(profile, scenario="launch", duration_s=.2, booster_policy=BOOSTER_RECOVERY_POLICY,
                       catch_config=short_launch["catch_profile"])
        assert received == [run["booster_separation_state"]]
        assert contact_calls == ([run["booster_separation_state"]] if case == "handoff" else [])
        assert run["booster_recovery"]["catch_invoked"] is (case == "handoff")
        assert run["booster_recovery"]["launch_connected_catch_supported"] is False


@pytest.mark.parametrize("scenario,scale", [("booster_catch", "1"), ("gimbal_step", "1"), ("all", "1"), ("launch", ".5")])
def test_cli_never_relabels_initialized_or_changed_step_case_as_launch(monkeypatch, tmp_path, scenario, scale):
    monkeypatch.setattr(sys, "argv", ["run_starship_sixdof.py", "--approve-simulation", "--scenario", scenario,
        "--booster-policy", BOOSTER_RECOVERY_POLICY, "--dt-scale", scale, "--output-dir", str(tmp_path / "out")])
    monkeypatch.setattr(cli, "simulate", lambda *a, **kw: pytest.fail("unapproved scope reached simulator"))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2 and not (tmp_path / "out").exists()


def test_cli_preserves_serialization_failure_without_fabricating_a_study(monkeypatch, tmp_path):
    import numpy as np
    output = tmp_path / "failed"
    monkeypatch.setattr(sys, "argv", ["run_starship_sixdof.py", "--approve-simulation", "--scenario", "launch",
        "--booster-policy", BOOSTER_RECOVERY_POLICY, "--output-dir", str(output)])
    monkeypatch.setattr(cli, "simulate", lambda *a, **kw: {"unexpected_numpy_flag": np.bool_(True)})
    with pytest.raises(TypeError, match="JSON serializable"):
        cli.main()
    failure = json.loads((output / "failure.json").read_text())
    assert failure["status"] == "execution_error"
    assert "TypeError" in failure["error"]
    assert failure["source_sha256"]["scripts/run_starship_sixdof.py"]
    assert not (output / "study.json").exists()
    assert not (output / "report.html").exists()
    assert not (output / "launch.json").exists()
