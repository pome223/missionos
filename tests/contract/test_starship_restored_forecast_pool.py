"""Fake processes/forecasts only; never integrate dynamics or send a model call."""
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.smoke_starship_restored_approval import synthetic_origin
from src.runtime import starship_restored_contract as contract
from src.runtime import starship_restored_forecast_pool as pool
from src.runtime import starship_restored_return as returns

REPO = Path(__file__).resolve().parents[2]
REAL_POPEN = subprocess.Popen
FORECAST_ENTRYPOINT = returns.forecast_restored_return


@pytest.fixture(autouse=True)
def no_actual_forecast_provider_or_plant(monkeypatch):
    from src.runtime import starship_physics as env, starship_sixdof as dyn
    from src.runtime import starship_sixdof_mission as mission
    from src.intelligence import starship_m2_broker
    def forbidden(*_a, **_kw):
        raise AssertionError("Forecast boundary tests forbid actual physics/predictors/providers/processes")
    for owner, name in ((dyn, "step"), (dyn, "_rhs"), (dyn, "_rk4"), (env, "step"),
            (mission, "advance_plant"), (mission, "control"), (returns, "advance_plant"),
            (returns, "forecast_restored_return"), (starship_m2_broker, "_send_once"),
            (socket, "create_connection"), (socket, "getaddrinfo"), (subprocess, "Popen")):
        monkeypatch.setattr(owner, name, forbidden)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    original, _ = synthetic_origin()
    raw = json.loads(original.source_record_bytes())
    profile = (REPO/"examples/spaceflight/starship-sixdof-profile.json").read_bytes()
    root, source = tmp_path/"source", {}
    for name in contract.CRITICAL_SOURCES | pool.REQUIRED_SOURCES:
        path = root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(profile if name == "examples/spaceflight/starship-sixdof-profile.json" else b"# synthetic source pin\n")
        source[name] = sha256(path.read_bytes()).hexdigest()
    raw["source_sha256"] = source
    encoded = pool._wire(raw)
    base = contract.ActualRestoredOrigin(sha256(encoded).hexdigest(), encoded, profile, source)
    base_schedule = contract.RestoredCandidateSchedule(base)
    current = deepcopy(raw)
    current["final_state"]["time_s"] += 1.
    current["inventory"]["observed_time_s"] += 1.
    current["state_history"].append({"state": deepcopy(current["final_state"]), "retained_count": 26})
    encoded = pool._wire(current)
    current_origin = contract.ActualRestoredOrigin(sha256(encoded).hexdigest(), encoded, profile, source)
    schedule = contract.RestoredCandidateSchedule(current_origin, anchor=base_schedule)
    requests = tuple(returns.RestoredReturnRequest(current_origin, schedule, candidate, profile, source)
        for candidate in ("nominal", "next_orbit"))
    monkeypatch.setattr(pool, "ROOT", root)
    monkeypatch.setattr(pool, "_environment", lambda: {"fixture_environment": True})
    clock = SimpleNamespace(now=100.)
    monkeypatch.setattr(pool.time, "monotonic", lambda: clock.now)
    children, killed = [], []
    class Process:
        def __init__(self, args, **kwargs):
            self.args, self.options, self.returncode, self.pid = args, kwargs, None, 100000000+len(children)
            children.append(self)
        def poll(self):
            return self.returncode
        def wait(self, timeout=None):
            self.wait_thread = threading.get_ident()
            if self.returncode is None:
                self.returncode = -15
            return self.returncode
    monkeypatch.setattr(pool.subprocess, "Popen", Process)
    monkeypatch.setattr(pool.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    batch = pool.RestoredForecastBatch(requests, output_dir=tmp_path/"forecast", wall_deadline_monotonic=1000.,
        expected_source_sha256=source, integration_policy=pool.RestoredIntegrationPolicy(2))
    yield batch, source, children, killed, clock
    batch.close()


def prediction(request):
    origin = request.origin.record()
    final = deepcopy(origin["state"])
    final["time_s"] = request.record()["maximum_simulation_time_s"]
    final["propellant_kg"] -= 1.
    return returns.RestoredReturnResult(pool._wire({
        "schema": "missionos.starship_restored_return_prediction.v1", "prediction_only": True,
        "binding": request.record(), "initial_binding": request.record(), "initial_state": origin["state"],
        "vehicle": origin["vehicle"], "final_vehicle": origin["vehicle"], "final_state": final,
        "retained_count": origin["retained_count"], "payload_releases_after_origin": 0, "reservation_changes": [],
        "actual_state_initialized_from_prediction": False, "model_calls": 0,
        "source_provenance_authenticated": False, "independent_physical_model": False,
        "outcome": {"termination": "time_limit"}, "samples": [], "events": [],
        "old_certificate_admitted": False, "qualification_refreshed": False,
        "mission_authority_granted": False, "dispatch_authorized": False, "physical_execution": False}))


def integration_receipt(policy):
    """Synthetic scope receipt for transport tests; no mathematical proof."""
    from src.runtime.starship_restored_integration import _Execution
    execution = _Execution(policy)
    execution._status = "completed"
    execution._trace.update(rk4_calls=1, rk4_substeps=policy.subdivisions)
    return execution.record()


def complete(batch, index, *, mutation=None, receipt_mutation=None, integration_mutation=None):
    request, process = batch.requests[index], batch.processes[index]
    raw = prediction(request)._record_json
    if mutation:
        value = json.loads(raw)
        mutation(value)
        raw = pool._wire(value)
    packed = gzip.compress(raw, mtime=0)
    folder = batch.output_dir/request.candidate_id
    token = sha256(batch._job).hexdigest()
    claim = {"job_sha256": token, "candidate_id": request.candidate_id,
        "request_sha256": pool._digest(request.record()), "attempt": 1}
    pool._save(folder/"worker.claim", claim)
    pool._save_bytes(folder/"prediction.json.gz", packed)
    integration = integration_receipt(batch.integration_policy)
    if integration_mutation:
        integration_mutation(integration)
    pool._save(folder/"integration.json", integration)
    result = {"schema": "missionos.starship_restored_forecast_worker_result.v1", **claim,
        "raw_sha256": sha256(raw).hexdigest(), "gzip_sha256": sha256(packed).hexdigest(),
        "integration_execution": integration,
        "prediction_only": True, "model_calls": 0, "execution_initialized": False}
    if receipt_mutation:
        receipt_mutation(result)
    pool._save(folder/"result.json", result)
    process.returncode = 0


def test_constructor_is_no_exec_and_raw_origin_accessor_is_immutable(setup):
    batch, _, children, _, _ = setup
    assert batch.state == "created" and children == [] and not batch.output_dir.exists()
    origin = batch.requests[0].origin
    assert sha256(origin.source_record_bytes()).hexdigest() == origin.source_record_sha256
    assert type(origin.source_record_bytes()) is bytes
    with pytest.raises(FrozenInstanceError):
        origin._source_record_bytes = b"{}"


def test_start_sanitizes_env_pins_all_private_inputs_and_reconstructs_anchor_chain(setup, monkeypatch):
    batch, source, children, _, _ = setup
    monkeypatch.setenv("DEEPSEEK_API_KEY", "do-not-send")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/private.json")
    batch.start()
    assert len(children) == 2 and batch.poll() is None
    job = pool._read(batch._job)
    assert job["integration_policy"] == batch.integration_policy.record()
    assert job["origin_proves_numerical_policy"] is False
    assert job["matching_actual_policy_requires_outer_registered_execution_binding"] is True
    for name, expected in job["input_sha256"].items():
        assert sha256((batch.output_dir/name).read_bytes()).hexdigest() == expected
    for child, request in zip(children, batch.requests):
        env = child.options["env"]
        assert not {"DEEPSEEK_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"} & env.keys()
        assert env[pool.WORKER_ENV] == sha256(batch._job).hexdigest()
        assert child.options["start_new_session"] is True
        rebuilt = pool._reconstruct(job, batch.output_dir, request.candidate_id)
        assert rebuilt.record() == request.record() and rebuilt.origin.source_record_bytes() == request.origin.source_record_bytes()
        assert rebuilt.schedule.opportunity_anchor_record() == request.schedule.opportunity_anchor_record()
    source.clear()
    assert batch.poll() is None  # constructor copied the caller's mutable map


def test_pair_returns_only_when_both_complete_immutable_prediction_only(setup):
    batch, _, _, _, _ = setup
    batch.start()
    complete(batch, 1)
    assert batch.poll() is None
    complete(batch, 0)
    results = batch.poll()
    assert type(results) is tuple and all(type(x) is returns.RestoredReturnResult for x in results)
    assert [x.record()["binding"]["candidate_id"] for x in results] == ["nominal", "next_orbit"]
    row = results[0].record()
    row["initial_state"]["propellant_kg"] = -1.
    assert results[0].record()["initial_state"]["propellant_kg"] > 0.
    assert batch.poll() == results
    with pytest.raises(ValueError, match="single_use"):
        batch.start()


@pytest.mark.parametrize("change", ["single", "reversed", "source", "deadline", "deadline_bool"])
def test_constructor_rejects_wrong_pair_source_or_deadline_without_writes_or_processes(setup, tmp_path, change):
    batch, source, children, _, _ = setup
    requests, deadline, expected = batch.requests, 1000., source
    if change == "single":
        requests = requests[:1]
    elif change == "reversed":
        requests = tuple(reversed(requests))
    elif change == "source":
        expected = source | {pool.SCRIPT: "0"*64}
    else:
        deadline = float("nan") if change == "deadline" else True
    with pytest.raises(ValueError):
        pool.RestoredForecastBatch(requests, output_dir=tmp_path/"uncreated", wall_deadline_monotonic=deadline,
            expected_source_sha256=expected, integration_policy=batch.integration_policy)
    assert not (tmp_path/"uncreated").exists() and not children


@pytest.mark.parametrize("mutation", [
    lambda r:r["binding"].update(return_time_s=r["binding"]["return_time_s"]+1.),
    lambda r:r["initial_state"].update(propellant_kg=1.),
    lambda r:r["vehicle"].update(dry_mass_kg=1.),
    lambda r:r["final_state"]["engine_states"][0].update(available=False),
    lambda r:r.update(prediction_only=False), lambda r:r.update(model_calls=False),
    lambda r:r.update(payload_releases_after_origin=False),
    lambda r:r["outcome"].update(termination="numerical_failure"),
])
def test_invalid_candidate_fails_whole_batch_kills_peer_and_cannot_retry(setup, mutation):
    batch, _, _, killed, _ = setup
    batch.start()
    complete(batch, 0, mutation=mutation)
    with pytest.raises(ValueError):
        batch.poll()
    batch.close()
    assert batch.state == "failed" and killed and (batch.output_dir/"failure.json").exists()
    assert (batch.output_dir/"nominal"/"prediction.json.gz").exists()
    with pytest.raises(ValueError, match="single_use"):
        batch.start()
    with pytest.raises(ValueError):
        batch.poll()


def test_deadline_stops_peers_nonblocking_and_keeps_claims(setup):
    batch, _, children, killed, clock = setup
    batch.start()
    clock.now = batch.deadline
    with pytest.raises(ValueError, match="deadline_expired"):
        batch.poll()
    batch.close()
    assert len(killed) == 2 and (batch.output_dir/"batch.claim").exists()
    assert all(child.wait_thread != threading.get_ident() for child in children)


def test_source_drift_invalidates_even_before_any_result(setup):
    batch, source, _, killed, _ = setup
    batch.start()
    (pool.ROOT/next(iter(source))).write_bytes(b"changed after start")
    with pytest.raises(ValueError, match="source_changed"):
        batch.poll()
    batch.close()
    assert killed


def test_wrong_claim_receipt_and_private_input_cannot_rebind_a_worker(setup):
    batch, _, _, _, _ = setup
    batch.start()
    job = pool._read(batch._job)
    name = job["requests"][0]["schedule_chain"][0]["origin_file"]
    (batch.output_dir/name).write_bytes(b"changed")
    with pytest.raises(ValueError, match="private_input_changed"):
        pool._reconstruct(job, batch.output_dir, "nominal")
    with pytest.raises(ValueError, match="private_input_changed"):
        batch.poll()
    with pytest.raises(ValueError, match="private_worker_token"):
        pool.worker(batch.output_dir, "nominal", "0"*64)


def test_unsanitized_direct_worker_environment_refuses_before_forecast(setup):
    batch, _, _, _, _ = setup
    batch.start()
    token = sha256(batch._job).hexdigest()
    with patch.dict(os.environ, pool.child_environment(token) | {"DEEPSEEK_API_KEY": "must-not-be-present"}, clear=True):
        with pytest.raises(ValueError, match="unsanitized_worker_environment"):
            pool.worker(batch.output_dir, "nominal", token)
    assert not (batch.output_dir/"nominal"/"worker.claim").exists()


def test_worker_nonzero_fails_batch_without_returning_completed_peer(setup):
    batch, _, children, _, _ = setup
    batch.start()
    complete(batch, 0)
    assert batch.poll() is None
    children[1].returncode = 1
    with pytest.raises(ValueError, match="worker_failed"):
        batch.poll()
    assert batch.state == "failed"


def test_receipt_hash_or_boolean_attempt_is_not_trusted(setup):
    batch, _, _, _, _ = setup
    batch.start()
    complete(batch, 0, receipt_mutation=lambda r:r.update(attempt=True))
    with pytest.raises(ValueError, match="result_or_claim_changed"):
        batch.poll()


def test_preexisting_output_is_not_modified_or_reused(setup):
    batch, _, children, _, _ = setup
    batch.output_dir.mkdir()
    (batch.output_dir/"keep").write_bytes(b"existing user data")
    with pytest.raises(ValueError, match="exclusive_output"):
        batch.start()
    assert [x.name for x in batch.output_dir.iterdir()] == ["keep"] and not children


def test_raw_gzip_expansion_is_bounded_before_unbounded_allocation(monkeypatch):
    monkeypatch.setattr(pool, "MAXIMUM_BYTES", 10)
    with pytest.raises(ValueError, match="raw_output_budget"):
        pool._restore(gzip.compress(b"x"*100))


def test_private_worker_uses_captured_shared_predictor_once_offline_and_preserves_partials(setup, monkeypatch):
    batch, _, _, _, _ = setup
    batch.start()
    calls = []
    def fake(request, *, on_tick):
        calls.append(request.record())
        with pytest.raises(RuntimeError, match="forbidden"):
            returns.forecast_restored_return(None)
        from src.runtime import starship_sixdof as dyn
        state = dyn.state_from_dict(request.origin.record()["state"])
        on_tick(SimpleNamespace(state=state, phase="orbital_coast"))
        return prediction(request)
    monkeypatch.setattr(returns, "forecast_restored_return", fake)
    @contextmanager
    def fake_integration(policy):
        assert type(policy) is pool.RestoredIntegrationPolicy and policy == batch.integration_policy
        yield SimpleNamespace(record=lambda: integration_receipt(policy))
    monkeypatch.setattr(pool, "restored_integration", fake_integration)
    token = sha256(batch._job).hexdigest()
    with patch.dict(os.environ, pool.child_environment(token), clear=True):
        pool.worker(batch.output_dir, "nominal", token)
    assert len(calls) == 1 and list((batch.output_dir/"nominal").glob("progress-*.json"))
    receipt = pool._read((batch.output_dir/"nominal"/"result.json").read_bytes())
    assert receipt["integration_execution"] == integration_receipt(batch.integration_policy)
    with pytest.raises(FileExistsError):
        with patch.dict(os.environ, pool.child_environment(token), clear=True):
            pool.worker(batch.output_dir, "nominal", token)
    assert len(calls) == 1


@pytest.mark.parametrize("value", [None, {}, {"subdivisions": 2}, 2, True])
def test_numerical_policy_must_be_explicit_exact_type(setup, tmp_path, value):
    batch, source, children, _, _ = setup
    with pytest.raises(ValueError, match="typed_integration_policy"):
        pool.RestoredForecastBatch(batch.requests, output_dir=tmp_path/"uncreated",
            wall_deadline_monotonic=1000., expected_source_sha256=source, integration_policy=value)
    assert not (tmp_path/"uncreated").exists() and not children


@pytest.mark.parametrize("subdivisions", [2, 4, 8])
def test_policy_is_bound_explicitly_and_never_inferred_from_same_origin(setup, tmp_path, subdivisions):
    batch, source, _, _, _ = setup
    other = pool.RestoredForecastBatch(batch.requests, output_dir=tmp_path/f"n{subdivisions}",
        wall_deadline_monotonic=1000., expected_source_sha256=source,
        integration_policy=pool.RestoredIntegrationPolicy(subdivisions))
    try:
        other.start()
        job = pool._read(other._job)
        assert job["integration_policy"]["subdivisions"] == subdivisions
        assert other.requests[0].origin is batch.requests[0].origin
    finally:
        other.close()


@pytest.mark.parametrize("mutation", [
    lambda r:r["policy"].update(subdivisions=4),
    lambda r:r.update(status="active"),
    lambda r:r.update(failure_class="ValueError"),
    lambda r:r["trace"].update(rk4_calls=0, rk4_substeps=0),
    lambda r:r["trace"].update(rk4_calls=True),
    lambda r:r["trace"].update(rk4_substeps=1),
    lambda r:r["trace"].update(commands_recomputed_inside_rk4=1),
    lambda r:r["trace"].update(contact_search_changed=True),
])
def test_integration_policy_completion_or_counter_tampering_fails_batch(setup, mutation):
    batch, _, _, killed, _ = setup
    batch.start()
    complete(batch, 0, integration_mutation=mutation)
    with pytest.raises(ValueError, match="integration_"):
        batch.poll()
    batch.close()
    assert batch.state == "failed" and killed


def test_prediction_observer_does_not_add_or_change_steps(monkeypatch):
    expected = prediction.__name__
    tick = SimpleNamespace(state="immutable synthetic state")
    def steps(request, *, prediction):
        assert request == "synthetic request" and prediction is True
        yield tick
        return expected
    monkeypatch.setattr(returns, "_steps", steps)
    seen = []
    # Exercise the actual API over a fake generator, never its dynamics path.
    assert FORECAST_ENTRYPOINT("synthetic request") == FORECAST_ENTRYPOINT("synthetic request", on_tick=seen.append) == expected
    assert seen == [tick] and seen[0] is tick


def test_cli_has_no_public_execution_mode_and_refuses_missing_arguments(tmp_path, monkeypatch):
    with monkeypatch.context() as local:
        local.setattr(subprocess, "Popen", REAL_POPEN)
        result = subprocess.run([sys.executable, str(REPO/pool.SCRIPT)], cwd=REPO,
            capture_output=True, text=True, env={"PATH": os.environ.get("PATH", "")}, timeout=10.)
    assert result.returncode == 2 and "required" in result.stderr
