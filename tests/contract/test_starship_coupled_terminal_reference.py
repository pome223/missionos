"""Static bookkeeping/governor tests, no physical or native model execution."""
from copy import deepcopy
import json
import time

import pytest

from test_starship_fixed_terminal_reference import fixture
from src.runtime import starship_actual_recovery_shooting as shooting
from src.runtime import starship_fixed_terminal_reference as reference
from src.runtime import starship_coupled_terminal_reference as coupled
from src.runtime import starship_sixdof as dyn


def sink(corpus):
    ledger = {"bytes": 0}
    def persist(identifier, payload):
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        corpus[identifier] = deepcopy(payload)
        ledger["bytes"] += len(raw)
        return {"artifact_id": identifier, "format": "json", "relative_path": identifier+".json",
            "raw_json_sha256": shooting.digest(payload), "sha256": shooting.digest(payload),
            "bytes": len(raw), "persisted_before_analysis": True}
    persist.check_before_query = lambda: None
    persist.resource_status = lambda: {"free_bytes": 500*1024*1024, "maximum_stored_bytes": 100*1024*1024,
        "minimum_free_bytes": 400*1024*1024, "persisted_bytes": ledger["bytes"]}
    return persist


def prepared():
    profile, catch, _, _, snapshot, prior, command = fixture()
    from src.runtime.starship_reference_tracking import ReferenceTracker
    tracker = ReferenceTracker(profile, snapshot["context"]["prior_command_reference"]["quaternion"], 99.9)
    snapshot["context"]["reference_tracker"] = shooting.saved(vars(tracker))
    plan = reference.build_terminal_reference(snapshot, profile, catch, prior_state=prior, previous_command=command)
    return plan, snapshot, profile, catch


def test_static_protocol_and_each_query_persist_before_analysis_with_carried_history(monkeypatch):
    plan, snapshot, profile, catch = prepared()
    before = deepcopy((plan, snapshot, profile, catch))
    corpus, calls = {}, []
    def model(sample, q, rate, acceleration, fuel, initial, booster, prof, config):
        assert any(value["kind"] == "coupled_static_protocol" for value in corpus.values())
        assert list(corpus.values())[-1]["kind"] == "static_model_attempted"
        calls.append(sample["time_s"])
        return {"mass_kg": booster.dry_mass_kg+fuel, "propellant_kg": fuel, "required_main_force_body_n": [0., 0., 1e6],
            "hypothetical_reference_state": True, "integrated_observation": False}
    monkeypatch.setattr(coupled, "_static_query", model)
    monkeypatch.setattr(dyn, "step", lambda *a, **k: pytest.fail("no physical integration"))
    result = coupled.generate_coupled_terminal_reference(plan, snapshot, profile, catch,
        artifact_sink=sink(corpus), wall_deadline_monotonic_s=time.monotonic()+120., source_map={"fixture.py": "a"*64})
    assert result["failure"] is None
    assert "computation_completed" not in result
    assert "failure_category" not in result
    assert result["counters"]["sweeps_completed"] == 3
    assert len(calls) == result["counters"]["model_queries_completed"] == 606
    assert (plan, snapshot, profile, catch) == before
    assert result["candidate_plant_admitted"] is False
    assert result["physical_plant_calls"] == 0
    first = result["sweeps"][0]["nodes"][0]
    assert first["tracking_receipt"]["previous_requested_q_body_to_eci"] == snapshot["context"]["reference_tracker"]["quaternion"]
    assert first["tracking_receipt"]["previous_time_s"] == 99.9
    assert first["tracking_actual_inputs_are_hypothetical"] is True
    assert first["governed_load_query"]["raw_artifact"]["persisted_before_analysis"] is True
    assert "conditioned_frame_diagnostics" in first


