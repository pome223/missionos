import copy
import json
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.gateway.yokohama_map import MapRequest, MapService, build_yokohama_map_router
from src.runtime.task_store import TaskStore
from src.runtime.yokohama_goal import GoalPlanner, approved_map_plan, source_route
from src.runtime.yokohama_goal_fixture import run, verify
from src.runtime.yokohama_payload import digest
from src.runtime.yokohama_scene import to_source, to_world


@pytest.fixture(scope="module")
def planner():
    return GoalPlanner()


@pytest.fixture
def current(tmp_path, monkeypatch, planner):
    monkeypatch.setenv("MISSIONOS_YOKOHAMA_MAP_BACKEND", "fixture")
    monkeypatch.setenv("RUN_MISSIONOS_YOKOHAMA_MAP_FIXTURE", "1")
    monkeypatch.setattr("src.runtime.yokohama_goal.GoalPlanner", lambda: planner)
    return MapService(TaskStore(str(tmp_path / "tasks.db")), tmp_path / "runs", delay=0)


def body(task=None, xy=None, session="test_session_123456"):
    return MapRequest(
        session_id=session,
        goal_xy_m=xy,
        task_id=task["task_id"] if task else None,
        plan_sha256=task["artifacts"]["plan_sha256"] if task else None,
    )


def test_real_source_candidates_and_legacy(planner):
    default = planner.plan([207.894, -115.179])
    moved = planner.plan([217.894, -115.179])
    assert default["legacy_route"]
    assert default["urban_route_source_xyz_m"] == [p["xyz_m"] for p in planner.legacy["waypoints"]]
    assert moved["goal_source_xyz_m"][:2] == [217.894, -115.179]
    assert moved["urban_route_source_xyz_m"][:3] == default["urban_route_source_xyz_m"][:3]
    assert to_source(
        to_world(moved["goal_source_xyz_m"], moved["frame"]), moved["frame"]
    ) == pytest.approx(moved["goal_source_xyz_m"])
    route = source_route(moved, planner.legacy)
    assert route["delivery_pad"]["center_xyz_m"][:2] == [217.894, -115.179]
    assert route["delivery_pad"]["center_xyz_m"][2] == pytest.approx(
        moved["goal_source_xyz_m"][2] + 0.12
    )


@pytest.mark.parametrize(
    "xy",
    [
        [float("nan"), 0],
        [float("inf"), 0],
        [300, 0],
        [0, 0],
        [197.894, -115.179],
        [True, 0],
        [100],
        [240, -115],
    ],
)
def test_unsafe_goals_rejected(planner, xy):
    with pytest.raises(ValueError):
        planner.plan(xy)


def test_goal_approval_fixture_observed_delivery_and_return(current, tmp_path):
    first = current.select(body(xy=[207.894, -115.179]), "operator")
    approved = current.action("approve", body(first), "operator")
    assert approved["status"] == "approved"
    moved = current.select(body(xy=[217.894, -115.179]), "operator")
    assert current.store.get(first["task_id"])["status"] == "superseded"
    with pytest.raises(ValueError):
        current.action("execute", body(first), "operator")
    current.action("approve", body(moved), "operator")
    current.action("execute", body(moved), "operator")
    current.worker.join(timeout=10)
    done = current.store.get(moved["task_id"])
    assert done["status"] == "completed"
    assert done["artifacts"]["result"]["home_returned"]
    folder = current.outputs / moved["task_id"]
    plan = approved_map_plan(folder / "approved.json")
    observations = [
        json.loads(line) for line in (folder / "observations.jsonl").read_text().splitlines()
    ]
    assert verify(plan, observations)["payload_received"]
    for row in observations:
        if row["kind"] == "payload_pose":
            row["xyz_m"] = first["artifacts"]["plan"]["goal_source_xyz_m"]
    assert not verify(plan, observations)["passed"]


def test_invalid_marker_still_invalidates_approval(current):
    task = current.select(body(xy=[207.894, -115.179]), "operator")
    current.action("approve", body(task), "operator")
    with pytest.raises(ValueError):
        current.select(body(xy=[900, 0]), "operator")
    assert current.store.get(task["task_id"])["status"] == "superseded"


def test_active_rejects_marker_and_duplicate_dispatch(current):
    current.delay = 0.01
    task = current.select(body(xy=[217.894, -115.179]), "operator")
    current.action("approve", body(task), "operator")
    current.action("execute", body(task), "operator")
    again = current.action("execute", body(task), "operator")
    assert again["status"] in {"starting", "running"}
    with pytest.raises(ValueError, match="飛行中"):
        current.select(body(xy=[207.894, -105.179]), "operator")
    with pytest.raises(ValueError, match="飛行中"):
        current.action("invalidate", body(task), "operator")
    current.action("cancel", body(task), "operator")
    current.worker.join(timeout=10)
    assert current.store.get(task["task_id"])["status"] == "canceled"


