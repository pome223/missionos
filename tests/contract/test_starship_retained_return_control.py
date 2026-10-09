"""Retained-payload guidance requires a separate bound preflight scope."""
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
    FIXED_RETURN_POLICY, RETAINED_RETURN_POLICY, RETAINED_RETURN_SCENARIO,
    SIXDOF_PROFILE, SIXDOF_SOURCES, retained_return_contract,
)


def planned(tmp_path, monkeypatch, scenario=RETAINED_RETURN_SCENARIO):
    monkeypatch.setenv("MISSIONOS_STARSHIP_FLIGHT_SUPERVISOR_MODE", "fixture")
    service = control.StarshipMissionService(tmp_path, planner=lambda text: plan_starship_request(text, "fixture"))
    state = service.plan("retained-operator", "Starship " + scenario)
    return service, state, ("retained-operator", state["plan"]["id"], state["plan"]["sha256"])


@pytest.mark.parametrize("text", ["Starship 六自由度で衛星を保持した帰還", "Starship 衛星保持の帰還計画",
                                  "Starship retained-payload return", "Starship " + RETAINED_RETURN_SCENARIO])
def test_fixture_planner_selects_retained_scope_without_inventing_approval(text):
    result = plan_starship_request(text, "fixture")
    assert result["proposal"]["scenario"] == RETAINED_RETURN_SCENARIO
    assert result["invocation"]["approval_granted"] is False


def test_new_plan_discloses_fixed_return_guidance_and_binds_approval(tmp_path, monkeypatch):
    service, state, ref = planned(tmp_path, monkeypatch)
    plan = state["plan"]
    assert plan["simulation"]["scenario"] == "deployment_no_effect"
    assert plan["simulation"]["return_policy"] == RETAINED_RETURN_POLICY
    assert plan["return_policy"] == retained_return_contract()
    assert plan["return_policy"]["provider_decision_required"] is False
    assert plan["flight_supervision"]["allowed_actions"] == ["hold", "skip_remaining_deployment"]
    assert plan["flight_supervision"]["maximum_commands"] == 1
    visible = starship_chat._response("test", "plan", state, ref[0])["message"]
    for text in (RETAINED_RETURN_POLICY, "固定の終端誘導アルゴリズム", "成否には依存しません", "安全な帰還を保証"):
        assert text in visible
    assert state["approval"] is None and state["execution"] == {}
    with pytest.raises(control.StarshipMissionError, match="explicit_plan_approval_required"):
        service.execute(*ref)
    approved = service.approve(*ref)
    assert approved["approval"]["return_policy"] == plan["return_policy"]
    assert approved["approval"]["scope"] == "local_simulation_and_bounded_flight_supervision_and_retained_return"
    assert approved["approval"]["physical_execution_authorized"] is False


def test_old_supervised_plan_keeps_original_guidance_and_scope(tmp_path, monkeypatch):
    service, state, ref = planned(tmp_path, monkeypatch, "sixdof_deployment_supervised")
    assert state["plan"]["simulation"]["return_policy"] == FIXED_RETURN_POLICY
    assert "return_policy" not in state["plan"]
    approved = service.approve(*ref)
    assert "return_policy" not in approved["approval"]
    assert approved["approval"]["scope"] == "local_simulation_and_bounded_flight_supervision"


@pytest.mark.parametrize("change", ["absent", "scope", "policy", "activation"])
def test_incomplete_signed_grant_cannot_authorize_retained_guidance(tmp_path, monkeypatch, change):
    service, _, ref = planned(tmp_path, monkeypatch)
    service.approve(*ref)
    state = service._load(ref[0])
    grant = state["approval"]
    if change == "absent":
        grant.pop("return_policy")
    elif change == "scope":
        grant["scope"] = "local_simulation_and_bounded_flight_supervision"
    elif change == "policy":
        grant["return_policy"]["policy_id"] = FIXED_RETURN_POLICY
    else:
        grant["return_policy"]["activation"] = "model_proposal"
    grant["signature"] = service._signature({key: value for key, value in grant.items() if key != "signature"})
    service._save(ref[0], state)
    with pytest.raises(control.StarshipMissionError, match="approval_binding"):
        service.execute(*ref)


