"""Capture mechanics require their own bound simulation approval and verifier."""
from copy import deepcopy
import json
import os
import sys

import pytest

from scripts import run_starship_sixdof as cli
from src.gateway import starship_chat
from src.intelligence.starship_mission_planner import plan_starship_request
from src.runtime import starship_mission_control as control
from src.runtime.starship_sixdof_catalog import (
    CATCH_PROFILE, CONTINUOUS_RETURN_POLICY, SIXDOF_PROFILE, SIXDOF_SOURCES,
)


def planned(tmp_path, scenario="sixdof_booster_catch"):
    service = control.StarshipMissionService(tmp_path, planner=lambda text: plan_starship_request(text, "fixture"))
    state = service.plan("catch-operator", "Starship " + scenario)
    return service, state, ("catch-operator", state["plan"]["id"], state["plan"]["sha256"])


@pytest.mark.parametrize("text,scenario", [
    ("Starship ブースターキャッチ", "sixdof_booster_catch"),
    ("Starship タワー故障のキャッチ", "sixdof_booster_catch_tower_unavailable"),
    ("Starship 姿勢連続の帰還", "sixdof_continuous_return_supervised"),
])
def test_fixture_selects_declared_scope(text, scenario):
    result = plan_starship_request(text, "fixture")
    assert result["proposal"]["scenario"] == scenario
    assert result["invocation"]["approval_granted"] is False


def test_catch_scope_disclosed_and_bound_before_dispatch(tmp_path):
    service, state, ref = planned(tmp_path)
    plan = state["plan"]
    assert plan["simulation"]["catch_profile_sha256"] == plan["source_sha256"][CATCH_PROFILE]
    assert "flight_supervision" not in plan and plan["jev_authority"] == "none"
    visible = starship_chat._response("test", "plan", state, ref[0])["message"]
    for text in ("タワー付近に初期化", "寸法と荷重特性は仮定", "実機のキャッチ承認でもありません"):
        assert text in visible
    with pytest.raises(control.StarshipMissionError, match="explicit_plan_approval_required"):
        service.execute(*ref)
    approved = service.approve(*ref)
    assert approved["approval"]["scope"] == "local_initialized_booster_catch_simulation"
    assert approved["approval"]["booster_catch"] == plan["booster_catch"]


@pytest.mark.parametrize("change", ["absent", "scope", "initialization", "authority"])
def test_incomplete_signed_grant_cannot_authorize_catch(tmp_path, change):
    service, _, ref = planned(tmp_path)
    service.approve(*ref)
    state = service._load(ref[0])
    grant = state["approval"]
    if change == "absent":
        grant.pop("booster_catch")
    elif change == "scope":
        grant["scope"] = "local_simulation_only"
    elif change == "initialization":
        grant["booster_catch"]["initialization"] = "launch_connected"
    else:
        grant["booster_catch"]["model_dispatch_authority"] = True
    grant["signature"] = service._signature({k: v for k, v in grant.items() if k != "signature"})
    service._save(ref[0], state)
    with pytest.raises(control.StarshipMissionError, match="approval_binding"):
        service.execute(*ref)


@pytest.mark.parametrize("source", [CATCH_PROFILE, "src/runtime/starship_booster_catch.py",
                                   "src/runtime/starship_booster_catch_verifier.py"])
def test_changed_catch_configuration_or_code_blocks_dispatch(tmp_path, monkeypatch, source):
    service, state, ref = planned(tmp_path)
    service.approve(*ref)
    monkeypatch.setattr(control, "_sources", lambda _: {**state["plan"]["source_sha256"], source: "changed"})
    with pytest.raises(control.StarshipMissionError, match="approved_source_changed"):
        service.execute(*ref)


def worker_setup(tmp_path, monkeypatch):
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
        "src/runtime/starship_booster_catch_verifier.py",
        "src/runtime/starship_booster_recovery_verifier.py", "src/runtime/starship_wind_verifier.py"}
    config = json.loads((control.REPO / CATCH_PROFILE).read_text())
    study = {"schema": "missionos.starship_sixdof_study.v1",
        "profile": json.loads((control.REPO / SIXDOF_PROFILE).read_text()), "catch_profile": config,
        "runs": [{"scenario": "booster_catch", "outcome": {"termination": "time_limit", "duration_s": 12.},
                  "catch_record": {"initialization": {"kind": "terminal_initialized", "launch_connected": False},
                                   "integration_dt_s": config["integration_dt_s"], "requested_duration_s": config["duration_s"]}}],
        "provenance": {"profile_sha256": state["plan"]["simulation"]["profile_sha256"],
            "catch_profile_sha256": state["plan"]["simulation"]["catch_profile_sha256"],
            "dt_scale": 1.0, "duration_override_s": None, "return_policy": "fixed_v1", "booster_policy": "fixed_v1",
            "physical_execution_invoked": False, "starship_vehicle_validated": False,
            "source_sha256": {name: state["plan"]["source_sha256"][name] for name in sources}}}
    (output / "manifest.json").write_text('{"files": {}}')
    return service, ref, run_id, output, study


