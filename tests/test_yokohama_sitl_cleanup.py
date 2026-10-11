"""Offline owned-resource cleanup: diagnostics cannot gate removal or reaping."""
import errno
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import yokohama_sitl as runner


class Worker:
    def __init__(self, *, exited=False, terminate_error=False, wait_errors=0, kill_error=False):
        self.calls = []
        self.exited = exited
        self.terminate_error, self.wait_errors, self.kill_error = terminate_error, wait_errors, kill_error

    def poll(self):
        self.calls.append("poll")
        return 0 if self.exited else None

    def terminate(self):
        self.calls.append("terminate")
        if self.terminate_error:
            raise OSError("fixture terminate failure")

    def wait(self, timeout):
        assert timeout == 5
        self.calls.append("wait")
        if self.wait_errors:
            self.wait_errors -= 1
            raise subprocess.TimeoutExpired("owned fixture worker", timeout)
        self.exited = True
        return 0

    def kill(self):
        self.calls.append("kill")
        if self.kill_error:
            raise OSError("fixture kill failure")


def diagnostic_failure(tmp_path, monkeypatch, fault):
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        assert args[-1] == "owned" or args[1] == "exec"
        if fault == "log_timeout" and args[1] == "logs":
            raise subprocess.TimeoutExpired(args, 60)
        if fault == "process_timeout" and args[1] == "exec":
            raise subprocess.TimeoutExpired(args, 60)
        return SimpleNamespace(returncode=0, stdout="fixture diagnostic", stderr="")

    monkeypatch.setattr(runner, "command", command)
    write = Path.write_text

    def limited_write(path, *args, **kwargs):
        if fault == "disk_full" and path.name in {"simulator.stdout", "simulator.stderr", "processes.txt"}:
            raise OSError(errno.ENOSPC, "fixture disk full")
        return write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", limited_write)
    return calls


@pytest.mark.parametrize("fault", ["log_timeout", "process_timeout", "disk_full"])
def test_diagnostic_failures_do_not_skip_owned_cleanup(tmp_path, monkeypatch, fault):
    calls = diagnostic_failure(tmp_path, monkeypatch, fault)
    result = dict(status="failed", reason="Original flight failure")
    worker = Worker()
    try:
        runner.capture_diagnostics(tmp_path, result, "owned")
    finally:
        runner.cleanup_owned(tmp_path, result, created=True, container="owned", worker=worker)
    assert calls[-1] == ["docker", "rm", "-f", "owned"]
    assert worker.calls == ["poll", "terminate", "wait"]
    assert result["cleanup"] and result["worker_reaped"]
    assert result["status"] == "failed" and result["reason"] == "Original flight failure"
    assert any(k.endswith("_error") for k in result)
    assert json.loads((tmp_path / "result.json").read_text())["reason"] == result["reason"]


@pytest.mark.parametrize("fault", ["returncode", "timeout", "io"])
def test_container_removal_failure_still_reaps_worker(tmp_path, monkeypatch, fault):
    def command(args, **kwargs):
        assert args == ["docker", "rm", "-f", "owned"] and kwargs == {"check": False}
        if fault == "timeout":
            raise subprocess.TimeoutExpired(args, 60)
        if fault == "io":
            raise OSError("fixture remove IO failure")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(runner, "command", command)
    worker, result = Worker(), dict(status="passed", reason="preserved")
    runner.cleanup_owned(tmp_path, result, created=True, container="owned", worker=worker)
    assert not result["cleanup"] and result["worker_reaped"]
    assert worker.calls == ["poll", "terminate", "wait"]
    assert result["status"] == "failed" and result["reason"] == "preserved"
    assert "container_remove_error" in result


@pytest.mark.parametrize("terminate_error,wait_errors,kill_error,reaped", [
    (True, 0, False, True), (False, 1, False, True),
    (True, 1, True, True), (False, 2, True, False),
])
def test_worker_failures_are_independent_and_bounded(tmp_path, terminate_error, wait_errors, kill_error, reaped):
    worker = Worker(terminate_error=terminate_error, wait_errors=wait_errors, kill_error=kill_error)
    result = dict(status="failed", reason="original")
    runner.cleanup_owned(tmp_path, result, created=False, container="uncreated", worker=worker)
    assert worker.calls[:3] == ["poll", "terminate", "wait"]
    if wait_errors:
        assert worker.calls == ["poll", "terminate", "wait", "kill", "wait"]
    assert result["worker_reaped"] is reaped and result["reason"] == "original"
    assert (tmp_path / "result.json").exists()


