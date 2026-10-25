"""Final numerical process ownership, without forecasts, providers or physics.

Synthetic Popen objects test lifecycle and file bindings. The only real worker
path exercised is the saved-only guard on deliberately malformed input.
"""
from hashlib import sha256
import gzip
import os
from pathlib import Path
import signal
import subprocess
import time

import pytest

from src.runtime import starship_final_tasks as tasks
from src.runtime import starship_final_shared as shared
from src.runtime import starship_sixdof as dyn, starship_physics as physics


@pytest.fixture(autouse=True)
def no_physics_or_provider(monkeypatch):
    from src.intelligence.starship_m2_broker import DeadlineTransport

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Numerical owner contracts cannot integrate or invoke providers")

    for owner, name in ((dyn, "step"), (dyn, "_rk4"), (dyn, "_rhs"),
                        (physics, "step"), (DeadlineTransport, "_invoke")):
        monkeypatch.setattr(owner, name, forbidden)


class Clock:
    value = 100.

    def __call__(self):
        return self.value


class Child:
    def __init__(self, pid):
        self.pid, self.returncode, self.waits = pid, None, []
        self.term_timeout = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if timeout is not None and self.term_timeout:
            self.term_timeout = False
            raise subprocess.TimeoutExpired("synthetic-worker", timeout)
        if self.returncode is None:
            self.returncode = -signal.SIGTERM
        return self.returncode


class Factory:
    def __init__(self):
        self.calls, self.children, self.signals = [], [], []
        self.fail_at = None

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if len(self.calls) == self.fail_at:
            raise OSError("synthetic spawn refusal")
        child = Child(800000+len(self.calls))
        self.children.append(child)
        return child

    def kill(self, pid, sig):
        self.signals.append((pid, sig))


@pytest.fixture
def factory(monkeypatch):
    value = Factory()
    monkeypatch.setattr(tasks.subprocess, "Popen", value)
    monkeypatch.setattr(tasks.os, "killpg", value.kill)
    return value


def start(tmp_path, factory, *, kind="forecast", clock=None):
    clock = Clock() if clock is None else clock
    payload = {"jobs": [{"synthetic_case": index} for index in range(6)]} if kind == "forecast" else {
        "bundle": {}, "authority": {}}
    value = tasks.NumericalFuture(tmp_path/"owned", kind, payload,
        deadline_monotonic=110., clock=clock)
    return value, clock


def finish(row, *, change=None):
    value = {"schema": "missionos.starship_final_numerical_result.v1", "kind": "forecast",
        "job_sha256": row["token"], "checker_pid": row["process"].pid,
        "credential_free": True, "result": {"synthetic_index": row["index"]}}
    if change is not None:
        value.update(change)
    (row["folder"]/"result.json.gz").write_bytes(gzip.compress(shared.wire(value), mtime=0))
    row["process"].returncode = 0


def test_forecasts_start_two_and_reap_before_advancing_exact_six(tmp_path, factory):
    value, _ = start(tmp_path, factory)
    assert len(factory.calls) == len(value.running) == 2
    assert len(value.jobs) == 4 and not value.done()
    while value.running:
        for row in list(value.running.values()):
            finish(row)
        value.done()
        assert len(value.running) <= 2
    assert value.result() == [{"synthetic_index": n} for n in range(6)]
    assert len(factory.calls) == 6 and all(child.waits == [None] for child in factory.children)
    assert not value.record()["automatic_retry"] and not value.record()["cancelled"]
    for command, kwargs in factory.calls:
        folder, token = Path(command[-2]), command[-1]
        assert token == sha256((folder/"job.json").read_bytes()).hexdigest()
        assert kwargs["cwd"] == shared.ROOT and kwargs["close_fds"] is True
        assert kwargs["start_new_session"] is True and kwargs["env"][tasks.TOKEN] == token
        assert kwargs["stdout"].closed


@pytest.mark.parametrize("kind,payload", [("forecast", {"jobs": []}),
    ("forecast", {"jobs": [{}]*5}), ("other", {}), ("forecast", {"jobs": ({},)*6})])
def test_unregistered_kind_or_count_never_spawns(tmp_path, factory, kind, payload):
    with pytest.raises(ValueError):
        tasks.NumericalFuture(tmp_path/"owned", kind, payload, deadline_monotonic=110., clock=Clock())
    assert factory.calls == []


