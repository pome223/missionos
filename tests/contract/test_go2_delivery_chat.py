import copy
import os
import threading

import pytest

from src.gateway.go2_delivery_chat import Go2ChatService, requested
from src.runtime.task_store import TaskStore


@pytest.fixture
def service(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    for name in (
        "venv/bin/python",
        "rl-sar-zoo/go2_description/mjcf/go2.xml",
        "rl-sar/policy/go2/robot_lab/policy.pt",
    ):
        p = cache / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("fixture")
    monkeypatch.setenv("GO2_DELIVERY_CACHE", str(cache))
    monkeypatch.setenv("MISSIONOS_GO2_OUTPUT_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("RUN_MISSIONOS_GO2_DELIVERY_SIM", "1")
    return Go2ChatService(TaskStore(str(tmp_path / "tasks.db")))


def plan(service, session="one"):
    return service.plan("会議室Aへ届けて", session, "baseline")


def context(task):
    return dict(
        go2_delivery_task_id=task["task_id"],
        go2_proposal_sha256=task["artifacts"]["go2_proposal_sha256"],
    )


def test_proposal_and_approval_do_not_start_a_process(service):
    task = plan(service)
    assert task["status"] == "proposed" and not service.workers
    with pytest.raises(ValueError, match="承認"):
        service.execute(task)
    approved = service.approve(task, "one")
    assert approved["status"] == "approved" and service.active is None


def test_approval_is_bound_to_the_exact_plan_and_inputs(service):
    task = service.approve(plan(service), "one")
    tampered = copy.deepcopy(task)
    tampered["artifacts"]["go2_delivery_proposal"]["scenario"] = "all_blocked"
    with pytest.raises(ValueError, match="変化"):
        service.execute(tampered)
    service.inputs()["policy"].write_text("changed-policy")
    with pytest.raises(ValueError, match="変化"):
        service.execute(task)
    assert service.active is None


def test_cross_session_and_forged_context_are_rejected(service):
    task = plan(service)
    with pytest.raises(ValueError):
        service.task(context(task), "two")
    forged = context(task)
    forged["go2_proposal_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        service.task(forged, "one")


def test_revised_plan_invalidates_prior_approval(service):
    old = service.approve(plan(service), "one")
    new = plan(service)
    assert old["task_id"] != new["task_id"]
    with pytest.raises(ValueError):
        service.execute(service.store.get(old["task_id"]))


def test_duplicate_run_does_not_launch_twice(service, monkeypatch):
    started = threading.Event()
    calls = []

    def worker(*args):
        os.close(args[-1])
        calls.append(args)
        started.set()

    monkeypatch.setattr(service, "_worker", worker)
    approved = service.approve(plan(service), "one")
    first = service.execute(approved)
    assert started.wait(2)
    second = service.execute(service.store.get(first["task_id"]))
    assert first["task_id"] == second["task_id"] and len(calls) == 1
    other = service.approve(plan(service, "two"), "two")
    with pytest.raises(ValueError, match="別のGo2"):
        service.execute(other)


def test_cancel_is_a_request_until_runtime_confirms_stop(service, monkeypatch):
    monkeypatch.setattr(service, "_worker", lambda *args: os.close(args[-1]))
    task = service.execute(service.approve(plan(service), "one"))
    canceled = service.cancel(task)
    assert canceled["status"] == "cancel_requested"
    assert (service.outputs / f"{task['task_id']}.cancel").exists()
    assert "go2_delivery_result" not in canceled["artifacts"]


def test_restart_does_not_replay_an_inflight_mission(service):
    task = plan(service)
    service.store.update(task["task_id"], status="running")
    restarted = Go2ChatService(service.store)
    assert restarted.store.get(task["task_id"])["status"] == "needs_attention"
    assert restarted.active is None


def test_simulator_opt_in_is_checked_at_execution(service, monkeypatch):
    task = service.approve(plan(service), "one")
    monkeypatch.delenv("RUN_MISSIONOS_GO2_DELIVERY_SIM")
    with pytest.raises(ValueError, match="有効"):
        service.execute(task)


@pytest.mark.parametrize("text", ["犬さん、会議室Bへ届けて", "犬さん、屋外の会議室Aへ届けて"])
def test_unsupported_target_does_not_silently_deliver_to_room_a(service, text):
    with pytest.raises(ValueError):
        service.plan(text, "one", "baseline")


def test_catalog_does_not_capture_other_robot_missions():
    assert requested("会議室Aへ届けて")
    assert requested("Go2で配送して")
    assert not requested("TurtleBot3で部屋を一周して")
    assert not requested("TurtleBot3で会議室Aへ届けて")
    assert not requested("犬の写真を見せて")


def test_invalid_go2_registry_context_does_not_fall_through_to_another_router(service):
    response = service.handle(
        {},
        "/run",
        "one",
        {
            "mission_designer_context_ref": "mission_designer_context:go2_unknown",
            "mission_designer_context_error": "not_source_bound",
        },
        lambda *args, **kwargs: pytest.fail("cannot register a forged context"),
    )
    assert response["routed_action"] == "clarification"
    assert not service.workers


def test_cancel_snapshot_does_not_claim_stop_before_final_evidence(service, monkeypatch):
    monkeypatch.setattr(service, "_worker", lambda *args: os.close(args[-1]))
    task = service.execute(service.approve(plan(service), "one"))
    service.store.update(
        task["task_id"], artifacts={"go2_delivery_snapshot": {"phase": "Needs Attention"}}
    )
    task = service.cancel(service.store.get(task["task_id"]))
    response = service.response(task, context(task), "status")
    assert response["mission_designer"]["summary"]["phase"] == "停止を確認中"


def test_host_judgment_requires_active_approved_execution_and_cannot_replay(service, monkeypatch):
    from src.intelligence import go2_supervisor
    from src.runtime.go2_delivery_mission import Go2DeliveryPlan

    calls = []
    monkeypatch.setattr(go2_supervisor, "configuration", lambda: {"model_id": "fixture"})
    monkeypatch.setattr(
        go2_supervisor,
        "judge",
        lambda *args: (
            calls.append(args)
            or {
                "judge_status": "invalid",
                "proposal": {},
            }
        ),
    )
    task = service.approve(service.plan("会議室Aへ届けて", "one", "all_blocked", "agent"), "one")
    identity = task["task_id"]
    request = dict(
        observation_id="decision_1",
        mission_id=identity,
        approved_plan_sha256=Go2DeliveryPlan(
            **task["artifacts"]["go2_delivery_proposal"]["plan"]
        ).digest,
    )
    with pytest.raises(ValueError):
        service._judge_request(identity, request)
    assert not calls
    service.active = identity
    service.store.update(identity, status="running")
    (service.outputs / identity / "supervision").mkdir(parents=True)
    service._judge_request(identity, request)
    assert len(calls) == 1
    with pytest.raises(ValueError):
        service._judge_request(identity, request)
    assert len(calls) == 1
    service.store.update(identity, artifacts={"go2_supervision_request_count": 3})
    with pytest.raises(ValueError):
        service._judge_request(identity, dict(request, observation_id="decision_4"))
    assert len(calls) == 1


def test_simulator_process_does_not_inherit_host_api_credentials(service, monkeypatch):
    import subprocess

    captured = {}
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-private-value")
    monkeypatch.setenv("GATEWAY_API_KEY", "fixture-gateway-value")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "fixture-private-path")

    def popen(*args, **kwargs):
        captured.update(kwargs["env"])
        raise OSError("fixture stops before process creation")

    monkeypatch.setattr(subprocess, "Popen", popen)
    task = plan(service)
    service.outputs.mkdir(parents=True)
    service._worker(task["task_id"], service.inputs(), service.outputs / "fixture-manifest.json")
    assert captured["RUN_MISSIONOS_GO2_DELIVERY_SIM"] == "1"
    assert not any(
        k in captured
        for k in ("DEEPSEEK_API_KEY", "GATEWAY_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS")
    )


def test_restart_blocks_new_delivery_while_legacy_worker_is_alive(service, monkeypatch):
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        old = plan(service)
        folder = service.outputs / old["task_id"]
        service.outputs.mkdir(parents=True, exist_ok=True)
        service.store.update(
            old["task_id"],
            status="running",
            artifacts={
                "go2_output_directory": str(folder),
                "go2_worker_pid": child.pid,
            },
        )
        restarted = Go2ChatService(service.store)
        monkeypatch.setattr(restarted, "_worker", lambda *args: None)
        next_task = restarted.approve(plan(restarted, "two"), "two")
        assert child.poll() is None
        with pytest.raises(ValueError, match="停止確認"):
            restarted.execute(next_task)
        assert restarted.store.get(next_task["task_id"])["status"] == "approved"
        assert (service.outputs / f"{old['task_id']}.cancel").exists()
        stopped = restarted.cancel(restarted.store.get(old["task_id"]))
        assert stopped["status"] == "needs_attention"
        assert stopped["artifacts"]["go2_restart_recovery"]["stop_confirmed"] is False
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_browser_default_uses_available_rules_mode():
    from html.parser import HTMLParser
    from pathlib import Path

    class SelectParser(HTMLParser):
        active = False
        options = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "select":
                self.active = attrs.get("id") == "supervision"
            if tag == "option" and self.active:
                self.options.append(attrs)

        def handle_endtag(self, tag):
            if tag == "select":
                self.active = False

    parser = SelectParser()
    parser.feed((Path(__file__).parents[2] / "src/gateway/static/go2_chat.html").read_text())
    selected = next((x for x in parser.options if "selected" in x), parser.options[0])
    assert selected["value"] == "rules"


def test_restart_waits_for_inherited_child_lease_and_then_allows_new_plan(service, monkeypatch):
    import os
    import subprocess
    import sys
    from src.runtime import go2_worker_lease

    old = plan(service)
    service.outputs.mkdir(parents=True)
    fd, lease = go2_worker_lease.acquire(service.outputs / f"{old['task_id']}.worker.lock")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], pass_fds=(fd,))
    os.close(fd)  # Models loss of the Gateway's descriptor; child owns the fence.
    try:
        service.store.update(
            old["task_id"],
            status="running",
            artifacts={
                "go2_output_directory": str(service.outputs / old["task_id"]),
                "go2_worker_lease": lease,
            },
        )
        restarted = Go2ChatService(service.store)
        next_task = restarted.approve(plan(restarted, "two"), "two")
        with pytest.raises(ValueError, match="停止確認"):
            restarted.execute(next_task)
        assert child.poll() is None
        restarted = Go2ChatService(service.store)  # Pending fence survives another restart.
        with pytest.raises(ValueError, match="停止確認"):
            restarted.execute(next_task)
        child.terminate()
        child.wait(timeout=5)
        launched = threading.Event()

        def no_simulator(*args):
            os.close(args[-1])
            launched.set()

        monkeypatch.setattr(restarted, "_worker", no_simulator)
        assert restarted.execute(next_task)["status"] == "starting"
        assert launched.wait(2)
        prior = restarted.store.get(old["task_id"])
        assert prior["status"] == "needs_attention"
        assert prior["artifacts"]["go2_restart_recovery"]["stop_confirmed"] is True
        assert "go2_delivery_result" not in prior["artifacts"]
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