def test_owner_and_plan_binding(current, tmp_path):
    task = current.select(body(xy=[207.894, -105.179]), "one")
    with pytest.raises(ValueError):
        current.action("approve", body(task), "two")
    with pytest.raises(ValueError):
        current.action("approve", body(task, session="other_session_123456"), "one")
    current.action("approve", body(task), "one")
    plan = copy.deepcopy(task["artifacts"]["plan"])
    plan["goal_source_xyz_m"][0] += 1
    value = dict(plan=plan, approval=current.store.get(task["task_id"])["artifacts"]["approval"])
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        approved_map_plan(path)
    value["approval"]["plan_sha256"] = digest(plan)
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        approved_map_plan(path)


def test_runtime_source_change_rejects_dispatch(current, monkeypatch):
    task = current.select(body(xy=[217.894, -115.179]), "one")
    current.action("approve", body(task), "one")
    monkeypatch.setattr("src.runtime.yokohama_goal.runtime_hashes", lambda: {})
    with pytest.raises(ValueError, match="runtime"):
        current.action("execute", body(task), "one")


def test_router_real_http_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("MISSIONOS_YOKOHAMA_MAP_BACKEND", "fixture")
    monkeypatch.setenv("RUN_MISSIONOS_YOKOHAMA_MAP_FIXTURE", "1")
    monkeypatch.setenv("MISSIONOS_YOKOHAMA_MAP_OUTPUT_ROOT", str(tmp_path / "runs"))
    app = FastAPI()
    router = build_yokohama_map_router(
        TaskStore(str(tmp_path / "tasks.db")), lambda request: "operator"
    )
    app.include_router(router)
    # Exercise real HTTP/worker/observation boundaries without the operator UI's
    # presentation delay; that accumulated delay can outlast the test's join.
    router.map_service().delay = 0
    with TestClient(app) as client:
        assert client.get("/missionos/yokohama/map").status_code == 200
        task = client.post(
            "/missionos/yokohama/map/plan", json=body(xy=[217.894, -115.179]).model_dump()
        ).json()
        b = body(task).model_dump()
        assert client.post("/missionos/yokohama/map/execute", json=b).status_code == 409
        assert client.post("/missionos/yokohama/map/approve", json=b).status_code == 200
        assert client.post("/missionos/yokohama/map/execute", json=b).status_code == 200
        router.map_service().worker.join(timeout=10)
        assert not router.map_service().worker.is_alive(), "fixture worker did not finish"
        state = client.get("/missionos/yokohama/map/state?session_id=test_session_123456").json()
        assert state["task"]["status"] == "completed"


def test_verifier_does_not_accept_return_or_goal_intent(planner):
    plan = planner.plan([217.894, -115.179])
    observations = []
    result = run(plan, observations.append, threading.Event(), delay=0)
    assert result["passed"]
    observations[-1]["xyz_m"][0] += 10
    assert not verify(plan, observations)["passed"]
    assert not verify(plan, [])["passed"]


def test_two_tab_old_marker_is_explicit_conflict(current):
    old = current.select(body(xy=[207.894, -115.179]), "operator")
    new = current.select(body(xy=[217.894, -115.179]), "operator")
    current.action("approve", body(new), "operator")
    with pytest.raises(ValueError, match="別タブ"):
        current.action("invalidate", body(old), "operator")
    assert current.store.get(new["task_id"])["status"] == "approved"


def test_sitl_metadata_matches_plan(current):
    current.backend = "sitl"
    task = current.select(body(xy=[217.894, -115.179]), "operator")
    assert task["metadata"]["execution_target"] == task["artifacts"]["plan"]["execution_target"]
    assert task["metadata"]["execution_target"] == "px4_gazebo_fixture"


def test_old_tab_http_409_and_current_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MISSIONOS_YOKOHAMA_MAP_BACKEND", "fixture")
    app = FastAPI()
    router = build_yokohama_map_router(
        TaskStore(str(tmp_path / "tabs.db")), lambda request: "operator"
    )
    app.include_router(router)
    with TestClient(app) as client:
        old = client.post(
            "/missionos/yokohama/map/plan", json=body(xy=[207.894, -115.179]).model_dump()
        ).json()
        new = client.post(
            "/missionos/yokohama/map/plan", json=body(xy=[217.894, -115.179]).model_dump()
        ).json()
        assert (
            client.post("/missionos/yokohama/map/approve", json=body(new).model_dump()).status_code
            == 200
        )
        assert (
            client.post(
                "/missionos/yokohama/map/invalidate", json=body(old).model_dump()
            ).status_code
            == 409
        )
        state = client.get("/missionos/yokohama/map/state?session_id=test_session_123456").json()
        assert state["task"]["task_id"] == new["task_id"]


def test_map_store_failure_after_reservation_releases_dispatch(current, monkeypatch):
    task = current.select(body(xy=[217.894, -115.179]), "operator")
    current.action("approve", body(task), "operator")
    original = current.store.update
    def failing(identity, **kwargs):
        if kwargs.get("status") == "starting":
            raise OSError("fixture database unavailable")
        return original(identity, **kwargs)
    monkeypatch.setattr(current.store, "update", failing)
    with pytest.raises(OSError):
        current.action("execute", body(task), "operator")
    assert current.dispatch.owner is None and current.active is None and current.worker is None
