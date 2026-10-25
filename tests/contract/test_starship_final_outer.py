"""Final outer/CLI boundary contracts; no mission, models or numerical steps.

Anonymous pipes are real. Host/process objects for the lifecycle tests are
explicit doubles and cannot establish a completed flight or native judgment.
"""
from copy import deepcopy
import os
from pathlib import Path
import subprocess

import pytest

from src.runtime import starship_final_outer as outer, starship_final_shared as shared
from src.runtime import starship_sixdof as dyn, starship_physics as physics
from scripts import run_starship_final_mission as cli
from scripts import smoke_starship_final_runtime as smoke


@pytest.fixture(autouse=True)
def no_execution(monkeypatch):
    from src.intelligence.starship_m2_broker import DeadlineTransport
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Outer contracts cannot integrate, invoke models or start real processes")
    for owner, name in ((dyn, "step"), (dyn, "_rk4"), (dyn, "_rhs"),
                        (physics, "step"), (DeadlineTransport, "_invoke"), (subprocess, "Popen")):
        monkeypatch.setattr(owner, name, forbidden)


def closed(fd):
    try:
        os.fstat(fd)
    except OSError:
        return True
    return False


def test_nine_distinct_owned_channels_have_exact_directions_and_close_on_error():
    pairs = [os.pipe() for _ in range(9)]
    fds = [pair[1 if i in outer.WRITE_INDICES else 0] for i, pair in enumerate(pairs)]
    opposite = [pair[0 if i in outer.WRITE_INDICES else 1] for i, pair in enumerate(pairs)]
    try:
        with pytest.raises(RuntimeError, match="synthetic"):
            with outer.worker_channels(fds) as channels:
                assert tuple(channels) == outer.CHANNEL_NAMES
                assert len({channel.fd for channel in channels.values()}) == 9
                for i, channel in enumerate(channels.values()):
                    expected = outer.ApprovalPipeWriter if i in outer.WRITE_INDICES else outer.ApprovalPipeReader
                    assert isinstance(channel, expected)
                raise RuntimeError("synthetic")
        assert all(closed(fd) for fd in fds) and all(not closed(fd) for fd in opposite)
    finally:
        for pair in pairs:
            for fd in pair:
                if not closed(fd):
                    os.close(fd)


@pytest.mark.parametrize("fds", [[], [3]*9, list(range(3, 11)), [True]+list(range(4, 12)),
    [2]+list(range(4, 12)), tuple(range(3, 12))])
def test_malformed_nine_fd_sets_are_rejected_before_any_ownership(fds):
    with pytest.raises(ValueError, match="nine_distinct_fds"):
        with outer.worker_channels(fds):
            raise AssertionError("No malformed channel set can enter")