def test_spawn_window_still_blocks_without_a_child_pid(service):
    import os
    from src.runtime import go2_worker_lease

    old = plan(service)
    service.outputs.mkdir(parents=True)
    fd, lease = go2_worker_lease.acquire(service.outputs / f"{old['task_id']}.worker.lock")
    try:
        service.store.update(
            old["task_id"],
            status="starting",
            artifacts={
                "go2_output_directory": str(service.outputs / old["task_id"]),
                "go2_worker_lease": lease,
            },
        )
        restarted = Go2ChatService(service.store)
        next_task = restarted.approve(plan(restarted, "two"), "two")
        with pytest.raises(ValueError, match="停止確認"):
            restarted.execute(next_task)
    finally:
        os.close(fd)


def test_legacy_attention_and_older_pages_remain_blocked(service, monkeypatch, tmp_path):
    old = plan(service)
    original_root = service.outputs
    service.store.update(
        old["task_id"],
        status="needs_attention",
        artifacts={
            "go2_output_directory": str(original_root / old["task_id"]),
        },
    )
    for i in range(105):
        service.store.create(
            task_id=f"newer_{i}",
            kind="go2_delivery_execution",
            title="newer terminal record",
            status="rejected",
        )
    monkeypatch.setenv("MISSIONOS_GO2_OUTPUT_ROOT", str(tmp_path / "changed-root"))
    restarted = Go2ChatService(service.store)
    assert old["task_id"] in restarted.recovering
    assert (original_root / f"{old['task_id']}.cancel").exists()
    next_task = restarted.approve(plan(restarted, "two"), "two")
    with pytest.raises(ValueError, match="停止確認"):
        restarted.execute(next_task)


def test_missing_or_replaced_lease_is_not_stop_evidence(tmp_path):
    import os
    from src.runtime import go2_worker_lease

    path = tmp_path / "worker.lock"
    fd, lease = go2_worker_lease.acquire(path)
    try:
        assert not go2_worker_lease.stopped(lease)
        path.rename(tmp_path / "original.lock")
        assert not go2_worker_lease.stopped(lease)
        path.touch()
        assert not go2_worker_lease.stopped(lease)
    finally:
        os.close(fd)
    assert not go2_worker_lease.stopped(lease)
    assert not go2_worker_lease.stopped({})
