"""Composition ordering only; scripted plant/coordinator, zero physics/models."""
from contextlib import contextmanager
import gzip
import json
from types import SimpleNamespace

import pytest

from src.runtime import starship_restored_native as native
from src.runtime.starship_inflight_runtime import approval_pipe
from src.runtime.starship_restored_contract import CRITICAL_SOURCES, PROFILE_SHA256
from src.runtime.starship_restored_integration import RestoredIntegrationPolicy


@pytest.fixture
def run_case(tmp_path, monkeypatch):
    calls, pairs = [], [approval_pipe() for _ in range(7)]
    source = {name: "a"*64 for name in CRITICAL_SOURCES | {"src/runtime/starship_restored_native.py"}}
    source["examples/spaceflight/starship-sixdof-profile.json"] = PROFILE_SHA256
    profile = {"integration": {"powered_dt_s": .25, "coast_dt_s": 2.}}
    settings = SimpleNamespace(record=lambda: {"synthetic": "ascent"})
    controls = SimpleNamespace(record=lambda: {"synthetic": "booster"})
    prefix = {"restored_prefix": {"closed": True}, "synthetic_scripted_record_only": True}
    actual_origin, coordinator_inputs = ["scripted_actual_origin"], []
    class Scope:
        completed = False
        def record(self):
            assert self.completed
            calls.append("completed_scope_receipt")
            return {"completed": True, "synthetic_only": True}
    scope = Scope()
    @contextmanager
    def integration(policy):
        assert policy.subdivisions == 2
        calls.append("scope_enter")
        yield scope
        scope.completed = True
        calls.append("scope_exit")
    class Prefix:
        def __init__(self, *args):
            assert args[-2] == 7500.
            calls.append("prefix_runtime")
        def origin(self):
            calls.append("actual_prefix_origin")
            return actual_origin[0]
    def produce(*args, **kwargs):
        calls.append("ground_prefix")
        assert not scope.completed
        return prefix
    class Deployment:
        def __init__(self, *args, **kwargs):
            calls.append("deployment_pipe_adapter")
        def finalize_prefix(self, saved):
            assert saved == prefix and (tmp_path/"prefix.json.gz").exists()
            calls.append("finalize_existing_deployment")
            return {"synthetic_deployment": True}
    origin_values = actual_origin
    class Coordinator:
        def __init__(self, actual_origin, **kwargs):
            assert actual_origin == origin_values[0]
            assert kwargs["deployment_record"] == {"synthetic_deployment": True}
            assert kwargs["output_dir"] == tmp_path/"governance"
            assert "signing_key" not in kwargs and "operator_token" not in kwargs
            coordinator_inputs.append(kwargs)
            self.result = None
            calls.append("coordinator_from_actual_origin")
        def start(self):
            assert not scope.completed
            calls.append("coordinator_start")
        def tick(self):
            assert not scope.completed
            calls.append("actual_continuation_tick")
            self.result = SimpleNamespace(record=lambda: {"actual_scripted_continuation": True})
            return {"detached_snapshot": []}
        def record(self):
            assert scope.completed
            calls.append("governance_record")
            return {"synthetic_governance": True}
        def close(self):
            calls.append("forecast_children_reaped")
    monkeypatch.setattr(native, "restored_integration", integration)
    monkeypatch.setattr(native, "RestoredPrefixRuntime", Prefix)
    monkeypatch.setattr(native, "restored_prefix_mission", produce)
    monkeypatch.setattr(native, "RestoredDeploymentJudgments", Deployment)
    monkeypatch.setattr(native, "RestoredReturnCoordinator", Coordinator)
    kwargs = dict(profile_raw_bytes=native._wire(profile), expected_source_sha256=source,
        ascent_settings=settings, control_settings=controls, return_sites=object(), splashdown_goal=object(),
        context={"run_id": "composition", "mission_id": "scripted", "vehicle_id": "ship"},
        plan_sha256="b"*64, case_id="O0_scope", mode="fixture", output_dir=tmp_path,
        integration_policy=RestoredIntegrationPolicy(2), decision_request_writer=pairs[0][1],
        decision_response_reader=pairs[1][0], ready_writer=pairs[2][1], ready_reader=pairs[3][0],
        approval_request_writer=pairs[4][1], approval_grant_reader=pairs[5][0], telemetry_writer=pairs[6][1],
        sites=(), notice_resources=lambda _snapshot: None, notice_generations=(0,))
    yield SimpleNamespace(profile=profile, kwargs=kwargs, calls=calls, prefix=prefix, folder=tmp_path,
        actual_origin=actual_origin, coordinator_inputs=coordinator_inputs)
    for pair in pairs:
        for endpoint in pair:
            endpoint.close()