def test_child_environment_is_a_distinct_host_secret_free_projection(monkeypatch):
    for name in ("DEEPSEEK_API_KEY", "JEV_API_KEY", "GCP_TOKEN", "OPERATOR_TOKEN", "HMAC_SECRET", "PYTHONPATH", "PYTHONHOME"):
        monkeypatch.setenv(name, "synthetic-private")
    value = outer.child_environment("a"*64)
    assert value[outer.TOKEN] == "a"*64
    assert "synthetic-private" not in value.values()
    assert set(value) <= {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", outer.TOKEN,
        "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "PYTHONDONTWRITEBYTECODE"}


class Host:
    instances = []
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs, self.ticks, self.closed = args, kwargs, 0, False
        self.instances.append(self)
    def tick(self):
        self.ticks += 1
    def _collect_answers(self):
        pass
    def record(self):
        return {"synthetic_double": True, "provider_calls": 0, "ticks": self.ticks}
    def close(self):
        self.closed = True


class Process:
    pid = 900001
    returncode = 0
    def poll(self):
        return self.returncode
    def wait(self):
        return self.returncode


@pytest.fixture
def lifecycle(monkeypatch):
    plan = {"run_id": "outer-contract", "configuration": {"visual_policy": {}}}
    from src.runtime.starship_restored_integration import RestoredIntegrationPolicy
    integration = RestoredIntegrationPolicy(2, fuel_event_mode="outer_bracket_v2")
    inputs = {"integration_policy": integration.record(), "mode": "fixture", "context": {"run_id": "outer-contract"}, "plan_sha256": "a"*64,
        "source_sha256": {"synthetic.py": "b"*64}, "backend": {}, "return_review_policy": {},
        "bounds": {"per_mission_wall_s": 10., "minimum_free_bytes": 0, "maximum_output_bytes": 2**20}}
    monkeypatch.setattr(outer, "validate_plan", lambda value, **kwargs: value)
    monkeypatch.setattr(outer, "verification_inputs", lambda _: deepcopy(inputs))
    monkeypatch.setattr(outer, "scripted_operator", lambda *_: lambda *_args: None)
    for name in ("RestoredNativeHost", "FinalSupervisionAgent", "FinalSensorAgent", "FinalVisualHost", "FinalObservationHost"):
        monkeypatch.setattr(outer, name, Host)
    monkeypatch.setattr(outer, "FinalVisualPolicy", lambda **_kw: object())
    monkeypatch.setattr(outer, "_table", lambda: {})
    Host.instances = []
    calls = []
    def spawn(command, **kwargs):
        calls.append((command, kwargs))
        return Process()
    monkeypatch.setattr(outer.subprocess, "Popen", spawn)
    return plan, calls


def test_outer_passes_only_nine_fds_and_saves_independent_lifecycle_records(tmp_path, lifecycle):
    plan, calls = lifecycle
    seen = []
    result = outer.run_final_child(plan, folder=tmp_path, worker_command=lambda fds: seen.extend(fds) or ["synthetic"],
        service=object(), address="http://127.0.0.1:1", fixture_visual_endpoint="http://127.0.0.1:2")
    assert len(calls) == 1 and len(seen) == len(set(seen)) == 9
    assert calls[0][1]["pass_fds"] == seen and calls[0][1]["start_new_session"] is True
    assert all(closed(fd) for fd in seen)
    assert result["worker_exit_code"] == 0 and set(result["host_sha256"]) == {"restored", "supplemental", "sensor", "visual"}
    process = shared.read(tmp_path/"outer-process.json")
    assert process["worker_pid"] == 900001 and process["observed_child_groups_absent"] is True
    assert process["all_children_exhaustively_observed"] is False and process["os_attestation"] is False
    assert all((tmp_path/("host-"+name+".json")).is_file() for name in result["host_sha256"])
    assert Host.instances[0].closed
    from src.runtime.starship_restored_integration import RestoredIntegrationPolicy
    expected = RestoredIntegrationPolicy(2, fuel_event_mode="outer_bracket_v2")
    # This constructor input must come from the externally validated plan; the
    # host cannot install an implicit policy unrelated to the actual worker.
    assert Host.instances[0].kwargs["integration_policy"] == expected
    assert Host.instances[0].kwargs["integration_policy"].record() == expected.record()


def test_outer_constructor_failure_closes_all_allocated_endpoints(tmp_path, lifecycle, monkeypatch):
    plan, calls = lifecycle
    owned = []
    original = outer.approval_pipe
    def capture():
        pair = original()
        owned.extend(endpoint.fd for endpoint in pair)
        return pair
    monkeypatch.setattr(outer, "approval_pipe", capture)
    def fail(*_args, **_kw):
        raise RuntimeError("synthetic host constructor refusal")
    monkeypatch.setattr(outer, "RestoredNativeHost", fail)
    with pytest.raises(RuntimeError, match="constructor refusal"):
        outer.run_final_child(plan, folder=tmp_path, worker_command=lambda _fds: ["never"],
            service=object(), address="http://127.0.0.1:1", fixture_visual_endpoint="http://127.0.0.1:2")
    assert calls == [] and len(owned) == 18 and all(closed(fd) for fd in owned)


def test_fixture_live_endpoint_mix_is_refused_before_pipe_creation(tmp_path, lifecycle):
    plan, calls = lifecycle
    with pytest.raises(ValueError, match="fixture_endpoint_scope"):
        outer.run_final_child(plan, folder=tmp_path, worker_command=lambda _fds: ["never"],
            service=object(), address="http://127.0.0.1:1")
    assert calls == [] and not (tmp_path/"camera").exists()


@pytest.mark.parametrize("args,code", [([], 2), (["--help"], 0),
    (["execute", "--plan", "absent", "--output", "output/no"], 2)])
def test_cli_never_executes_without_complete_explicit_command(args, code):
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == code


def test_cli_exact_approval_hash_is_required_before_prerequisite_or_output(tmp_path, monkeypatch):
    path = tmp_path/"plan.json"
    shared.save(path, {"plan_sha256": "a"*64})
    monkeypatch.setattr(cli.planning, "validate_plan", lambda row, **_kw: row)
    def forbidden(*_args):
        raise AssertionError("Wrong approval cannot reach prerequisites, Gateway or output")
    monkeypatch.setattr(cli, "verify_prerequisites", forbidden)
    monkeypatch.setattr(cli, "output_path", forbidden)
    with pytest.raises(ValueError, match="concrete_plan_execution_opt_in"):
        cli.execute(path, "b"*64, "output/never")


def test_cli_worker_wrong_opt_in_refuses_before_claim_channels_or_native_runner(tmp_path, monkeypatch):
    path = tmp_path/"plan.json"
    shared.save(path, {"plan_sha256": "a"*64})
    monkeypatch.setattr(cli.planning, "validate_plan", lambda row, **_kw: row)
    monkeypatch.delenv(outer.TOKEN, raising=False)
    with pytest.raises(ValueError, match="exact_plan_opt_in"):
        cli.worker(path, tmp_path, [])
    assert not (tmp_path/"worker.claim.json").exists()


def test_cli_worker_host_credentials_are_refused_before_claim_or_numerics(tmp_path, monkeypatch):
    path = tmp_path/"plan.json"
    shared.save(path, {"plan_sha256": "a"*64})
    monkeypatch.setattr(cli.planning, "validate_plan", lambda row, **_kw: row)
    monkeypatch.setattr(os, "environ", {outer.TOKEN: "a"*64, "DEEPSEEK_API_KEY": "synthetic"})
    with pytest.raises(ValueError, match="credentials_or_unexpected_environment"):
        cli.worker(path, tmp_path, [])
    assert not (tmp_path/"worker.claim.json").exists()


def test_saved_only_explicitly_blocks_dynamic_steps_and_processes():
    with cli.saved_only():
        for call in (dyn.step, dyn._rk4, dyn._rhs, physics.step, subprocess.Popen):
            with pytest.raises(ValueError, match="saved_checker_cannot_execute"):
                call()


def test_cli_validate_is_zero_step_with_real_entrypoint_dispatch(tmp_path, monkeypatch, capsys):
    path = tmp_path/"plan.json"
    shared.save(path, {"plan_sha256": "a"*64})
    calls = []
    monkeypatch.setattr(cli.planning, "validate_plan", lambda row, **_kw: row)
    monkeypatch.setattr(cli, "verify_prerequisites", lambda row: calls.append(row))
    cli.main(["validate", "--plan", str(path)])
    value = shared.parse(capsys.readouterr().out)
    assert value == {"plan_sha256": "a"*64, "validated": True, "physics_steps": 0, "provider_calls": 0}
    assert len(calls) == 1


def test_no_plant_runtime_smoke_requires_its_own_explicit_run_and_new_output():
    for args in ([], ["--run"], ["--output", "output/never"]):
        with pytest.raises(ValueError, match="explicit_run_and_new_output"):
            smoke.main(args)


def test_imports_resolve_to_the_maintained_current_worktree():
    import missionos_core
    for module in (outer, cli, smoke, missionos_core):
        if getattr(module, "__file__", None):
            assert Path(module.__file__).resolve().is_relative_to(shared.ROOT)
        else:
            assert all(Path(name).resolve().is_relative_to(shared.ROOT) for name in module.__path__)


@pytest.mark.parametrize("when", ["during_child", "after_child", "after_host_records"])
def test_suspend_inclusive_budget_catches_running_or_just_completed_worker(tmp_path, lifecycle, monkeypatch, when):
    plan, calls = lifecycle
    mono, utc = [100.], [1000.]
    pairs = []
    original_pipe = outer.approval_pipe
    def pipe():
        pair = original_pipe();pairs.extend(pair);return pair
    monkeypatch.setattr(outer, "approval_pipe", pipe)
    original_spawn = outer.subprocess.Popen
    def spawn(*a, **kw):
        value=original_spawn(*a, **kw)
        if when == "after_child": utc[0] += 11.
        if when == "during_child":
            count=[0]
            def poll():
                count[0] += 1
                if count[0] == 1:
                    utc[0] += 11.;return None
                return 0
            value.poll=poll
        return value
    monkeypatch.setattr(outer.subprocess, "Popen", spawn)
    original_save=outer.shared.save
    def save(path, value):
        result=original_save(path,value)
        if when == "after_host_records" and path.name == "host-sensor.json": utc[0] += 11.
        return result
    monkeypatch.setattr(outer.shared,"save",save)
    with pytest.raises(ValueError,match="final_outer_wall_budget_stop_no_retry"):
        outer.run_final_child(plan,folder=tmp_path,worker_command=lambda _: ["synthetic"],
            service=object(),address="http://127.0.0.1:1",fixture_visual_endpoint="http://127.0.0.1:2",
            clock=lambda:mono[0],wall_clock=lambda:utc[0])
    assert len(calls)==1 and all(closed(endpoint.fd) for endpoint in pairs)
    stamp=shared.read(tmp_path/"outer-timing.json")["timing"]
    assert stamp["monotonic_elapsed_s"] == 0. and stamp["utc_elapsed_s"] == 11.
    assert stamp["status"] == "exceeded" and not (tmp_path/"outer-process.json").exists()


def test_timing_receipt_write_failure_still_closes_owned_endpoints(tmp_path,lifecycle,monkeypatch):
    plan,_=lifecycle;pairs=[];original_pipe=outer.approval_pipe
    def pipe():
        pair=original_pipe();pairs.extend(pair);return pair
    monkeypatch.setattr(outer,"approval_pipe",pipe)
    original_save=outer.shared.save
    def save(path,value):
        if path.name == "outer-timing.json": raise OSError("synthetic timing receipt write failure")
        return original_save(path,value)
    monkeypatch.setattr(outer.shared,"save",save)
    with pytest.raises(OSError,match="timing receipt"):
        outer.run_final_child(plan,folder=tmp_path,worker_command=lambda _: ["synthetic"],
            service=object(),address="http://127.0.0.1:1",fixture_visual_endpoint="http://127.0.0.1:2")
    assert all(closed(endpoint.fd) for endpoint in pairs) and Host.instances[0].closed