def test_failed_query_retains_attempt_raw_failure_and_consumes_budget_without_retry(monkeypatch):
    plan, snapshot, profile, catch = prepared()
    corpus = {}
    def failure(*args):
        raise ArithmeticError("synthetic fixed fixture")
    monkeypatch.setattr(coupled, "_static_query", failure)
    result = coupled.generate_coupled_terminal_reference(plan, snapshot, profile, catch,
        artifact_sink=sink(corpus), wall_deadline_monotonic_s=time.monotonic()+120., source_map={"fixture.py": "a"*64})
    assert result["failure"] == "ArithmeticError"
    assert result["counters"]["model_queries_attempted"] == 1
    assert result["counters"]["model_queries_completed"] == 0
    assert result["counters"]["model_queries_failed"] == 1
    assert result["incomplete_io"] is None
    assert any(p["kind"] == "static_model_failed" for p in corpus.values())
    assert list(corpus.values())[-1]["kind"] == "coupled_static_complete_or_partial"


def test_wall_expiration_after_durable_attempt_does_not_call_model(monkeypatch):
    plan, snapshot, profile, catch = prepared()
    corpus = {}
    moments = iter([10., 10.1, 10.2, 200., 201.])
    monkeypatch.setattr(coupled.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(coupled, "_static_query", lambda *a: pytest.fail("deadline exceeded before model"))
    result = coupled.generate_coupled_terminal_reference(plan, snapshot, profile, catch,
        artifact_sink=sink(corpus), wall_deadline_monotonic_s=120., source_map={"fixture.py": "a"*64})
    assert result["failure"] == "static_wall_or_query_budget_exhausted"
    assert result["counters"]["model_queries_attempted"] == 1
    assert result["counters"]["model_queries_skipped_after_durable_attempt"] == 1
    assert result["counters"]["model_queries_completed"] == 0
    assert result["incomplete_io"]["stage"] == "after_attempt_before_model"


@pytest.mark.parametrize("deadline", [True, float("nan"), float("inf"), -1., 1e50])
def test_invalid_or_unbounded_wall_budget_before_any_query(deadline, monkeypatch):
    plan, snapshot, profile, catch = prepared()
    monkeypatch.setattr(coupled, "_static_query", lambda *a: pytest.fail("preflight must fail"))
    with pytest.raises(ValueError, match="predeclared_sink_and_wall_deadline"):
        coupled.generate_coupled_terminal_reference(plan, snapshot, profile, catch,
            artifact_sink=lambda *a: None, wall_deadline_monotonic_s=deadline, source_map={"fixture.py": "a"*64})


def test_unpersisted_protocol_cannot_start_model_query(monkeypatch):
    plan, snapshot, profile, catch = prepared()
    monkeypatch.setattr(coupled, "_static_query", lambda *a: pytest.fail("missing persistence"))
    bad = sink({})
    def bad_manifest(*args):
        return {"persisted_before_analysis": False}
    bad_manifest.check_before_query, bad_manifest.resource_status = bad.check_before_query, bad.resource_status
    with pytest.raises(ValueError, match="persisted_forecast_manifest"):
        coupled.generate_coupled_terminal_reference(plan, snapshot, profile, catch,
            artifact_sink=bad_manifest,
            wall_deadline_monotonic_s=time.monotonic()+120., source_map={"fixture.py": "a"*64})


def test_wall_expiration_before_next_sweep_does_not_repeat_completed_nodes(monkeypatch):
    plan, snapshot, profile, catch = prepared()
    corpus, clock = {}, {"remaining": None}
    original_sink = sink(corpus)
    def persist(identifier, payload):
        manifest = original_sink(identifier, payload)
        if payload["kind"] == "static_model_raw":
            request = corpus[payload["attempted"]["artifact_id"]]
            if request["node"] == 100 and request["stage"] == "governed_recheck":
                clock["remaining"] = 1
        return manifest
    persist.check_before_query = original_sink.check_before_query
    persist.resource_status = original_sink.resource_status
    def now():
        if clock["remaining"] is None:
            return 10.
        if clock["remaining"]:
            clock["remaining"] -= 1
            return 10.
        return 200.
    monkeypatch.setattr(coupled.time, "monotonic", now)
    monkeypatch.setattr(coupled, "_static_query", lambda sample,q,rate,acceleration,fuel,initial,booster,*args:
        {"mass_kg":booster.dry_mass_kg+fuel, "propellant_kg":fuel, "required_main_force_body_n":[0.,0.,1e6]})
    result = coupled.generate_coupled_terminal_reference(plan, snapshot, profile, catch,
        artifact_sink=persist, wall_deadline_monotonic_s=120., source_map={"fixture.py":"a"*64})
    assert result["failure"] == "static_wall_or_query_budget_exhausted"
    assert result["counters"]["sweeps_attempted"] == result["counters"]["sweeps_completed"] == 1
    assert len(result["sweeps"][0]["nodes"]) == 101
    assert result["current_partial_nodes"] == []
    assert result["partial_sweep_index"] is None


@pytest.mark.parametrize("angle", [0., 1e-18, 1e-16, 1e-8, .5, 3.141592653589793/4])
def test_continuous_increment_has_identity_limit_and_no_small_axis_refusal(angle):
    import math
    q = (1., 0., 0., 0.)
    result = coupled._increment_continuous(q, angle, 0.)
    assert result == pytest.approx((math.cos(angle/2), math.sin(angle/2), 0., 0.), abs=4*math.ulp(1.))
    assert math.hypot(*result) == pytest.approx(1.)
    if 0 < angle < 1e-14:
        with pytest.raises(ValueError, match="zero vector"):
            coupled._increment(q, angle, 0.)


def test_opt_in_negative_axial_stationary_branch_is_unresolved_without_force_clamp(monkeypatch):
    plan, snapshot, profile, catch = prepared()
    corpus = {}
    monkeypatch.setattr(coupled, "_static_query", lambda sample,q,rate,acceleration,fuel,initial,booster,*args:
        {"mass_kg":booster.dry_mass_kg+fuel, "propellant_kg":fuel, "required_main_force_body_n":[0.,0.,-1.]})
    monkeypatch.setattr(dyn, "step", lambda *a, **k: pytest.fail("no physical integration"))
    result = coupled.generate_coupled_terminal_reference(plan, snapshot, profile, catch,
        artifact_sink=sink(corpus), wall_deadline_monotonic_s=time.monotonic()+120.,
        source_map={"fixture.py":"a"*64}, development_continuous_increment=True)
    assert result["schema"] == coupled.SCHEMA_V2
    assert corpus[result["origin_artifact"]["artifact_id"]]["configuration"] == coupled.CONFIG_V2
    assert result["failure"] is None
    assert result["computation_completed"] is True
    assert result["failure_category"] is result["failure_stage"] is None
    assert result["counters"]["model_queries_completed"] == 606
    assert result["counters"]["updates_attempted"] == 303
    assert result["counters"]["line_search_queries"] == 0
    for sweep in result["sweeps"]:
        for node in sweep["nodes"]:
            assert node["inverse_converged"] is False
            assert node["nominal_force_cone_passed"] is False
            assert node["governed_load_query"]["required_main_force_body_n"][2] == -1.
            history = node["updates"][0]
            assert history["status"] == "transverse_stationary_negative_axial"
            assert history["positive_axial_passed"] is False
            assert history["inverse_convergence_established"] is False
    assert result["candidate_plant_admitted"] is False


@pytest.mark.parametrize("flag", [1, 0, None, "true"])
def test_increment_policy_is_strict_bool_before_persistence_or_query(flag, monkeypatch):
    plan, snapshot, profile, catch = prepared()
    monkeypatch.setattr(coupled, "_static_query", lambda *a: pytest.fail("invalid flag before query"))
    corpus = {}
    with pytest.raises(ValueError, match="must_be_bool"):
        coupled.generate_coupled_terminal_reference(plan, snapshot, profile, catch,
            artifact_sink=sink(corpus), wall_deadline_monotonic_s=time.monotonic()+120.,
            source_map={"fixture.py":"a"*64}, development_continuous_increment=flag)
    assert corpus == {}


def test_model_failure_and_failed_log_io_both_remain_accounted_in_v2(monkeypatch):
    plan, snapshot, profile, catch = prepared()
    corpus = {}
    persisted = sink(corpus)
    def fail_model(*args):
        raise ArithmeticError("public fixture model refusal")
    def fail_log(identifier, payload):
        if payload["kind"] == "static_model_failed":
            raise OSError("public fixture failed-record I/O refusal")
        return persisted(identifier, payload)
    fail_log.check_before_query, fail_log.resource_status = persisted.check_before_query, persisted.resource_status
    monkeypatch.setattr(coupled, "_static_query", fail_model)
    result = coupled.generate_coupled_terminal_reference(plan, snapshot, profile, catch,
        artifact_sink=fail_log, wall_deadline_monotonic_s=time.monotonic()+120.,
        source_map={"fixture.py":"a"*64}, development_continuous_increment=True)
    assert result["counters"]["model_queries_attempted"] == 1
    assert result["counters"]["model_queries_failed"] == 1
    assert result["counters"]["model_queries_completed"] == 0
    assert result["failure"] == "OSError"
    assert result["failure_category"] == "resource_or_io"
    assert result["failure_stage"] == "failed_model_record_persistence"
    assert result["computation_completed"] is False
    incomplete = result["incomplete_io"]
    assert incomplete["stage"] == "model_failed_before_failure_persist"
    assert incomplete["error_class"] == "ArithmeticError"
    assert corpus[incomplete["attempted_artifact"]["artifact_id"]] == incomplete["request"]
    assert result["raw_artifact"]["artifact_id"] in corpus


def test_implementation_failure_is_retained_but_not_completed_computation(monkeypatch):
    plan, snapshot, profile, catch = prepared()
    corpus = {}
    monkeypatch.setattr(coupled, "_static_query", lambda sample,q,rate,acceleration,fuel,initial,booster,*args:
        {"mass_kg":booster.dry_mass_kg+fuel, "propellant_kg":fuel, "required_main_force_body_n":[1.,1.,1e6]})
    def bug(*args):
        raise ValueError("public fixture request-geometry bug")
    monkeypatch.setattr(coupled, "_increment_continuous", bug)
    result = coupled.generate_coupled_terminal_reference(plan, snapshot, profile, catch,
        artifact_sink=sink(corpus), wall_deadline_monotonic_s=time.monotonic()+120.,
        source_map={"fixture.py":"a"*64}, development_continuous_increment=True)
    assert result["failure"] == "ValueError"
    assert result["failure_category"] == "implementation"
    assert result["failure_stage"] == "reference_geometry_or_iteration"
    assert result["computation_completed"] is False
    assert result["counters"]["model_queries_completed"] == 1
    assert result["counters"]["model_queries_failed"] == 0
    assert result["candidate_plant_admitted"] is False
    assert result["raw_artifact"]["artifact_id"] in corpus


def test_saved_summary_uses_json_domain_for_native_inertia_and_panel_vectors():
    native = {"sweeps":[{"nodes":[{"governed_load_query":{
        "inertia_kg_m2":((1.,0.,0.),(0.,2.,0.),(0.,0.,3.)),
        "panel_loads":[{"force_body_n":(4.,5.,6.)}]}}]}], "physical_plant_calls":0}
    saved = json.loads(json.dumps(native,allow_nan=False))
    returned = {**native,"raw_artifact":{"public_fixture":True},"raw_persisted_before_admission_analysis":True}
    assert native != saved
    assert coupled.saved_summary_matches(saved,returned)
    changed = deepcopy(saved)
    changed["sweeps"][0]["nodes"][0]["governed_load_query"]["panel_loads"][0]["force_body_n"][0] += 1e-6
    assert not coupled.saved_summary_matches(changed,returned)


def test_v3_probes_nonstationary_negative_branch_and_keeps_positive_root_constraint(monkeypatch):
    plan, snapshot, profile, catch = prepared()
    corpus = {}
    anchor = snapshot["context"]["reference_tracker"]["quaternion"]
    fixed_force = dyn.rotate(anchor,(1.,1.,-1.))
    def model(sample,q,rate,acceleration,fuel,initial,booster,*args):
        return {"mass_kg":booster.dry_mass_kg+fuel,"propellant_kg":fuel,
            "required_main_force_body_n":list(dyn.inverse_rotate(q,fixed_force))}
    monkeypatch.setattr(coupled,"_static_query",model)
    monkeypatch.setattr(dyn,"step",lambda *a,**k:pytest.fail("no plant integration"))
    result = coupled.generate_coupled_terminal_reference(plan,snapshot,profile,catch,
        artifact_sink=sink(corpus),wall_deadline_monotonic_s=time.monotonic()+120.,
        source_map={"fixture.py":"a"*64},development_continuous_increment=True,
        development_positive_axial_branch=True)
    assert result["schema"] == coupled.SCHEMA_V3
    assert result["failure"] is None
    assert result["counters"]["positive_axial_branch_probes"] == 3
    first = result["sweeps"][0]["nodes"][0]
    branch = first["positive_axial_branch"]
    assert branch["attempted"] and branch["applied"] and branch["positive_axial_passed"]
    source = corpus[branch["from_query"]["artifact_id"]]["result"]["required_main_force_body_n"]
    probe = corpus[branch["probe_query"]["artifact_id"]]["result"]["required_main_force_body_n"]
    assert abs(source[0])+abs(source[1]) > 1.
    assert source[2] < 0 < probe[2]
    assert source != probe
    for history in first["updates"]:
        if history["status"] == "accepted":
            chosen = corpus[history["line_search_queries"][-1]["artifact_id"]]["result"]["required_main_force_body_n"]
            assert chosen[2] >= 0
    assert result["candidate_plant_admitted"] is False
    assert result["joint_reference_feasibility_established"] is False


@pytest.mark.parametrize("force",[(0.,0.,-1.),(0.,0.,-1e-8)])
def test_v3_failed_or_low_force_probe_remains_unresolved_without_force_change(force,monkeypatch):
    plan,snapshot,profile,catch = prepared()
    corpus = {}
    monkeypatch.setattr(coupled,"_static_query",lambda sample,q,rate,acceleration,fuel,initial,booster,*args:
        {"mass_kg":booster.dry_mass_kg+fuel,"propellant_kg":fuel,"required_main_force_body_n":list(force)})
    result = coupled.generate_coupled_terminal_reference(plan,snapshot,profile,catch,
        artifact_sink=sink(corpus),wall_deadline_monotonic_s=time.monotonic()+120.,
        source_map={"fixture.py":"a"*64},development_continuous_increment=True,
        development_positive_axial_branch=True)
    assert result["failure"] is None
    expected_probes = 303 if force[2] == -1. else 0
    assert result["counters"]["positive_axial_branch_probes"] == expected_probes
    for sweep in result["sweeps"]:
        for node in sweep["nodes"]:
            assert node["inverse_converged"] is False
            assert node["positive_axial_branch"]["applied"] is False
            assert node["governed_load_query"]["required_main_force_body_n"] == list(force)
    assert result["candidate_plant_admitted"] is False


@pytest.mark.parametrize("continuous,positive",[(False,True),(True,1),(True,None)])
def test_v3_requires_bool_opt_in_and_continuous_kernel_before_any_query(continuous,positive,monkeypatch):
    plan,snapshot,profile,catch = prepared()
    corpus = {}
    monkeypatch.setattr(coupled,"_static_query",lambda *a:pytest.fail("invalid branch policy before query"))
    with pytest.raises(ValueError,match="requires_continuous_policy"):
        coupled.generate_coupled_terminal_reference(plan,snapshot,profile,catch,
            artifact_sink=sink(corpus),wall_deadline_monotonic_s=time.monotonic()+120.,
            source_map={"fixture.py":"a"*64},development_continuous_increment=continuous,
            development_positive_axial_branch=positive)
    assert corpus == {}