def test_registered_return_review_binds_worker_client_and_execution_without_stepping_physics(run_case):
    from copy import deepcopy
    from hashlib import sha256
    from pathlib import Path
    from src.runtime import starship_restored_return_review as review
    case = run_case
    policy = deepcopy(review.DEFAULT_POLICY)
    case.kwargs['expected_source_sha256']['src/runtime/starship_restored_return_review.py'] = sha256(
        Path(review.__file__).read_bytes()).hexdigest()
    record = native.run_restored_native_worker(case.profile, **case.kwargs, return_review_policy=policy)
    client = case.coordinator_inputs[0]['decision_client'].record()
    assert client['return_review_policy'] == record['execution']['return_review_policy'] == policy
    assert client['return_review_policy_sha256'] == record['execution']['return_review_policy_sha256'] == review.policy_digest(policy)
    assert client['provider_calls_by_worker'] == 0
    assert case.coordinator_inputs[0]['return_review_policy'] == policy


def test_return_review_without_source_pin_stops_before_any_scripted_plant_work(run_case):
    from src.runtime.starship_restored_return_review import DEFAULT_POLICY
    with pytest.raises(ValueError, match='return_review_source_opt_in'):
        native.run_restored_native_worker(run_case.profile, **run_case.kwargs, return_review_policy=DEFAULT_POLICY)
    assert run_case.calls == []


def test_one_scope_covers_ground_and_actual_continuation_with_post_exit_receipt(run_case):
    case = run_case
    record = native.run_restored_native_worker(case.profile, **case.kwargs)
    assert case.calls == ["deployment_pipe_adapter", "scope_enter", "prefix_runtime", "ground_prefix",
        "finalize_existing_deployment", "actual_prefix_origin", "coordinator_from_actual_origin", "coordinator_start",
        "actual_continuation_tick", "forecast_children_reaped", "scope_exit", "completed_scope_receipt", "governance_record"]
    assert record["schema"] == "missionos.starship_restored_native_mission.v1"
    assert record["execution"]["mode"] == "fixture"
    assert record["execution"]["initial_return_candidate_id"] == "nominal"
    assert json.loads(gzip.decompress((case.folder/"prefix.json.gz").read_bytes())) == record["prefix"]


def test_unclosed_physical_negative_is_retained_without_an_origin_or_coordinator(run_case):
    case = run_case
    case.prefix["restored_prefix"]["closed"] = False
    record = native.run_restored_native_worker(case.profile, **case.kwargs)
    assert record["prefix"] == case.prefix and record["return"] is record["governance"] is None
    assert (case.folder/"prefix.json.gz").exists()
    assert "actual_prefix_origin" not in case.calls and "coordinator_start" not in case.calls
    assert "completed_scope_receipt" in case.calls


@pytest.mark.parametrize("update", [
    {"mode": "auto"}, {"case_id": "../escape"}, {"plan_sha256": "bad"},
    {"integration_policy": RestoredIntegrationPolicy(4)}, {"decision_request_writer": 123},
    {"return_context_projection": 1}, {"return_context_projection": True},
])
def test_invalid_scope_or_boundary_cannot_start_a_plant_or_create_a_prefix(run_case, update):
    case = run_case
    with pytest.raises(ValueError):
        native.run_restored_native_worker(case.profile, **{**case.kwargs, **update})
    assert case.calls == [] and not (case.folder/"prefix.json.gz").exists()


def test_no_reuse_of_previous_actual_prefix(run_case):
    case = run_case
    native.run_restored_native_worker(case.profile, **case.kwargs)
    before = list(case.calls)
    with pytest.raises(ValueError, match="exclusive_worker_output"):
        native.run_restored_native_worker(case.profile, **case.kwargs)
    assert case.calls == before


def test_explicit_return_context_is_source_bound_and_forwarded_only_after_actual_origin(run_case):
    case = run_case
    case.kwargs["expected_source_sha256"]["src/runtime/starship_restored_return_context.py"] = "d"*64
    native.run_restored_native_worker(case.profile, **case.kwargs, return_context_projection=True)
    assert case.coordinator_inputs[0]["return_context_projection"] is True
    assert case.calls.index("actual_prefix_origin") < case.calls.index("coordinator_from_actual_origin")


def test_observer_error_reaps_forecast_children_without_a_completed_receipt(run_case):
    case = run_case
    def bad_observer(_snapshot):
        raise ValueError("synthetic_observer_rejection")
    with pytest.raises(ValueError, match="observer_rejection"):
        native.run_restored_native_worker(case.profile, **case.kwargs, on_return_snapshot=bad_observer)
    assert case.calls[-1] == "forecast_children_reaped"
    assert "completed_scope_receipt" not in case.calls
    assert (case.folder/"prefix.json.gz").exists()


