"""Six-DOF MissionOS dispatch binds configuration and preserves failed outcomes."""

import json
import os
import time

import pytest

from src.intelligence.starship_mission_planner import plan_starship_request
from src.runtime import starship_mission_control as control
from src.runtime.starship_sixdof_catalog import SIXDOF_PROFILE, SIXDOF_SCENARIOS, SIXDOF_SOURCES


@pytest.mark.parametrize("utterance,scenario", [
    ("Starship 六自由度で打ち上げから帰還", "sixdof_launch"),
    ("Starship 6DOF エンジン故障", "sixdof_engine_out"),
    ("Starship 6DOF engine-out", "sixdof_engine_out"),
    ("Starship 6DOF engine_out", "sixdof_engine_out"),
    ("Starship 六軸のジンバル応答", "sixdof_gimbal_step"),
    ("Starship 6自由度のフラップ非対称", "sixdof_flap_asymmetry"),
    ("Starship 6dof 再突入の摂動", "sixdof_entry_perturbation"),
    *[("Starship " + scenario, scenario) for scenario in SIXDOF_SCENARIOS],
])
def test_sixdof_selection_is_explicit_and_does_not_infer_approval(utterance, scenario):
    result = plan_starship_request(utterance, "fixture")
    assert result["proposal"]["scenario"] == scenario
    assert result["invocation"]["approval_granted"] is False
    assert result["invocation"]["model_inference_invoked"] is False


def _service(tmp_path):
    return control.StarshipMissionService(
        tmp_path, planner=lambda text: plan_starship_request(text, "fixture"))


def _plan(service, scenario="sixdof_gimbal_step"):
    state = service.plan("sixdof-operator", "Starship " + scenario)
    return state, ("sixdof-operator", state["plan"]["id"], state["plan"]["sha256"])


def test_plan_discloses_exact_backend_fixed_inputs_and_control_authority(tmp_path):
    state, _ = _plan(_service(tmp_path))
    plan = state["plan"]
    assert plan["backend"] == "starship_sixdof"
    assert plan["simulation"] == {
        "scenario": "gimbal_step", "profile": SIXDOF_PROFILE,
        "profile_id": json.loads((control.REPO / SIXDOF_PROFILE).read_text())["profile_id"],
        "profile_sha256": plan["source_sha256"][SIXDOF_PROFILE],
        "dt_scale": 1.0, "duration_override_s": None, "maximum_wall_time_s": 300,
        "return_policy": "fixed_v1", "booster_policy": "fixed_v1",
    }
    assert set(SIXDOF_SOURCES) <= plan["source_sha256"].keys()
    assert plan["jev_authority"] == "none"
    assert plan["model_authority"] == "planning_only"
    assert state["approval"] is None and state["execution"] == {}


def test_operator_sees_configuration_before_approval_and_can_request_sixdof_followup(tmp_path, monkeypatch):
    from src.gateway import starship_chat
    service = _service(tmp_path)
    monkeypatch.setenv("MISSIONOS_STARSHIP_PLANNER_MODE", "fixture")
    monkeypatch.setattr(starship_chat, "get_starship_service", lambda: service)
    response = starship_chat.maybe_handle_starship_chat({"session_id": "operator", "message": "sixdof_launch"})
    plan = response["operation_result"]["plan"]
    for visible in (plan["simulation"]["profile_id"], plan["simulation"]["profile_sha256"],
                    "300秒", plan["verification_scope"], *plan["limitations"]):
        assert visible in response["message"]
    followup = starship_chat.maybe_handle_starship_chat({
        "session_id": "operator", "message": "六軸でエンジン故障",
        "starship_context": response["starship_context"],
    })
    assert followup["operation_result"]["plan"]["scenario"] == "sixdof_engine_out"
    assert followup["operation_result"]["approval"] is None


@pytest.mark.parametrize("source", [SIXDOF_PROFILE, "src/runtime/starship_sixdof_verifier.py",
                                   "src/runtime/starship_sixdof_mission.py"])
def test_changed_sixdof_configuration_or_verifier_blocks_dispatch(tmp_path, monkeypatch, source):
    service = _service(tmp_path)
    state, ref = _plan(service)
    service.approve(*ref)
    changed = {**state["plan"]["source_sha256"], source: "changed"}
    monkeypatch.setattr(control, "_sources", lambda _: changed)
    with pytest.raises(control.StarshipMissionError, match="approved_source_changed"):
        service.execute(*ref)


def test_real_short_sixdof_worker_and_signed_verified_artifacts(tmp_path, monkeypatch):
    service = _service(tmp_path)
    state, ref = _plan(service)
    # The worker and simulator must not inherit planner credentials.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-sentinel-no-secret")
    service.approve(*ref)
    running = service.execute(*ref)
    assert running["execution"]["worker_pid"] != os.getpid()
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        result = service.status(ref[0], ref[1])
        if result["status"] != "running":
            break
        time.sleep(.05)
    assert result["status"] == "verified", result
    assert result["execution"]["worker_receipt_verified"] is True
    assert result["execution"]["provider_credentials_present"] is False
    assert result["mission_completed"] is False
    assert result["physical_execution"] is False
    study = json.loads(service.read_artifact(*ref[:2], "study.json").read_text())
    assert study["schema"] == "missionos.starship_sixdof_study.v1"
    assert study["runs"][0]["scenario"] == "gimbal_step"
    assert study["runs"][0]["outcome"]["termination"] == "time_limit"
    assert study["provenance"]["duration_override_s"] is None
    assert study["provenance"]["profile_sha256"] == state["plan"]["simulation"]["profile_sha256"]
    verdict = json.loads(service.read_artifact(*ref[:2], "verification.json").read_text())
    assert verdict["passed"] is True
    assert verdict["mission_completed"] is False
    manifest = json.loads(service.read_artifact(*ref[:2], "manifest.json").read_text())
    assert manifest["files"]["verification.json"] == result["execution"]["artifact_sha256"]["verification.json"]


@pytest.mark.parametrize("study", [{"provenance": {"profile_sha256": "wrong"}}, [], {"provenance": []}])
def test_worker_rejects_different_actual_inputs_even_when_process_returns_zero(tmp_path, monkeypatch, study):
    service = _service(tmp_path)
    _, ref = _plan(service)
    service.approve(*ref)
    # Prepare the signed request without starting an external worker.
    monkeypatch.setattr(control, "Thread", lambda **kw: type("NoThread", (), {"start": lambda self: None})())
    monkeypatch.setattr(control.subprocess, "Popen", lambda *a, **kw: type("NoProcess", (), {"pid": os.getpid()})())
    running = service.execute(*ref)
    run_id = running["execution"]["run_id"]
    output = service.root / ("run-" + run_id) / "results"
    output.mkdir()
    (output / "study.json").write_text(json.dumps(study))
    monkeypatch.setattr(control, "_run_simulator", lambda args: 0)
    assert control.execute_worker(service.root, run_id) == 2
    result = service.status(ref[0], ref[1])
    assert result["status"] == "failed"
    assert result["execution"]["failure_reason"] == "sixdof_execution_input_binding_mismatch"
    assert not result["execution"].get("artifacts")