@pytest.mark.parametrize("changed", ["configuration", "digest", "scenario", "supplied_state", "dt", "duration", "control_policy",
                                     "schema", "physical_claim", "fidelity_claim"])
def test_worker_rejects_input_substitution_before_verification(tmp_path, monkeypatch, changed):
    from src.runtime import starship_booster_catch_verifier as verifier
    service, _, run_id, output, study = worker_setup(tmp_path, monkeypatch)
    record = study["runs"][0]["catch_record"]
    if changed == "configuration":
        study["catch_profile"]["maximum_support_force_n"] *= 2
    elif changed == "digest":
        study["provenance"]["catch_profile_sha256"] = "0" * 64
    elif changed == "scenario":
        study["runs"][0]["scenario"] = "booster_catch_tower_unavailable"
    elif changed == "supplied_state":
        record["initialization"]["kind"] = "supplied_state"
    elif changed == "dt":
        record["integration_dt_s"] /= 2
    elif changed == "duration":
        record["requested_duration_s"] = 1
    elif changed == "control_policy":
        record["control_policy"] = "net_thrust_trim_v1"
    elif changed == "schema":
        study["schema"] = "unrelated.study.v1"
    elif changed == "physical_claim":
        study["provenance"]["physical_execution_invoked"] = True
    else:
        study["provenance"]["starship_vehicle_validated"] = True
    (output / "study.json").write_text(json.dumps(study))
    monkeypatch.setattr(control, "_run_simulator", lambda args: 0)
    monkeypatch.setattr(verifier, "verify_catch", lambda *a: pytest.fail("unbound input must not reach verifier"))
    assert control.execute_worker(service.root, run_id) == 2
    result = json.loads((output.parent / "result.json").read_text())
    assert result["failure_reason"] == "catch_execution_input_binding_mismatch"


@pytest.mark.parametrize("passed", [True, False])
def test_worker_uses_independent_catch_verdict_preserving_unsuccessful_outcome(tmp_path, monkeypatch, passed):
    from src.runtime import starship_booster_catch_verifier as verifier
    service, ref, run_id, output, study = worker_setup(tmp_path, monkeypatch)
    (output / "study.json").write_text(json.dumps(study))
    seen = []
    def verify(*args):
        seen.append(deepcopy(args))
        return {"passed": passed, "issues": [] if passed else [{"code": "bad_support"}], "simulated_catch_supported": False}
    monkeypatch.setattr(verifier, "verify_catch", verify)
    monkeypatch.setattr(control, "_run_simulator", lambda args: 0)
    assert control.execute_worker(service.root, run_id) == (0 if passed else 2)
    assert seen == [(study["runs"][0], study["profile"], study["catch_profile"])]
    state = service.status(*ref[:2])
    assert state["status"] == ("verified" if passed else "failed")
    if passed:
        visible = starship_chat._response("/status", "status", state, ref[0])["message"]
        assert "アームによる連続支持 未達" in visible
        assert "ミッション成功の認定ではありません" in visible


def test_continuous_plan_binds_v3_independent_of_supervisor(tmp_path, monkeypatch):
    monkeypatch.setenv("MISSIONOS_STARSHIP_FLIGHT_SUPERVISOR_MODE", "fixture")
    service, state, ref = planned(tmp_path, "sixdof_continuous_return_supervised")
    assert state["plan"]["return_policy"]["policy_id"] == CONTINUOUS_RETURN_POLICY == "mass_state_terminal_v3"
    assert state["plan"]["return_policy"]["provider_decision_required"] is False
    assert service.approve(*ref)["approval"]["return_policy"] == state["plan"]["return_policy"]


@pytest.mark.parametrize("dt", ["0", "nan", "inf", "1.5", "1e-300", "1e-6"])
def test_cli_refuses_invalid_catch_step_before_start(monkeypatch, tmp_path, dt):
    monkeypatch.setattr(sys, "argv", ["run_starship_sixdof.py", "--approve-simulation", "--scenario", "booster_catch",
                                     "--dt-scale", dt, "--output-dir", str(tmp_path / "out")])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert not (tmp_path / "out").exists()