def test_exited_worker_is_reaped_without_signals_or_foreign_container(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "command", lambda *a, **k: pytest.fail("No created container"))
    worker, result = Worker(exited=True), dict(status="failed")
    runner.cleanup_owned(tmp_path, result, created=False, container="foreign", worker=worker)
    assert worker.calls == ["poll", "wait"] and result["worker_reaped"]


def test_recovery_stops_owned_container_for_preservation_before_removal(tmp_path, monkeypatch):
    calls = []
    def command(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(runner, "command", command)
    worker, result = Worker(exited=True), dict(status="failed")
    runner.cleanup_owned(tmp_path, result, created=True, container="owned", worker=worker,
                         preserve_container=True)
    assert calls == [["docker", "stop", "--time", "10", "owned"]]
    assert result["container_stopped_for_preservation"] is True
    assert result["cleanup"] is False and result["worker_reaped"] is True


def test_result_disk_full_is_after_all_cleanup_and_retains_original(tmp_path, monkeypatch):
    calls = diagnostic_failure(tmp_path, monkeypatch, "none")
    monkeypatch.setattr(Path, "write_text", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.ENOSPC, "fixture")))
    worker, result = Worker(), dict(status="failed", reason="original")
    runner.cleanup_owned(tmp_path, result, created=True, container="owned", worker=worker)
    assert calls == [["docker", "rm", "-f", "owned"]]
    assert worker.calls[-1] == "wait" and result["worker_reaped"]
    assert result["reason"] == "original" and "result_write_error" in result


def test_real_owned_child_reaped_without_touching_sibling(tmp_path):
    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]) as sibling:
        owned = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            result = dict(status="failed")
            runner.cleanup_owned(tmp_path, result, created=False, container="none", worker=owned)
            assert owned.poll() is not None and result["worker_reaped"]
            assert sibling.poll() is None
        finally:
            if owned.poll() is None:
                owned.kill()
            owned.wait(timeout=5)
            sibling.terminate()
            sibling.wait(timeout=5)


@pytest.mark.parametrize("fault", ["disk_full", "log_timeout", "diagnostic_interrupt"])
def test_main_finally_boundary_with_mock_transport(tmp_path, monkeypatch, capsys, fault):
    root = tmp_path / "run"
    calls = []
    class MainWorker(Worker):
        returncode = 0

        def wait(self, timeout):
            if timeout != 5:
                self.calls.append("flight_wait")
                raise subprocess.TimeoutExpired("fixture mission", timeout)
            return super().wait(timeout)

    worker = MainWorker()
    interruption = KeyboardInterrupt("fixture diagnostic interruption")

    def command(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "logs"]:
            if fault == "log_timeout":
                raise subprocess.TimeoutExpired(args, 60)
            if fault == "diagnostic_interrupt":
                raise interruption
        return SimpleNamespace(returncode=0, stdout="fixture image", stderr="")

    # No Docker/Gazebo process is invoked. main still constructs the real config,
    # copies its runtime sources and executes its production finally boundary.
    monkeypatch.setattr(runner, "command", command)
    monkeypatch.setattr(runner, "prepare_models", lambda *a, **k: None)
    monkeypatch.setattr(runner, "build_world", lambda *a, **k: {"phase": "contacts"})
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: worker)
    write = Path.write_text

    def limited_write(path, *args, **kwargs):
        if fault == "disk_full" and path.name in {"simulator.stdout", "result.json"}:
            raise OSError(errno.ENOSPC, "fixture disk full")
        return write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", limited_write)
    monkeypatch.setattr(sys, "argv", ["yokohama_sitl.py", "--phase", "contacts", "--approve-sitl", "--output-dir", str(root)])
    if fault == "diagnostic_interrupt":
        with pytest.raises(KeyboardInterrupt) as caught:
            runner.main()
        assert caught.value is interruption
        summary = json.loads((root / "result.json").read_text())
        assert "finalization_interrupted_error" in summary
    else:
        assert runner.main() == 1
        summary = json.loads(capsys.readouterr().out)
        assert "simulator_log_error" in summary["finalization_errors"]
        if fault == "disk_full":
            assert "result_write_error" in summary["finalization_errors"]
    assert summary["reason"].startswith("TimeoutExpired")
    assert any(a[:3] == ["docker", "rm", "-f"] for a in calls)
    assert worker.calls[-3:] == ["poll", "terminate", "wait"]
