"""Authority/lifecycle regressions; mock outcomes here are not flight evidence."""

from copy import deepcopy
import json
import time

import pytest

from src.runtime import px4_depth_navigation as depth
from src.runtime import px4_reobserve_navigation as reobserve
from src.runtime.task_store import TaskStore


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    store = TaskStore(str(tmp_path / "tasks.db"))
    real_lock = depth._exclusive
    monkeypatch.setattr(depth, "_exclusive", lambda name: real_lock(str(tmp_path) + name))
    monkeypatch.setenv("MISSIONOS_PX4_DEPTH_ARTIFACT_ROOT", str(tmp_path / "runs"))
    for key in depth.OPT_INS:
        monkeypatch.setenv(key, "1")
    task = depth.prepare(store, "gap", reobserve=True)["task"]
    approval = depth.approve(store, task["task_id"], actor="test-operator")["execution_operator_approval"]
    return store, task["task_id"], approval["approval_id"]


@pytest.mark.parametrize("scene,flag", [("climb", True), ("gap", "true"), ("gap", 1)])
def test_unsupported_scope_rejected_before_task_creation(tmp_path, scene, flag):
    with pytest.raises(depth.DepthNavigationError):
        depth.prepare(TaskStore(str(tmp_path / "tasks.db")), scene, reobserve=flag)


@pytest.mark.parametrize("change", ["old_scope", "other_task", "route", "checkpoint", "runtime", "expiry"])
def test_changed_authority_never_dispatches(prepared, monkeypatch, change):
    store, task_id, approval_id = prepared
    task = store.get(task_id)
    request = deepcopy(task["artifacts"][depth.REQUEST])
    approval = deepcopy(task["artifacts"][depth.APPROVALS][approval_id])
    if change == "old_scope":
        approval["scope"] = depth.approval_scope({})
    elif change == "other_task":
        approval["task_id"] = "another-task"
    elif change == "route":
        request["routes"]["left_detour"][1] = [1.5, 15, 3]
    elif change == "checkpoint":
        request["checkpoint_enu_m"] = [4, 0, 3]
    elif change == "runtime":
        monkeypatch.setattr(reobserve.trial, "source_hashes", lambda: {})
    elif change == "expiry":
        approval["approved_at_unix_s"] = time.time() - 601
    store.update(task_id, artifacts={depth.REQUEST: request, depth.APPROVALS: {approval_id: approval}})
    with pytest.raises(depth.DepthNavigationError):
        depth.execute(store, task_id, approval_id, runner=lambda *_: pytest.fail("dispatch"))
    assert store.get(task_id)["status"] == "pending"
    assert not store.get(task_id)["artifacts"][depth.APPROVALS][approval_id]["consumed_in_runtime"]


@pytest.mark.parametrize("reached", [False, True])
def test_same_task_tracks_stages_and_safe_abort_is_not_completion(prepared, monkeypatch, reached):
    store, task_id, approval_id = prepared
    observed = {
        "route_id": "left_detour" if reached else None,
        "destination_reached": reached, "safe_abort": not reached,
        "landing_and_disarm_observed": True, "reobserve": True,
    }
    monkeypatch.setattr(depth, "verify_run", lambda *_: observed)

    def runner(root, request, approval, progress):
        assert store.get(task_id)["status"] == "running"
        assert store.get(task_id)["artifacts"][depth.APPROVALS][approval_id]["consumed_in_runtime"]
        for phase in ("checkpoint_stopped", "reobserving_depth", "resume_route_selected", "landing_observed"):
            progress(phase)

    response = depth.execute(store, task_id, approval_id, runner=runner)
    assert response["task"]["task_id"] == task_id
    assert response["task"]["status"] == ("completed" if reached else "failed")
    assert response["summary"]["destination_reached"] is reached
    assert response["summary"]["delivery_completion_claimed"] is False
    entries = response["task"]["artifacts"]["px4_depth_navigation_lifecycle"]["entries"]
    assert [e["sequence"] for e in entries] == list(range(5))
    assert entries[-1]["phase"] == ("verified" if reached else "safe_aborted")
    # Reopening the actual SQLite store retains the same task and evidence.
    assert TaskStore(str(store.db_path)).get(task_id)["artifacts"] == response["task"]["artifacts"]
    with pytest.raises(depth.DepthNavigationError):
        depth.execute(store, task_id, approval_id, runner=runner)


def test_host_failure_preserves_checkpoint_and_consumes_approval(prepared):
    store, task_id, approval_id = prepared

    def runner(root, request, approval, progress):
        progress("checkpoint_stopped")
        raise RuntimeError("fresh observation unavailable")

    with pytest.raises(depth.DepthNavigationError):
        depth.execute(store, task_id, approval_id, runner=runner)
    task = store.get(task_id)
    assert task["status"] == "failed"
    assert task["artifacts"][depth.RESULT]["destination_reached"] is None
    assert task["artifacts"]["px4_depth_navigation_lifecycle"]["entries"][0]["phase"] == "checkpoint_stopped"
    assert task["artifacts"][depth.APPROVALS][approval_id]["consumed_in_runtime"] is True


def test_ack_cannot_complete_reobserve_task(prepared):
    store, task_id, approval_id = prepared
    with pytest.raises(depth.DepthNavigationError):
        depth.execute(store, task_id, approval_id, runner=lambda *_: {"complete": True})
    assert store.get(task_id)["status"] == "failed"


def test_other_flight_cannot_be_attached_to_this_task(prepared, tmp_path):
    store, task_id, approval_id = prepared
    task = store.get(task_id)
    request = task["artifacts"][depth.REQUEST]
    approval = task["artifacts"][depth.APPROVALS][approval_id]
    root = tmp_path / "foreign-flight"
    root.mkdir()
    (root / "gateway-binding.json").write_text(json.dumps({
        **reobserve.binding(request, approval), "task_id": "other-task",
    }))
    with pytest.raises(depth.DepthNavigationError, match="binding"):
        reobserve.verify_run(root, request, approval)


def test_status_exposes_bounded_reobservation_without_delivery_claim():
    from missionos_cli.job_status import _job_operator_summary

    text = "\n".join(_job_operator_summary({"task": {
        "task_id": "test", "kind": depth.KIND, "status": "failed",
        "metadata": {"depth_navigation_phase": "safe_aborted"},
        "artifacts": {depth.REQUEST: {"scene": "gap", "reobserve": True},
                      depth.RESULT: {"destination_reached": False, "safe_abort": True}},
    }}))
    assert "safe_aborted" in text and "Safe abort verified: True" in text
    assert "one planned checkpoint" in text and "payload delivery not evaluated" in text
