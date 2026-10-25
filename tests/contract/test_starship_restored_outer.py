"""Shared mechanical boundary only: mocked children, no flight/model calls."""

from hashlib import sha256
import os
from pathlib import Path
from types import SimpleNamespace
import pytest
from src.runtime import starship_restored_outer as outer


def config(tmp_path):
    return dict(
        folder=tmp_path,
        cwd=tmp_path,
        context={"run_id": "synthetic", "mission_id": "m", "vehicle_id": "ship"},
        plan_sha256="a" * 64,
        source_sha256={
            "src/runtime/starship_restored_outer.py": sha256(
                Path(outer.__file__).read_bytes()
            ).hexdigest()
        },
        profile_raw_bytes=b"synthetic",
        integration_policy=object(),
        mode="live",
        environment={"synthetic": True},
        service=object(),
        worker_command=lambda fds: ["inert", str(fds)],
        worker_token_name="MISSIONOS_OUTER_TEST",
        operator_id="scripted-test-not-human",
        check_wall=lambda: None,
        check_resources=lambda: None,
    )


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    calls = []
    process = SimpleNamespace(returncode=None, pid=999999, polls=0)

    def poll():
        process.polls += 1
        return process.returncode

    process.poll = poll

    class Host:
        approval_host = SimpleNamespace(
            bridge=SimpleNamespace(request={"request_id": "actual-request"})
        )

        def __init__(self, **kwargs):
            calls.append(("host", kwargs))

        def tick(self):
            calls.append(("tick",))
            process.returncode = 0

        def record(self):
            return {"synthetic_host_record": True}

        def close(self):
            calls.append(("close",))

    def spawn(command, **kwargs):
        calls.append(("spawn", command, kwargs))
        return process

    def cleanup(owned, **kwargs):
        assert owned is process and process.polls == 1
        calls.append(("cleanup", kwargs))
        process.returncode = -15
        return {"descendant_cleanup_verified": True}

    monkeypatch.setattr(outer, "RestoredNativeHost", Host)
    monkeypatch.setattr(outer.subprocess, "Popen", spawn)
    monkeypatch.setattr(outer, "cleanup_restored_worker", cleanup)
    monkeypatch.setattr(outer, "tracked_children", lambda process, seen: seen.update({999998}))
    monkeypatch.setattr(
        outer,
        "no_outstanding_children",
        lambda seen, normal_exit=True: {"normal": normal_exit, "seen": sorted(seen)},
    )
    return config(tmp_path), calls, process


def test_live_uses_production_agent_none_and_never_answers_operator_itself(boundary, monkeypatch):
    cfg, calls, _ = boundary
    monkeypatch.setenv("DEEPSEEK_API_KEY", "SYNTHETIC_DO_NOT_INHERIT")
    monkeypatch.setenv("MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE", "live")
    result = outer.run_native_child(**cfg)
    host = next(row[1] for row in calls if row[0] == "host")
    spawn = next(row[2] for row in calls if row[0] == "spawn")
    assert (
        host["mode"] == "live" and host["agent_factory"] is None and result["operator_http"] == []
    )
    assert (
        "DEEPSEEK_API_KEY" not in spawn["env"]
        and "MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE" not in spawn["env"]
    )
    assert (
        host["operator_token"] not in spawn["env"].values()
        and host["signing_key"] not in spawn["env"].values()
    )
    assert len(spawn["pass_fds"]) == 7 and spawn["start_new_session"] is True
    for fd in spawn["pass_fds"]:
        with pytest.raises(OSError):
            os.fstat(fd)
    assert (cfg["folder"] / "host-record.json").read_bytes() == outer.wire(
        {"synthetic_host_record": True}
    )


def test_review_policy_is_source_pinned_and_forwarded_only_by_explicit_opt_in(boundary):
    from copy import deepcopy
    from src.runtime import starship_restored_return_review as review
    cfg, calls, _ = boundary
    policy = deepcopy(review.DEFAULT_POLICY)
    cfg['source_sha256']['src/runtime/starship_restored_return_review.py'] = sha256(
        Path(review.__file__).read_bytes()).hexdigest()
    outer.run_native_child(**cfg, return_review_policy=policy)
    host = next(row[1] for row in calls if row[0] == 'host')
    assert host['return_review_policy'] == policy and host['return_review_policy'] is not policy


def test_review_policy_without_source_pin_stops_before_child_or_host_creation(boundary):
    from src.runtime.starship_restored_return_review import DEFAULT_POLICY
    cfg, calls, _ = boundary
    with pytest.raises(ValueError, match='return_review_source_opt_in'):
        outer.run_native_child(**cfg, return_review_policy=DEFAULT_POLICY)
    assert calls == []


def test_explicit_operator_callback_is_caller_specific_and_retained(boundary):
    cfg, calls, _ = boundary
    observed = []

    def step(host, token, transcript):
        observed.append(host.approval_host.bridge.request["request_id"])
        assert token
        transcript.append(
            {"caller_explicitly_observed_actual_request": True, "not_human_review": True}
        )

    result = outer.run_native_child(**cfg, operator_step=step)
    assert observed == ["actual-request"] and result["operator_http"][0]["not_human_review"] is True
    assert calls[-1] == ("close",)


def test_hardstop_freezes_cleanup_before_any_additional_poll_and_retains_originals(boundary):
    cfg, calls, _ = boundary

    def stop():
        raise ValueError("synthetic_bound")

    cfg["check_wall"] = stop
    with pytest.raises(ValueError, match="synthetic_bound"):
        outer.run_native_child(**cfg)
    assert [row[0] for row in calls][-2:] == ["cleanup", "close"]
    assert (cfg["folder"] / "process-cleanup.json").exists() and (
        cfg["folder"] / "host-record.json"
    ).exists()


@pytest.mark.parametrize(
    "mode,factory", [("live", lambda **_k: None), ("fixture", None), ("unknown", None)]
)
def test_mode_factory_mixing_refuses_before_fds_or_process(tmp_path, monkeypatch, mode, factory):
    monkeypatch.setattr(
        outer, "approval_pipe", lambda: (_ for _ in ()).throw(AssertionError("must refuse first"))
    )
    cfg = config(tmp_path)
    cfg.update(mode=mode, agent_factory=factory)
    with pytest.raises(ValueError, match="provider_mode_required"):
        outer.run_native_child(**cfg)


def test_offline_worker_blocks_network_and_provider_without_any_plant():
    import socket
    from src.intelligence.starship_m2_broker import DeadlineTransport

    with outer.offline_worker():
        with pytest.raises(ValueError, match="forbidden"):
            socket.create_connection(("127.0.0.1", 1))
        with pytest.raises(ValueError, match="forbidden"):
            DeadlineTransport._invoke(None, None, None, None, None)


def test_worker_channels_preserve_exact_owned_directions_and_close_only_worker_ends():
    pairs = [outer.approval_pipe() for _ in range(7)]
    fds = [pair[1 if i in (0, 2, 4, 6) else 0].fd for i, pair in enumerate(pairs)]
    try:
        with outer.worker_channels(fds) as channels:
            assert [channel.fd for channel in channels.values()] == fds
        for fd in fds:
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        for i, pair in enumerate(pairs):
            pair[0 if i in (0, 2, 4, 6) else 1].close()