def test_registered_tool_binds_only_after_actual_prefix_with_fixed_deployment_definition(run_case):
    from src.runtime.starship_restored_operations import RegisteredRestoredOperations, RestoredOperations
    from tests.contract.test_starship_restored_admission import case as actual_case
    from tests.contract.test_starship_restored_operations import definition
    values = actual_case()
    case = run_case
    case.actual_origin[0] = values[1]
    case.kwargs["expected_source_sha256"]["src/runtime/starship_restored_operations.py"] = "d"*64
    registered = RegisteredRestoredOperations(definition(values))
    kwargs = {key: value for key, value in case.kwargs.items() if key not in ("sites", "notice_resources", "notice_generations")}
    native.run_restored_native_worker(case.profile, **kwargs, registered_operations=registered)
    route = case.coordinator_inputs[0]
    assert type(route["notice_resources"]) is RestoredOperations
    assert route["notice_resources"].record()["resource_epoch_s"] == values[1].time_s
    assert route["notice_generations"] == registered.notice_generations and route["sites"] == registered.sites
    assert case.calls.index("actual_prefix_origin") < case.calls.index("coordinator_from_actual_origin")


@pytest.mark.parametrize("mixed", ["sites", "notice_resources", "notice_generations", "deployment_notice", "notice_summary", "source"])
def test_registered_operations_cannot_mix_unregistered_callbacks_or_sites_before_plant(run_case, mixed):
    from src.runtime.starship_restored_operations import RegisteredRestoredOperations
    from tests.contract.test_starship_restored_admission import case as actual_case
    from tests.contract.test_starship_restored_operations import definition
    case = run_case
    kwargs = {key: value for key, value in case.kwargs.items() if key not in ("sites", "notice_resources", "notice_generations")}
    kwargs["expected_source_sha256"]["src/runtime/starship_restored_operations.py"] = "d"*64
    registered = RegisteredRestoredOperations(definition(actual_case()))
    if mixed in ("sites", "notice_resources", "notice_generations"):
        kwargs[mixed] = case.kwargs[mixed]
    elif mixed in ("deployment_notice", "notice_summary"):
        kwargs[mixed] = lambda _value: "unregistered callback"
    else:
        del kwargs["expected_source_sha256"]["src/runtime/starship_restored_operations.py"]
    with pytest.raises(ValueError, match="registered_operations"):
        native.run_restored_native_worker(case.profile, **kwargs, registered_operations=registered)
    assert not case.calls


def test_unclosed_prefix_after_partial_judgment_work_cannot_discard_attempts_as_model_free_negative(run_case, monkeypatch):
    case = run_case
    case.prefix["restored_prefix"]["closed"] = False
    original = native.RestoredDecisionClient.record
    def partial(client):
        row = original(client)
        row["events"] = [{"event": "request_sent"}]
        return row
    monkeypatch.setattr(native.RestoredDecisionClient, "record", partial)
    with pytest.raises(ValueError, match="unclosed_prefix_has_judgment_work"):
        native.run_restored_native_worker(case.profile, **case.kwargs)
    assert (case.folder/"prefix.json.gz").exists()
    assert "coordinator_start" not in case.calls and "completed_scope_receipt" not in case.calls


@pytest.mark.parametrize("closed", [True, False])
def test_complete_numpy_prefix_is_finite_json_canonical_after_saved_gzip_and_parent_wrapper_roundtrip(run_case, closed):
    import numpy as np
    case = run_case
    case.prefix["restored_prefix"]["closed"] = np.bool_(closed)
    case.prefix["complete_scripted_numpy_diagnostics"] = {"gate": np.bool_(False), "mass": np.float64(123.25),
        "step_count": np.int64(3), "nested": [{"state_finite": np.bool_(True), "time_s": np.float32(1.5)}]}
    record = native.run_restored_native_worker(case.profile, **case.kwargs)
    restored = json.loads(gzip.decompress((case.folder/"prefix.json.gz").read_bytes()))
    assert record["prefix"] == restored
    assert type(restored["restored_prefix"]["closed"]) is bool
    diagnostic = restored["complete_scripted_numpy_diagnostics"]
    assert type(diagnostic["gate"]) is bool and type(diagnostic["step_count"]) is int
    assert type(diagnostic["mass"]) is float and type(diagnostic["nested"][0]["state_finite"]) is bool
    assert type(case.prefix["restored_prefix"]["closed"]) is np.bool_  # Caller state untouched.
    assert json.loads(json.dumps(record, allow_nan=False)) == record
    if not closed:
        assert record["return"] is record["governance"] is None
        assert "actual_prefix_origin" not in case.calls and "completed_scope_receipt" in case.calls


def test_numpy_nonfinite_or_unsupported_serialization_stops_before_a_record_can_be_claimed(run_case):
    import numpy as np
    case = run_case
    case.prefix["complete_scripted_numpy_diagnostics"] = {"invalid": np.float64(float("nan"))}
    with pytest.raises(ValueError):
        native.run_restored_native_worker(case.profile, **case.kwargs)
    assert not (case.folder/"prefix.json.gz").exists()
    assert "coordinator_start" not in case.calls and "completed_scope_receipt" not in case.calls
