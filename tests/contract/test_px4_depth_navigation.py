from copy import deepcopy
import hashlib
import json
import time

import pytest

from src.runtime import px4_depth_navigation as depth
from src.runtime.task_store import TaskStore


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    store = TaskStore(str(tmp_path / "tasks.db"))
    real_lock = depth._exclusive
    monkeypatch.setattr(
        depth, "_exclusive", lambda name: real_lock(str(tmp_path) + name)
    )
    monkeypatch.setenv("MISSIONOS_PX4_DEPTH_ARTIFACT_ROOT", str(tmp_path / "runs"))
    for name in depth.OPT_INS:
        monkeypatch.setenv(name, "1")
    task = depth.prepare(store, "climb")["task"]
    approval = depth.approve(store, task["task_id"], actor="test-operator")[
        "execution_operator_approval"
    ]
    return store, task["task_id"], approval["approval_id"]


def test_prepare_cannot_create_authority_or_success(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    result = depth.prepare(store, "climb")
    assert result["task"]["status"] == "pending"
    assert depth.APPROVALS not in result["task"]["artifacts"]
    assert result[depth.REQUEST]["delivery_completion_claimed"] is False
    with pytest.raises(depth.DepthNavigationError):
        depth.prepare(store, "arbitrary-city")


@pytest.mark.parametrize(
    "change",
    ["missing", "expired", "future", "consumed", "request", "runtime", "opt_in"],
)
def test_invalid_authority_never_calls_executor(prepared, monkeypatch, change):
    store, task_id, approval_id = prepared
    task = store.get(task_id)
    approval = deepcopy(task["artifacts"][depth.APPROVALS][approval_id])
    if change == "missing":
        approval_id = "missing"
    elif change == "expired":
        approval["approved_at_unix_s"] = time.time() - 601
    elif change == "future":
        approval["approved_at_unix_s"] = time.time() + 10
    elif change == "consumed":
        approval["consumed_in_runtime"] = True
    elif change == "request":
        approval["request_sha256"] = "0" * 64
    elif change == "runtime":
        monkeypatch.setattr(depth, "source_hashes", lambda: {})
    elif change == "opt_in":
        monkeypatch.delenv(depth.OPT_INS[0])
    if change not in ("missing", "runtime", "opt_in"):
        store.update(task_id, artifacts={depth.APPROVALS: {approval_id: approval}})
    calls = []
    with pytest.raises(depth.DepthNavigationError):
        depth.execute(store, task_id, approval_id, runner=lambda *_: calls.append(True))
    assert calls == []
    assert store.get(task_id)["status"] == "pending"


def test_executor_success_alone_cannot_complete_task(prepared):
    store, task_id, approval_id = prepared
    with pytest.raises(depth.DepthNavigationError, match="verification failed"):
        depth.execute(store, task_id, approval_id, runner=lambda *_: {"complete": True})
    task = store.get(task_id)
    assert task["status"] == "failed"
    assert task["artifacts"][depth.RESULT]["destination_reached"] is None
    assert (
        task["artifacts"][depth.APPROVALS][approval_id]["consumed_in_runtime"] is True
    )
    with pytest.raises(depth.DepthNavigationError, match="pending"):
        depth.execute(
            store, task_id, approval_id, runner=lambda *_: pytest.fail("replay")
        )


def test_verified_result_is_bound_to_same_task_without_delivery_claim(
    prepared, monkeypatch
):
    store, task_id, approval_id = prepared
    observed = {
        "route_id": "climb",
        "destination_reached": True,
        "landing_and_disarm_observed": True,
        "delivery_completion_claimed": False,
    }
    monkeypatch.setattr(depth, "verify_run", lambda *_: observed)

    def runner(root, request, approval, progress):
        assert store.get(task_id)["status"] == "running"
        assert approval["task_id"] == task_id
        assert request["scene"] == "climb"
        progress("route_selected", {"route_id": "climb"})

    result = depth.execute(store, task_id, approval_id, runner=runner)
    assert result["task"]["status"] == "completed"
    assert result["summary"]["delivery_completion_claimed"] is False
    with pytest.raises(depth.DepthNavigationError):
        depth.execute(store, task_id, approval_id, runner=runner)


def test_concurrent_dispatch_rejected_before_consumption(prepared):
    store, task_id, approval_id = prepared
    with depth._exclusive("urban-depth-simulator"):
        with pytest.raises(depth.DepthNavigationError, match="in progress"):
            depth.execute(
                store, task_id, approval_id, runner=lambda *_: pytest.fail("concurrent")
            )
    assert store.get(task_id)["status"] == "pending"


def test_status_does_not_project_depth_arrival_as_delivery():
    from missionos_cli.job_status import _job_operator_summary

    lines = _job_operator_summary(
        {
            "task": {
                "task_id": "test",
                "kind": depth.KIND,
                "status": "completed",
                "metadata": {},
                "artifacts": {
                    depth.REQUEST: {"scene": "climb"},
                    depth.RESULT: {
                        "route_id": "climb",
                        "destination_reached": True,
                        "landing_and_disarm_observed": True,
                    },
                },
            }
        }
    )
    assert any("climb" in line for line in lines)
    assert any("payload delivery not evaluated" in line for line in lines)


def test_verifier_retains_depth_result_schema(prepared, monkeypatch, tmp_path):
    from scripts import verify_urban_wam_trial

    store, task_id, approval_id = prepared
    request = store.get(task_id)["artifacts"][depth.REQUEST]
    approval = store.get(task_id)["artifacts"][depth.APPROVALS][approval_id]
    run = tmp_path / "recorded-flight"
    (run / "session").mkdir(parents=True)
    records = {
        "gateway-binding.json": {
            "task_id": task_id,
            "request_sha256": depth.digest(request),
            "execution_approval_id": approval_id,
        },
        "container.json": {"image_id": depth.IMAGE},
        "session/config.json": {
            "approved_instruction_ref": "gateway-depth-approval:" + approval_id
        },
        "session/contact-positive-control.json": {
            "subscriptions_released": True,
            "probe_removed_observed": True,
            "probe_contacts": 4,
        },
    }
    for name, payload in records.items():
        (run / name).write_text(json.dumps(payload))
    (run / "contact-process.json").write_text(
        json.dumps(
            {
                "exit_code": 0,
                "receipt_sha256": hashlib.sha256(
                    (run / "session/contact-positive-control.json").read_bytes()
                ).hexdigest(),
            }
        )
    )
    monkeypatch.setattr(
        verify_urban_wam_trial,
        "verify",
        lambda _: {
            "schema_version": "missionos_urban_route_verification.v1",
            "family": "headroom_climb_0",
            "route_id": "climb",
            "route_simulation_seconds": 28,
            "route_wall_seconds": 140,
            "ground_contact_positive_control_messages": 2,
        },
    )
    result = depth.verify_run(run, request, approval)
    assert result["schema_version"] == "px4_depth_navigation_result.v1"
    assert result["request_sha256"] == depth.digest(request)
