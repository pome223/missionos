"""Prefix/return composition guards. Every numerical actor is stubbed."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from src.runtime import starship_m2_runner as m2
from src.runtime import starship_m3_fixture as m3
from src.runtime.starship_replanning import digest


@pytest.fixture
def inputs(monkeypatch):
    profile = json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())
    authority = m3.contract("normal")
    state = {"time_s":1000.7000000000407, "identity":"exact executed state"}
    launch = {"final_state":state, "mission_director":{"records":[]},
        "booster_run":{"shared_clock_checkpoint":{"unfinished":False},
            "outcome":{"termination":"surface_contact","simulated_contact_envelope_met":False},
            "contact":{"contact":True,"surface_relative_speed_mps":8.285}, "final_state":{"time_s":609.1}}}
    prefix = {"authority":authority["prefix_authority"], "run_id":"m2-fixture", "profile":profile,
        "prefix_run":launch, "handoff":{"execution_state":deepcopy(state)}, "trace":[]}
    monkeypatch.setattr(m3,"verify_prefix",lambda _: {"passed":True,"handoff_ready":True})
    return profile, authority, prefix


def test_terminal_booster_failure_is_preserved_not_called_success(inputs):
    profile, authority, prefix = inputs
    assert m3.validate_prefix(prefix,authority["m2_authority"],profile)["passed"]
    assert prefix["prefix_run"]["booster_run"]["outcome"]["simulated_contact_envelope_met"] is False


@pytest.mark.parametrize("mutate",[
    lambda p:p["prefix_run"]["booster_run"]["shared_clock_checkpoint"].update(unfinished=True),
    lambda p:p["prefix_run"]["booster_run"]["outcome"].update(termination="time_limit"),
    lambda p:p["prefix_run"]["booster_run"].update(contact=None),
    lambda p:p["prefix_run"]["booster_run"]["contact"].update(contact=False),
    lambda p:p["prefix_run"]["booster_run"]["final_state"].update(time_s=1100.),
    lambda p:p["handoff"]["execution_state"].update(identity="predicted reset"),
    lambda p:p["authority"]["m2_authority"].update(case="release_fault"),
    lambda p:p["profile"].update(unapproved=True),
])
def test_no_silent_freeze_or_authority_expansion(inputs,mutate):
    profile, authority, prefix = inputs
    original_profile, inner = deepcopy(profile), deepcopy(authority["m2_authority"])
    mutate(prefix)
    with pytest.raises(ValueError):
        m3.validate_prefix(prefix,inner,original_profile)


def test_unverified_prefix_cannot_construct_return(inputs,monkeypatch):
    profile, authority, prefix = inputs
    monkeypatch.setattr(m3,"verify_prefix",lambda _: {"passed":True,"handoff_ready":False})
    with pytest.raises(ValueError,match="handoff_unavailable"):
        m3.validate_prefix(prefix,authority["m2_authority"],profile)


def setup_runtime(inputs, monkeypatch):
    profile, authority, prefix = inputs
    manifest={"examples/spaceflight/starship-sixdof-profile.json":digest(profile)}
    monkeypatch.setattr(m3,"sources",lambda:manifest)
    monkeypatch.setattr(m2,"return_sources",lambda:manifest)
    monkeypatch.setattr(m2,"readiness",lambda _: (None,None,None))
    monkeypatch.setattr(m3,"produce_prefix",lambda *args, **kwargs:deepcopy(prefix))
    from src.runtime import starship_m2_control
    monkeypatch.setattr(starship_m2_control,"validate_worker_input",lambda *args,**kwargs:None)
    def forbidden(*args,**kwargs):
        raise AssertionError("must not restart sequential launch")
    monkeypatch.setattr(m2,"simulate",forbidden)
    return profile, authority, prefix


def test_actual_m2_hook_passes_exact_prefix_to_existing_actor(inputs,monkeypatch,tmp_path):
    profile, authority, prefix = setup_runtime(inputs,monkeypatch)
    constructed=[]
    class Actor:
        def __init__(self,p,envelope,case,output,**kwargs):
            constructed.append(kwargs)
            assert kwargs["deployment"] == {"authority":authority["m2_authority"],
                "handoff":prefix["handoff"],"launch":prefix["prefix_run"]}
        def run(self):
            return {"decisions":[],"execution_origin":prefix["prefix_run"]["final_state"]}
    monkeypatch.setattr(m2,"ReplanningExecutor",Actor)
    study=m2.run_simulation(profile,"normal",tmp_path/"run",authority=authority["m2_authority"],concurrent_prefix=True)
    from src.runtime.starship_artifacts import read_verified_input, CHECKPOINT_SCHEMA
    assert json.loads((tmp_path/"run/study.json").read_text())["schema"] == CHECKPOINT_SCHEMA
    assert read_verified_input(tmp_path/"run/study.json") == study
    assert read_verified_input(tmp_path/"run/return-study.json") == study["return_study"]
    assert len(constructed)==1 and "runtime_failure" not in study
    assert study["runtime"]["continuation_started"] is True
    assert study["deployment_run"]==prefix["prefix_run"]
    assert study["return_study"]["execution_origin"]==study["handoff"]["execution_state"]


def test_unfinished_booster_saves_negative_prefix_and_never_runs_actor(inputs,monkeypatch,tmp_path):
    profile, authority, prefix=setup_runtime(inputs,monkeypatch)
    prefix["prefix_run"]["booster_run"]["shared_clock_checkpoint"]["unfinished"]=True
    monkeypatch.setattr(m2,"ReplanningExecutor",lambda *a,**k:pytest.fail("no return after unfinished booster"))
    study=m2.run_simulation(profile,"normal",tmp_path/"run",authority=authority["m2_authority"],concurrent_prefix=True)
    assert study["runtime"]["continuation_started"] is False
    assert study["concurrent_prefix"]["prefix_run"]==prefix["prefix_run"]
    assert study["runtime_failure"]["message"]=="m3_unfinished_booster_requires_concurrent_return_scheduler"
    assert (tmp_path/"run/failure.json").is_file()


@pytest.mark.parametrize("mode,value,reason",[
    ("live",True,"registered_live_outer_grant_required"),
    ("fixture",1,"concurrent_prefix_flag"),("fixture","true","concurrent_prefix_flag")])
def test_hook_requires_explicit_registered_live_grant_and_boolean_flag(tmp_path,mode,value,reason):
    with pytest.raises(ValueError,match=reason):
        m2.run_simulation({},"normal",tmp_path/"run",mode=mode,concurrent_prefix=value)
    assert not (tmp_path/"run").exists()


def test_outer_authority_precedes_execution_and_keeps_claims_bounded(inputs,monkeypatch,tmp_path):
    profile, authority, prefix=inputs
    def run(p,case,out,**kwargs):
        assert json.loads((out.parent/"authority.json").read_text())==authority
        assert kwargs["concurrent_prefix"] is True
        return {"concurrent_prefix":prefix,"source_sha256":{},"return_source_sha256":{}}
    monkeypatch.setattr(m2,"run_simulation",run)
    from src.runtime import starship_m2_verifier
    monkeypatch.setattr(starship_m2_verifier,"verify",lambda *a,**k:{"passed":True,"case_accepted":True})
    study=m3.run_fixture(profile,"normal",tmp_path/"full")
    assert study["same_state_continuation_checked"] is True
    assert study["m3_completed"] is study["mission_completed"] is study["physical_execution"] is False
    assert study["booster_outcome"]["simulated_contact_envelope_met"] is False


def runtime_bundle(run_id="m2-fixture"):
    return {"inflight": {
        "schema":"missionos.inflight_executor_contract.v1",
        "context":{"run_id":run_id+"-return","mission_id":"fixture-mission","vehicle_id":"ship"},
        "initial_scope":{"commands":["select_nominal","keep_reserved_return"],
                         "resource_limits":{"maximum_return_delay_s":6000.}},
        "bounds":{"simulation_deadline_s":2100.,"wall_deadline_s":9000.,
                  "maximum_observation_age_s":5.,"reservation_cutoff_s":2220.7}},
        "request_writer":object(),"grant_reader":object(),"snapshot_sink":lambda _:None,"clock":lambda:1.}


def test_registered_inflight_class_only_and_no_ipc_in_artifacts(inputs,monkeypatch,tmp_path):
    import sys
    from types import ModuleType
    profile, authority, prefix=setup_runtime(inputs,monkeypatch)
    bundle,constructed=runtime_bundle(),[]
    registered=ModuleType("src.runtime.starship_inflight_executor")
    class RegisteredActor:
        def __init__(self,*args,**kwargs):
            constructed.append(kwargs)
        def run(self):
            return {"decisions":[],"inflight_approval":{"contract":bundle["inflight"]}}
    registered.InflightReplanningExecutor=RegisteredActor
    registered.validate_inflight_executor_contract=lambda *a,**k:None
    monkeypatch.setitem(sys.modules,registered.__name__,registered)
    monkeypatch.setattr(m3,"sources",lambda **kwargs:{})
    monkeypatch.setattr(m2,"return_sources",lambda:{})
    monkeypatch.setattr(m2,"ReplanningExecutor",lambda *a,**k:pytest.fail("must use registered actor"))
    study=m2.run_simulation(profile,"normal",tmp_path/"run",authority=authority["m2_authority"],
                            concurrent_prefix=True,inflight_runtime=bundle)
    assert "runtime_failure" not in study and len(constructed)==1
    assert constructed[0]["request_writer"] is bundle["request_writer"]
    assert constructed[0]["inflight"]==bundle["inflight"]
    saved=json.loads((tmp_path/"run/inputs.json").read_text())
    assert saved["inflight_contract"]==bundle["inflight"]
    assert all(key not in saved for key in {"request_writer","grant_reader","snapshot_sink","clock"})
    assert study["runtime"]["registered_inflight_executor"] is True
    assert study["runtime"]["boundary"]=="local_cli_fixture_continuation"


@pytest.mark.parametrize("change",[
    lambda b:b.update(executor_class=lambda:None),
    lambda b:b.update(operator_token="unacceptable"),
    lambda b:b.update(snapshot_sink="unregistered JSON callback"),
    lambda b:b["inflight"]["context"].update(run_id="another-return"),
    lambda b:b["inflight"].update(signing_key="unacceptable"),
])
def test_runtime_seam_rejects_extra_authority_or_wrong_identity_before_launch(tmp_path,change):
    bundle=runtime_bundle()
    change(bundle)
    with pytest.raises(ValueError,match="m3_"):
        m2.run_simulation({},"normal",tmp_path/"run",concurrent_prefix=True,inflight_runtime=bundle)
    assert not (tmp_path/"run").exists()


def test_inflight_seam_unavailable_to_ordinary_m2_run(tmp_path):
    with pytest.raises(ValueError,match="registered_inflight_runtime_required"):
        m2.run_simulation({},"normal",tmp_path/"run",inflight_runtime=runtime_bundle())
    assert not (tmp_path/"run").exists()


def test_outer_contract_binds_initial_amendment_limits_without_claiming_approval():
    inflight=runtime_bundle()["inflight"]
    authority=m3.contract("normal",inflight=inflight)
    assert authority["inflight_contract_sha256"]==digest(inflight)
    assert authority["maximum_inflight_scope_amendments"]==1
    assert authority["maximum_human_inflight_commands"]==0
    inflight["context"]["run_id"]="mutated"
    assert authority["inflight_contract"]["context"]["run_id"]!="mutated"



def test_real_composition_prepares_nested_prefix_before_first_physical_step(tmp_path, monkeypatch):
    from src.runtime import starship_m3_prefix as prefix
    from src.runtime.starship_replanning import ROOT
    profile = json.loads((ROOT / "examples/spaceflight/starship-sixdof-profile.json").read_text())
    output, calls = tmp_path / "new-parent" / "flight", []
    def before_physics(_profile, **kwargs):
        calls.append(True)
        directory = output / "mission/concurrent-prefix"
        authority = json.loads((directory / "authority.json").read_text())
        binding = json.loads((directory / "approval-binding.json").read_text())
        assert binding["outer_authority_sha256"] == digest(authority)
        assert (directory / "source-sha256.json").is_file()
        assert kwargs["splashdown_goal"].to_dict() == authority["booster_authority"]["splashdown_goal"]
        assert kwargs["mission_director"].ship.run_id == "directory-boundary-deployment"
        raise RuntimeError("test_stop_before_first_physical_step")
    monkeypatch.setattr(prefix, "simulate_concurrent", before_physics)
    monkeypatch.setattr(m2, "ReplanningExecutor", lambda *a, **k: pytest.fail("return must not start"))
    result = m3.run_fixture(profile, "normal", output, run_id="directory-boundary")
    assert calls == [True]
    failure = result["m2_study"]["runtime_failure"]
    assert failure == {"type":"RuntimeError", "message":"test_stop_before_first_physical_step", "retry_performed":False}
    assert result["m2_study"]["deployment_run"] is None
    assert result["m2_study"]["return_study"] is None
    assert result["same_state_continuation_checked"] is False
    directory = output / "mission/concurrent-prefix"
    snapshot = {p.name:p.read_bytes() for p in directory.iterdir() if p.is_file()}
    with pytest.raises(ValueError, match="m3_prefix_fresh_output_required"):
        prefix.run_fixture(profile, "normal", directory)
    assert snapshot == {p.name:p.read_bytes() for p in directory.iterdir() if p.is_file()}