def test_worker_environment_drops_host_credentials_and_python_search_injection(monkeypatch):
    for name in ("DEEPSEEK_API_KEY", "JEV_API_KEY", "GCP_TOKEN", "HMAC_SECRET", "PYTHONPATH", "PYTHONHOME"):
        monkeypatch.setenv(name, "synthetic-value-must-not-cross")
    monkeypatch.setenv("PATH", "/usr/bin")
    result = tasks.environment("a"*64)
    assert result["PATH"] == "/usr/bin" and result[tasks.TOKEN] == "a"*64
    assert result["OPENBLAS_NUM_THREADS"] == result["OMP_NUM_THREADS"] == "1"
    assert set(result) <= tasks.ENV_KEYS | {tasks.TOKEN, "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "PYTHONDONTWRITEBYTECODE"}
    assert "synthetic-value-must-not-cross" not in result.values()


def test_cancel_reaps_only_owned_groups_and_is_one_use(tmp_path, factory):
    value, _ = start(tmp_path, factory)
    factory.children[0].term_timeout = True
    assert value.cancel() is True and value.cancel() is False
    assert factory.signals == [(800001, signal.SIGTERM), (800002, signal.SIGTERM), (800001, signal.SIGKILL)]
    assert all(child.returncode is not None for child in factory.children)
    assert len(factory.calls) == 2 and value.jobs == [] and value.record()["cancelled"]
    with pytest.raises(ValueError, match="not_complete"):
        value.result()


def test_deadline_terminates_owned_processes_and_never_starts_pending_work(tmp_path, factory):
    clock = Clock()
    value, _ = start(tmp_path, factory, clock=clock)
    clock.value = 110.
    with pytest.raises(TimeoutError, match="deadline_no_retry"):
        value.done()
    assert value.cancelled and len(factory.calls) == 2 and len(factory.signals) == 2
    assert all(child.returncode is not None for child in factory.children)


def test_nonzero_child_cancels_sibling_and_does_not_retry(tmp_path, factory):
    value, _ = start(tmp_path, factory)
    factory.children[0].returncode = 7
    with pytest.raises(ValueError, match="child_failed_no_retry"):
        value.done()
    assert value.cancelled and len(factory.calls) == 2
    assert factory.signals == [(800002, signal.SIGTERM)]


@pytest.mark.parametrize("failure", [2, 4])
def test_spawn_failure_reaps_already_owned_children_even_without_returned_future(tmp_path, factory, failure):
    factory.fail_at = failure
    if failure == 2:
        with pytest.raises(OSError, match="spawn refusal"):
            start(tmp_path, factory)
    else:
        value, _ = start(tmp_path, factory)
        for row in list(value.running.values()):
            finish(row)
        with pytest.raises(OSError, match="spawn refusal"):
            value.done()
        assert value.cancelled and value.jobs == []
    assert len(factory.calls) == failure
    assert all(child.returncode is not None for child in factory.children)
    assert factory.signals[-1] == (factory.children[-1].pid, signal.SIGTERM)


@pytest.mark.parametrize("change", [{"job_sha256": "f"*64}, {"kind": "guard"},
    {"checker_pid": 999}, {"credential_free": False}])
def test_finished_files_are_bound_to_exact_job_kind_and_owned_pid(tmp_path, factory, change):
    value, _ = start(tmp_path, factory)
    first = True
    while value.running:
        for row in list(value.running.values()):
            finish(row, change=change if first else None)
            first = False
        value.done()
    with pytest.raises(ValueError, match="child_result_binding"):
        value.result()
    assert len(factory.calls) == 6


def test_guard_result_evidence_identifies_separate_saved_checker(tmp_path, factory):
    value, _ = start(tmp_path, factory, kind="guard")
    row = value.running[0]
    finish(row, change={"kind": "guard", "result": {"passed": False}})
    result = value.result()
    assert result["receipt"] == {"passed": False} and result["process_evidence"]["checker_pid"] == 800001
    assert result["process_evidence"]["job_sha256"] == row["token"]
    assert result["process_evidence"]["source_bound_saved_checker"] is True
    assert len(factory.calls) == 1


def test_result_symlink_and_expansion_budget_are_refused(tmp_path, monkeypatch):
    target = tmp_path/"target.gz"
    target.write_bytes(gzip.compress(shared.wire({"x": "a"*300}), mtime=0))
    link = tmp_path/"link.gz"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="missing_or_oversized"):
        tasks.read_result(link)
    monkeypatch.setattr(tasks, "MAXIMUM_BYTES", 100)
    with pytest.raises(ValueError, match="expansion_budget"):
        tasks.read_result(target)