@pytest.mark.parametrize("source", ["src/runtime/starship_retained_return.py", "src/runtime/starship_retained_return_verifier.py", "src/runtime/starship_ship_return.py"])
def test_return_algorithm_and_verifier_are_covered_by_approval(tmp_path, monkeypatch, source):
    service, state, ref = planned(tmp_path, monkeypatch)
    service.approve(*ref)
    assert source in state["plan"]["source_sha256"]
    monkeypatch.setattr(control, "_sources", lambda _: {**state["plan"]["source_sha256"], source: "changed"})
    with pytest.raises(control.StarshipMissionError, match="approved_source_changed"):
        service.execute(*ref)


@pytest.mark.parametrize("scenario", ["launch", "gimbal_step", "all"])
def test_cli_refuses_retained_policy_for_unapproved_runtime_scope(monkeypatch, tmp_path, scenario):
    monkeypatch.setattr(sys, "argv", ["run_starship_sixdof.py", "--approve-simulation", "--scenario", scenario,
                                     "--return-policy", RETAINED_RETURN_POLICY, "--output-dir", str(tmp_path / "out")])
    monkeypatch.setattr(cli, "simulate", lambda *args, **kwargs: pytest.fail("must reject before simulator"))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert not (tmp_path / "out").exists()


def worker_setup(tmp_path, monkeypatch):
    service, state, ref = planned(tmp_path, monkeypatch)
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
    study = {"profile": json.loads((control.REPO / SIXDOF_PROFILE).read_text()),
             "runs": [{"scenario": "deployment_no_effect"}],
             "provenance": {"profile_sha256": state["plan"]["simulation"]["profile_sha256"],
                            "dt_scale": 1.0, "duration_override_s": None, "return_policy": RETAINED_RETURN_POLICY, "booster_policy": "fixed_v1",
                            "source_sha256": {name: state["plan"]["source_sha256"][name] for name in sources}}}
    (output / "manifest.json").write_text('{"files": {}}')
    return service, state, ref, run_id, output, study


def test_worker_rejects_unapproved_return_policy_even_when_process_exits_zero(tmp_path, monkeypatch):
    service, _, ref, run_id, output, study = worker_setup(tmp_path, monkeypatch)
    study["provenance"]["return_policy"] = FIXED_RETURN_POLICY
    (output / "study.json").write_text(json.dumps(study))
    seen = []
    monkeypatch.setattr(control, "_run_simulator", lambda args: seen.append(args) or 0)
    assert control.execute_worker(service.root, run_id) == 2
    assert seen[0][seen[0].index("--return-policy") + 1] == RETAINED_RETURN_POLICY
    assert "--supervision-dir" in seen[0]
    result = service.status(*ref[:2])
    assert result["execution"]["failure_reason"] == "sixdof_execution_input_binding_mismatch"


@pytest.mark.parametrize("policy_passed", [True, False])
def test_worker_requires_independent_return_verification(tmp_path, monkeypatch, policy_passed):
    from src.runtime import starship_sixdof_verifier as flight_verifier
    from src.runtime import starship_flight_supervision_verifier as supervisor_verifier
    from src.runtime import starship_retained_return_verifier as return_verifier
    service, _, ref, run_id, output, study = worker_setup(tmp_path, monkeypatch)
    (output / "study.json").write_text(json.dumps(study))
    monkeypatch.setattr(control, "_run_simulator", lambda args: 0)
    monkeypatch.setattr(flight_verifier, "verify_study", lambda *a, **kw: {"passed": True})
    monkeypatch.setattr(supervisor_verifier, "verify_supervision", lambda *a, **kw: {"passed": True})
    seen = []
    def verify(run, profile, *, expected_policy):
        seen.append((deepcopy(run), deepcopy(profile), expected_policy))
        return {"passed": policy_passed, "policy_id": expected_policy}
    monkeypatch.setattr(return_verifier, "verify_retained_return", verify)
    assert control.execute_worker(service.root, run_id) == (0 if policy_passed else 2)
    assert seen == [(study["runs"][0], study["profile"], RETAINED_RETURN_POLICY)]
    result = service.status(*ref[:2])
    assert result["status"] == ("verified" if policy_passed else "failed")
    assert result["execution"]["verification"]["retained_return"]["passed"] is policy_passed
