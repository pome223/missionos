"""Cohort control flow only: execution, physics, providers and Popen forbidden."""
from copy import deepcopy
import socket
import subprocess
from types import SimpleNamespace

import pytest

from scripts import run_starship_final_mission as runner
from src.runtime import starship_final_shared as shared
from src.runtime import starship_sixdof as dyn

# Capture before the autouse fixture replaces runner.execute.
REAL_EXECUTE = runner.execute


@pytest.fixture(autouse=True)
def no_execution(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Cohort contracts cannot execute a mission or call a provider")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(dyn, "step", forbidden)
    monkeypatch.setattr(runner, "execute", forbidden)


@pytest.fixture
def cohort(tmp_path, monkeypatch):
    bounds = {"cohort_wall_s": 6000., "minimum_free_bytes": 10*2**30, "maximum_output_bytes": 6*2**30}
    plans = [{"case_id": case, "run_id": "synthetic-"+case, "mode": "fixture",
        "source_sha256": {"synthetic_source": "a"*64}, "backend": {"synthetic_backend": "bound"},
        "configuration": {"bounds": bounds}, "plan_sha256": str(index)*64}
        for index, case in enumerate(("N", "T", "A"), 1)]
    value = {"schema": "missionos.starship_final_cohort_plan.v1", "plans": plans}
    value["cohort_sha256"] = shared.digest(value)
    path = tmp_path/"cohort.json"
    shared.save(path, value)
    monkeypatch.setattr(runner.planning, "validate_plan", lambda value, **_kwargs: deepcopy(value))
    monkeypatch.setattr(runner, "verify_prerequisites", lambda _plan: None)
    monkeypatch.setattr(runner, "output_path", lambda _name: tmp_path/"owned-output")
    monkeypatch.setattr(runner.shutil, "disk_usage", lambda _path: SimpleNamespace(free=20*2**30))
    return value, path, tmp_path/"owned-output"


def update_cohort(value, path):
    value["cohort_sha256"] = shared.digest({key: val for key, val in value.items() if key != "cohort_sha256"})
    path.unlink()
    shared.save(path, value)


def fake_execute(monkeypatch, *, physical_failure=None, execution_failure=None, call_budget=False):
    calls = []
    def execute(plan_path, approved_hash, output_name, *, cohort_budget):
        plan = shared.read(plan_path)
        calls.append(plan["case_id"])
        assert approved_hash == plan["plan_sha256"] and output_name.endswith(plan["run_id"])
        if call_budget:
            cohort_budget()
        if plan["case_id"] == execution_failure:
            raise ValueError("synthetic_execution_failure")
        return {"passed": True, "outcome": {"all_required_physical_gates_met": plan["case_id"] != physical_failure},
            "synthetic_control_flow_only": True, "provider_calls": 0, "plant_steps": 0}
    monkeypatch.setattr(runner, "execute", execute)
    return calls


def test_fixed_cohort_uses_exact_registered_order_and_one_global_budget(cohort, monkeypatch):
    value, path, output = cohort
    calls = fake_execute(monkeypatch, call_budget=True)
    result = runner.run_cohort(path, value["cohort_sha256"], "unused-test-output")
    assert calls == ["N", "T", "A"]
    assert result["complete"] is True and result["automatic_retry"] is False
    assert shared.read(output/"result.json") == result


@pytest.mark.parametrize("field,value", [("mode", "live"), ("source_sha256", {"other": "b"*64}),
    ("backend", {"synthetic_backend": "other"}), ("configuration", {"bounds": {"cohort_wall_s": 9999.}}),
    ("run_id", "synthetic-N"), ("case_id", "N")])
def test_mixed_or_repeated_cohort_cannot_reach_execute(cohort, field, value):
    registration, path, output = cohort
    registration["plans"][1][field] = value
    update_cohort(registration, path)
    with pytest.raises(ValueError, match="matched_unique"):
        runner.run_cohort(path, registration["cohort_sha256"], "unused-test-output")
    assert not output.exists()


def test_wrong_explicit_cohort_hash_refuses_before_any_start(cohort):
    value, path, output = cohort
    with pytest.raises(ValueError, match="concrete_cohort"):
        runner.run_cohort(path, "0"*64, "unused-test-output")
    assert not output.exists()


def test_fixed_physics_failure_stops_next_case_and_preserves_the_negative_once(cohort, monkeypatch):
    value, path, output = cohort
    calls = fake_execute(monkeypatch, physical_failure="T")
    with pytest.raises(ValueError, match="fixture_gate_failed"):
        runner.run_cohort(path, value["cohort_sha256"], "unused-test-output")
    assert calls == ["N", "T"]
    assert shared.read(output/"T-result.json")["verification"]["outcome"]["all_required_physical_gates_met"] is False
    stopped = shared.read(output/"stopped.json")
    assert stopped["completed_cases"] == ["N", "T"] and stopped["automatic_retry"] is False
    assert not (output/"A-result.json").exists()
    with pytest.raises(FileExistsError):
        runner.run_cohort(path, value["cohort_sha256"], "unused-test-output")
    assert calls == ["N", "T"]


def test_execution_failure_is_not_retried_or_changed_into_a_physical_result(cohort, monkeypatch):
    value, path, output = cohort
    calls = fake_execute(monkeypatch, execution_failure="N")
    with pytest.raises(ValueError, match="synthetic_execution_failure"):
        runner.run_cohort(path, value["cohort_sha256"], "unused-test-output")
    assert calls == ["N"]
    assert shared.read(output/"stopped.json")["completed_cases"] == []
    assert not (output/"N-result.json").exists()


@pytest.mark.parametrize("condition", ["wall", "disk"])
def test_global_budget_exhaustion_refuses_without_starting_any_case(cohort, monkeypatch, condition):
    value, path, output = cohort
    if condition == "wall":
        clock = iter((0., 6000.))
        monkeypatch.setattr(runner.time, "monotonic", lambda: next(clock))
    else:
        monkeypatch.setattr(runner.shutil, "disk_usage", lambda _path: SimpleNamespace(free=9*2**30))
    with pytest.raises(ValueError, match="cohort_.*_budget_stop"):
        runner.run_cohort(path, value["cohort_sha256"], "unused-test-output")
    stopped = shared.read(output/"stopped.json")
    assert stopped["completed_cases"] == [] and stopped["automatic_retry"] is False


def test_live_valid_negative_is_preserved_and_not_rewritten_to_force_physical_success(cohort, monkeypatch):
    value, path, output = cohort
    for plan in value["plans"]:
        plan["mode"] = "live"
    update_cohort(value, path)
    calls = fake_execute(monkeypatch, physical_failure="T")
    result = runner.run_cohort(path, value["cohort_sha256"], "unused-test-output")
    assert calls == ["N", "T", "A"] and result["complete"] is True
    assert result["cases"][1]["verification"]["outcome"]["all_required_physical_gates_met"] is False
    assert not (output/"stopped.json").exists()


# These readiness fixtures represent the trusted saved checker's SMALL output.
# They do not create flight or provider evidence; raw verification stays tested
# separately. Production CLI prerequisite control flow is exercised below.
def readiness_case(case):
    plan = {"case_id": case, "run_id": "synthetic-fixed-"+case, "mode": "fixture",
        "plan_sha256": str({"N": 1, "T": 2, "A": 3}[case])*64,
        "source_sha256": {"src/synthetic.py": "a"*64},
        "backend": {"python": "synthetic", "platform": "synthetic", "machine": "synthetic",
            "numpy": "synthetic", "scipy": "synthetic", "executable_sha256": "b"*64},
        "configuration": {"synthetic": True}, "context": {"run_id": "synthetic-fixed-"+case}}
    normal, changed = case == "N", case == "T"
    selected = "adopt_accelerometer_estimate" if changed else "continue_nominal_assumption"
    sensor = {"native_judgments": 0, "fixture_judgments": 1 if normal else 2,
        "fallback_count": 0, "declined_work": False,
        "tracking_requested_by_accepted_judgment": not normal,
        "independent_guard_receipts_recomputed": 0 if normal else 1,
        "accepted_actions": [selected] if normal else ["request_independent_tracking", selected],
        "application": None if normal else {"mode": "fixture", "action": selected,
            "factor": .80127 if changed else 1., "control_model_changed": changed}}
    checked = {"schema": "missionos.starship_final_native_verification.v3", "passed": True, "issues": [],
        "mode": "fixture", "case_id": case, "plan_sha256": plan["plan_sha256"], "context": plan["context"],
        "source_sha256": shared.digest(plan["source_sha256"]),
        "outcome": {"all_required_physical_gates_met": True}, "checks": {"sensor": sensor},
        "work": {"sensor_fallback_count": 0, "sensor_tracking_requested": not normal,
            "sensor_once_only_application": not normal}}
    return plan, checked


def install_readiness_cli_fixture(directory, monkeypatch, *, stopped_a=False, claimed_ready=True):
    plans, checks = {}, {}
    for case in ("N", "T", "A"):
        plans[case], checks[case] = readiness_case(case)
        shared.save(directory/(case+"-plan.json"), plans[case])
    if stopped_a:
        sensor = checks["A"]["checks"]["sensor"]
        sensor.update(fixture_judgments=1, accepted_actions=["request_independent_tracking"],
            application=None, independent_guard_receipts_recomputed=0, fallback_count=1)
        checks["A"]["work"].update(sensor_fallback_count=1, sensor_once_only_application=False)
    source = plans["N"]["source_sha256"]
    proof = {"schema": "missionos.starship_final_fixture_cohort_proof.v1", "source_sha256": source,
        "backend": plans["N"]["backend"], "configuration_sha256": shared.digest(plans["N"]["configuration"]),
        "pipeline_ready": claimed_ready,  # Deliberately untrusted metadata.
        "cases": [{"case_id": case, "plan_path": case+"-plan.json", "folder": "unused-"+case,
            "plan_sha256": plans[case]["plan_sha256"]} for case in ("N", "T", "A")]}
    proof_path = directory/"proof.json"
    shared.save(proof_path, proof)
    live = {**deepcopy(plans["N"]), "mode": "live", "plan_sha256": "d"*64,
        "prerequisites": {"artifacts": [{"path": "proof.json", "sha256": runner.sha256(proof_path.read_bytes()).hexdigest()}]}}
    live_path = directory/"live-plan.json"
    shared.save(live_path, live)
    calls = []
    monkeypatch.setattr(runner.shared, "relative", lambda name: directory/name)
    monkeypatch.setattr(runner.planning, "validate_plan", lambda row, **_kwargs: deepcopy(row))
    def saved(plan, _folder):
        calls.append(plan["case_id"])
        return deepcopy(checks[plan["case_id"]])
    monkeypatch.setattr(runner, "verify_saved", saved)
    return live_path, live, plans, checks, calls, proof


@pytest.mark.parametrize("case", ["N", "T", "A"])
def test_live_readiness_accepts_exercised_fixture_work_without_forcing_native_models(case):
    plan, verified = readiness_case(case)
    before = deepcopy(verified)
    assert runner.require_fixture_sensor_readiness(plan, verified) == {"case_id": case,
        "fixture_sensor_pipeline_ready": True, "execution_authority_granted": False,
        "native_model_work_established": False}
    assert verified == before
    if case == "T":
        assert verified["checks"]["sensor"]["application"]["factor"] != .8


@pytest.mark.parametrize("case", ["T", "A"])
@pytest.mark.parametrize("mutation", ["no_tracking", "fallback", "declined", "missing_guard",
    "missing_application", "wrong_action", "wrong_control_change", "live_application", "wrong_plan", "wrong_source"])
def test_live_readiness_refuses_incomplete_or_relabelled_diagnosis_even_if_contact_passes(case, mutation):
    plan, verified = readiness_case(case)
    sensor, work = verified["checks"]["sensor"], verified["work"]
    if mutation == "no_tracking":
        sensor["tracking_requested_by_accepted_judgment"] = work["sensor_tracking_requested"] = False
    elif mutation == "fallback":
        sensor["fallback_count"] = work["sensor_fallback_count"] = 1
    elif mutation == "declined":
        sensor["declined_work"] = True
    elif mutation == "missing_guard":
        sensor["independent_guard_receipts_recomputed"] = 0
    elif mutation == "missing_application":
        sensor["application"], work["sensor_once_only_application"] = None, False
    elif mutation == "wrong_action":
        sensor["application"]["action"] = "request_independent_tracking"
    elif mutation == "wrong_control_change":
        sensor["application"]["control_model_changed"] = case != "T"
    elif mutation == "live_application":
        sensor["application"]["mode"] = "live"
    elif mutation == "wrong_plan":
        verified["plan_sha256"] = "f"*64
    else:
        verified["source_sha256"] = "f"*64
    assert verified["passed"] is True and verified["outcome"]["all_required_physical_gates_met"] is True
    with pytest.raises(ValueError, match="final_fixture_sensor_"):
        runner.require_fixture_sensor_readiness(plan, verified)


def test_live_readiness_allows_registered_reconsideration_without_counting_unused_guard_as_application():
    plan, verified = readiness_case("A")
    sensor = verified["checks"]["sensor"]
    sensor.update(fixture_judgments=3, independent_guard_receipts_recomputed=2,
        accepted_actions=["request_independent_tracking", "adopt_accelerometer_estimate", "continue_nominal_assumption"])
    assert runner.require_fixture_sensor_readiness(plan, verified)["fixture_sensor_pipeline_ready"] is True
    sensor["application"], verified["work"]["sensor_once_only_application"] = None, False
    with pytest.raises(ValueError, match="diagnosis_pipeline_not_ready"):
        runner.require_fixture_sensor_readiness(plan, verified)


def test_live_readiness_does_not_manufacture_tracking_or_application_for_normal():
    plan, verified = readiness_case("N")
    assert runner.require_fixture_sensor_readiness(plan, verified)["fixture_sensor_pipeline_ready"] is True
    verified["checks"]["sensor"]["independent_guard_receipts_recomputed"] = 1
    with pytest.raises(ValueError, match="normal_work_changed"):
        runner.require_fixture_sensor_readiness(plan, verified)


def test_cli_validate_recomputes_sensor_work_and_ignores_claimed_ready_before_any_start(tmp_path, monkeypatch, capsys):
    path, _live, _plans, _checks, calls, _proof = install_readiness_cli_fixture(tmp_path, monkeypatch, stopped_a=True)
    with pytest.raises(ValueError, match="pipeline_not_ready"):
        runner.main(["validate", "--plan", str(path)])
    assert calls == ["N", "T", "A"] and capsys.readouterr().out == ""
    assert not (tmp_path/"started.json").exists() and not (tmp_path/"gateway").exists()


def test_cli_validate_real_prerequisite_dispatch_accepts_tiny_trusted_checker_fixtures(tmp_path, monkeypatch, capsys):
    path, _live, _plans, _checks, calls, _proof = install_readiness_cli_fixture(tmp_path, monkeypatch, claimed_ready=False)
    runner.main(["validate", "--plan", str(path)])
    assert shared.parse(capsys.readouterr().out) == {"plan_sha256": "d"*64,
        "validated": True, "physics_steps": 0, "provider_calls": 0}
    assert calls == ["N", "T", "A"] and not (tmp_path/"started.json").exists()


@pytest.mark.parametrize("mutation", ["case", "source", "backend"])
def test_live_prerequisite_refuses_rebinding_a_label_or_different_source_backend(tmp_path, monkeypatch, mutation):
    _path, live, plans, _checks, calls, proof = install_readiness_cli_fixture(tmp_path, monkeypatch)
    if mutation == "case":
        proof["cases"][0].update(plan_path="A-plan.json", plan_sha256=plans["A"]["plan_sha256"])
    else:
        changed = deepcopy(plans["N"])
        changed["source_sha256" if mutation == "source" else "backend"] = {"other": "e"*64}
        path = tmp_path/"N-plan.json"
        path.unlink()
        shared.save(path, changed)
    proof_path = tmp_path/"proof.json"
    proof_path.unlink()
    shared.save(proof_path, proof)
    live["prerequisites"]["artifacts"][0]["sha256"] = runner.sha256(proof_path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="fixture_counterpart_changed"):
        runner.verify_prerequisites(live)
    assert calls == []


def block_live_start_boundaries(monkeypatch):
    """No sockets, process, runtime directory or live-configuration access."""
    from src.runtime import starship_final_outer as outer
    from src.runtime import starship_final_gateway as gateway
    from src.intelligence import starship_m2_broker as broker

    reached = []

    def forbid(name):
        def blocked(*_args, **_kwargs):
            reached.append(name)
            raise AssertionError("Readiness refusal must precede " + name)
        return blocked

    monkeypatch.setattr(runner, "output_path", forbid("output_path"))
    monkeypatch.setattr(gateway, "loopback_gateway", forbid("Gateway"))
    monkeypatch.setattr(outer, "run_final_child", forbid("run_final_child"))
    monkeypatch.setattr(broker, "require_live_configuration", forbid("live_configuration"))
    return reached, forbid


def test_live_execute_refuses_current_a_fixture_before_output_gateway_or_paid_host(tmp_path, monkeypatch):
    path, live, _plans, _checks, calls, _proof = install_readiness_cli_fixture(
        tmp_path, monkeypatch, stopped_a=True)
    before = set(tmp_path.iterdir())
    reached, _forbid = block_live_start_boundaries(monkeypatch)
    # Restore only the original production execute for this focused test.
    # Popen, socket and dyn.step remain forbidden by the autouse fixture.
    monkeypatch.setattr(runner, "execute", REAL_EXECUTE)

    with pytest.raises(ValueError, match="final_fixture_sensor_pipeline_not_ready"):
        runner.execute(path, live["plan_sha256"], "must-not-create-a-live-run")

    assert calls == ["N", "T", "A"]
    assert reached == []
    assert set(tmp_path.iterdir()) == before
    assert not (tmp_path / "started.json").exists()
    assert not (tmp_path / "gateway").exists()


def test_live_cohort_refuses_unready_a_fixture_before_starting_even_n(tmp_path, monkeypatch):
    _path, live, _plans, _checks, calls, _proof = install_readiness_cli_fixture(
        tmp_path, monkeypatch, stopped_a=True)
    # The first live plan's real prerequisite loop must inspect N/T/A fixtures
    # before the cohort creates output or dispatches N. Bounds are intentionally
    # absent: execution must stop before they could be read.
    live_plans = []
    for index, case in enumerate(("N", "T", "A"), 4):
        plan = deepcopy(live)
        plan.update(case_id=case, run_id="synthetic-live-" + case,
            context={"run_id": "synthetic-live-" + case}, plan_sha256=str(index) * 64)
        live_plans.append(plan)
    cohort = {"schema": "missionos.starship_final_cohort_plan.v1", "plans": live_plans}
    cohort["cohort_sha256"] = shared.digest(cohort)
    cohort_path = tmp_path / "live-cohort.json"
    shared.save(cohort_path, cohort)
    before = set(tmp_path.iterdir())
    reached, forbid = block_live_start_boundaries(monkeypatch)
    monkeypatch.setattr(runner, "execute", forbid("execute"))

    with pytest.raises(ValueError, match="final_fixture_sensor_pipeline_not_ready"):
        runner.run_cohort(cohort_path, cohort["cohort_sha256"], "must-not-create-a-live-cohort")

    assert calls == ["N", "T", "A"]
    assert reached == []
    assert set(tmp_path.iterdir()) == before
    assert not (tmp_path / "started.json").exists()
    assert not (tmp_path / "gateway").exists()


def test_utc_sleep_jump_stops_cohort_before_first_mission(cohort,monkeypatch):
    value,path,output=cohort
    calls=fake_execute(monkeypatch)
    wall=iter((1000.,7001.))
    monkeypatch.setattr(runner.time,'time',lambda:next(wall))
    with pytest.raises(ValueError,match='cohort_wall_budget_stop'):
        runner.run_cohort(path,value['cohort_sha256'],'unused-test-output')
    assert calls==[]
    assert shared.read(output/'cohort-timing.json')['timing']['status']=='exceeded'


def test_cohort_rechecks_global_clock_after_the_last_completed_case(cohort,monkeypatch):
    value,path,output=cohort;wall=[1000.]
    monkeypatch.setattr(runner.time,'time',lambda:wall[0])
    calls=fake_execute(monkeypatch);original=runner.execute
    def execute(*a,**kw):
        result=original(*a,**kw)
        if calls[-1]=='A':wall[0]+=6001.
        return result
    monkeypatch.setattr(runner,'execute',execute)
    with pytest.raises(ValueError,match='cohort_wall_budget_stop'):
        runner.run_cohort(path,value['cohort_sha256'],'unused-test-output')
    assert calls==['N','T','A'] and not (output/'result.json').exists()
    assert shared.read(output/'stopped.json')['completed_cases']==['N','T','A']