def test_worker_opt_in_and_unexpected_secret_refuse_before_any_task(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    with pytest.raises(ValueError, match="worker_opt_in"):
        tasks.worker(tmp_path, "a"*64)
    monkeypatch.setattr(os, "environ", {tasks.TOKEN: "a"*64, "JEV_API_KEY": "synthetic"})
    with pytest.raises(ValueError, match="unexpected_environment"):
        tasks.worker(tmp_path, "a"*64)


def test_actual_saved_guard_worker_emits_bound_negative_without_physics(tmp_path, monkeypatch):
    folder = tmp_path/"guard"
    folder.mkdir()
    shared.save(folder/"job.json", {"schema": "missionos.starship_final_numerical_job.v1",
        "kind": "guard", "payload": {"bundle": {}, "authority": {}}})
    token = sha256((folder/"job.json").read_bytes()).hexdigest()
    shared.save(folder/"parent-observed.json", observation_ack(token))
    monkeypatch.setattr(os, "environ", {tasks.TOKEN: token})
    tasks.worker(folder, token)
    result = tasks.read_result(folder/"result.json.gz")
    assert result["job_sha256"] == token and result["checker_pid"] == os.getpid()
    assert result["credential_free"] is True and result["result"]["passed"] is False
    with pytest.raises(FileExistsError):
        tasks.worker(folder, token)


def observation_ack(token):
    # A test-only parent witness for this same-process zero-dynamics invocation;
    # this is not evidence of an independently observed numerical child.
    return {"schema": "missionos.starship_final_numerical_parent_observation.v1", "job_sha256": token,
        "pid": os.getpid(), "ppid": os.getppid(), "pgid": os.getpgrp(), "plan_sha256": "a"*64,
        "outer_host_pid": os.getppid(), "observed_wall_s": time.monotonic()}


@pytest.mark.parametrize("mismatch", ["missing", "pid", "job_sha256"])
def test_numerical_worker_refuses_unobserved_or_mismatched_pid_before_saved_checker(tmp_path, monkeypatch, mismatch):
    shared.save(tmp_path/"job.json", {"schema": "missionos.starship_final_numerical_job.v1",
        "kind": "guard", "payload": {"bundle": {}, "authority": {}}})
    token = sha256((tmp_path/"job.json").read_bytes()).hexdigest()
    if mismatch != "missing":
        ack = observation_ack(token)
        ack[mismatch] = os.getpid()+1 if mismatch == "pid" else "f"*64
        shared.save(tmp_path/"parent-observed.json", ack)
    ticks = iter([100., 106., 107.])
    monkeypatch.setattr(tasks.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(tasks.time, "sleep", lambda *_: None)
    monkeypatch.setattr(os, "environ", {tasks.TOKEN: token})
    with pytest.raises(ValueError):
        tasks.worker(tmp_path, token)
    assert (tmp_path/"process-ready.json").is_file() and not (tmp_path/"result.json.gz").exists()


def test_worker_job_mutation_refuses_before_guard_or_forecast(tmp_path, monkeypatch):
    shared.save(tmp_path/"job.json", {"schema": "missionos.starship_final_numerical_job.v1",
        "kind": "guard", "payload": {"bundle": {}, "authority": {}}})
    token = sha256((tmp_path/"job.json").read_bytes()).hexdigest()
    (tmp_path/"job.json").write_text("{}")
    monkeypatch.setattr(os, "environ", {tasks.TOKEN: token})
    with pytest.raises(ValueError, match="input_hash"):
        tasks.worker(tmp_path, token)
    assert not (tmp_path/"result.json.gz").exists()



def test_worker_passes_explicit_policy_to_forecast_without_hardcoded_or_nested_hook(tmp_path, monkeypatch):
    from src.runtime import starship_final_sensor_prediction as prediction
    from src.runtime import starship_restored_integration as integration
    policy = integration.RestoredIntegrationPolicy(4, fuel_event_mode="outer_bracket_v2")
    payload = {"integration_policy": policy.record(), "synthetic_no_propagation": True}
    shared.save(tmp_path/"job.json", {"schema": "missionos.starship_final_numerical_job.v1",
        "kind": "forecast", "payload": payload})
    token = sha256((tmp_path/"job.json").read_bytes()).hexdigest()
    shared.save(tmp_path/"parent-observed.json", observation_ack(token))
    monkeypatch.setattr(os, "environ", {tasks.TOKEN: token})
    seen = []
    def forecast(job):
        seen.append(job)
        return {"synthetic_no_propagation": True, "policy": job["integration_policy"]}
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Task worker must not own a shadow integration hook")
    monkeypatch.setattr(prediction, "forecast_job", forecast)
    monkeypatch.setattr(integration, "restored_integration", forbidden)
    tasks.worker(tmp_path, token)
    result = tasks.read_result(tmp_path/"result.json.gz")
    assert seen == [payload] and result["result"]["policy"] == policy.record()
    assert result["job_sha256"] == token
