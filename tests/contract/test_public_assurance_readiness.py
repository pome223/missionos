"""Readiness regressions found at the actual chat/simulator boundary."""

from copy import deepcopy
import math

import pytest
from fastapi.testclient import TestClient

from missionos_cli import cli
from src.gateway import server
from src.runtime import turtlebot3_home_mission as tb3
from src.runtime.nav2_core_action_feasibility_adapter import nav2_recovery_policy


@pytest.mark.parametrize("clearance", [29.2, 29.568, 30.0, 31.0])
def test_preflight_calibration_target_preserves_terrain_policy(clearance):
    altitude = 29.568
    payload = {
        "task": {
            "status": "running",
            "artifacts": {
                "missionos_auto_mission_runtime_snapshot": {
                    "snapshot_status": "running",
                    "heartbeat_observed": True,
                    "nav_state": 3,
                    "local_x_m": 4.571,
                    "local_y_m": 3.759,
                    "altitude_above_home_m": altitude,
                    "distance_to_home_m": 6.0,
                    "wind_speed_mps": 4.0,
                    "terrain_clearance_m": clearance,
                    "terrain_clearance_target_m": 30.0,
                },
            },
        }
    }
    parameters = cli._preflight_offboard_calibration_parameters(payload)
    assert parameters is not None
    assert parameters["target_altitude_m"] >= altitude
    projected_clearance = clearance + parameters["target_altitude_m"] - altitude
    assert projected_clearance >= 30.5 - 1e-9
    assert parameters["target_altitude_m"] - altitude <= 1.301
    dx = parameters["target_x_m"] - 4.571
    dy = parameters["target_y_m"] - 3.759
    assert math.hypot(dx, dy) == pytest.approx(10.0, abs=0.001)
    assert dx * 4.571 + dy * 3.759 == pytest.approx(0.0, abs=0.004)


def test_arena_observation_pose_reserves_goal_tolerance_and_costmap_cell():
    goal = tb3._TURTLEBOT3_DYNAMIC_OBSTACLE_APPROACH_SEGMENT
    # Source-backed scene box (-1.15, -0.5), 0.32 m square; robot radius 0.19 m.
    dx = max(abs(goal.x_m + 1.15) - 0.16, 0.0)
    dy = max(abs(goal.y_m + 0.5) - 0.16, 0.0)
    surface_gap = math.hypot(dx, dy) - tb3._TURTLEBOT3_STOCK_COLLISION_ENVELOPE["radius_m"]
    assert (
        surface_gap - goal.tolerance_m - 0.05
        >= nav2_recovery_policy()["minimum_surface_clearance_m"]
    )
    # Previously observed stopping pose is still correctly rejected.
    old_gap = math.hypot(1.60 - 1.15 - 0.16, 0.80 - 0.50 - 0.16) - 0.19
    assert old_gap < nav2_recovery_policy()["minimum_surface_clearance_m"]


@pytest.mark.parametrize("field", ["local_x_m", "altitude_above_home_m", "terrain_clearance_m"])
@pytest.mark.parametrize("invalid", [float("nan"), float("inf")])
def test_calibration_nonfinite_observation_cannot_create_command(field, invalid):
    snapshot = {
        "snapshot_status": "running",
        "heartbeat_observed": True,
        "nav_state": 3,
        "local_x_m": 0.0,
        "local_y_m": 0.0,
        "altitude_above_home_m": 30.0,
        "distance_to_home_m": 0.0,
        "wind_speed_mps": 4.0,
        "terrain_clearance_m": 30.0,
        "terrain_clearance_target_m": 30.0,
    }
    snapshot[field] = invalid
    assert (
        cli._preflight_offboard_calibration_parameters(
            {
                "task": {
                    "status": "running",
                    "artifacts": {"missionos_auto_mission_runtime_snapshot": snapshot},
                }
            }
        )
        is None
    )


def test_client_distinguishes_direct_land_from_agent_land_approval(monkeypatch):
    from missionos_cli.gateway_client import MissionOSGatewayClient

    requests = []
    monkeypatch.setattr(
        MissionOSGatewayClient,
        "_request",
        lambda _self, *_args, **kwargs: requests.append(kwargs["json"]) or {},
    )
    client = MissionOSGatewayClient(base_url="http://127.0.0.1:18999", timeout=10)
    client.recovery_dispatch(task_id="fixture_land", recovery_action="land")
    client.recovery_dispatch(
        task_id="fixture_land", recovery_action="land", operator_direct_land=True
    )
    assert "operator_direct_land" not in requests[0]
    assert requests[1]["operator_direct_land"] is True
    assert all(p["explicit_recovery_dispatch_approval"] is True for p in requests)


@pytest.fixture
def direct_land_gateway(monkeypatch, tmp_path):
    from src.config.settings import reset_settings
    from src.runtime.task_store import reset_task_store
    from src.security import audit

    monkeypatch.chdir(tmp_path)
    for name, path in (
        ("TASK_STORE_DB_PATH", "tasks.db"),
        ("MEMORY_DB_PATH", "memory.db"),
        ("AUDIT_LOG_PATH", "audit.log"),
    ):
        monkeypatch.setenv(name, str(tmp_path / path))
    reset_settings()
    reset_task_store()
    audit._audit_logger = None
    gateway = server.create_missionos_gateway()
    queued = []
    monkeypatch.setattr(
        server,
        "_write_missionos_auto_operator_recovery_request_to_container",
        lambda **kw: queued.append(kw) or {"written": True},
    )

    def no_direct_mavlink(**_):
        raise AssertionError("Direct LAND must remain on the bound simulator runner")

    monkeypatch.setattr(server, "run_px4_gazebo_emergency_command_dispatch", no_direct_mavlink)
    yield gateway, queued
    reset_settings()
    reset_task_store()
    audit._audit_logger = None


@pytest.mark.parametrize(
    "case",
    [
        "no_proposal",
        "unrelated_stale_proposal",
        "agent_request",
        "approval_missing",
        "hardware",
        "runner_missing",
        "not_running",
        "wrong_action",
    ],
)
def test_direct_land_is_not_agent_proposal_approval(direct_land_gateway, case):
    gateway, queued = direct_land_gateway
    receipt = {
        "operator_recovery_request_container_path": "/tmp/fixture-land.json",
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    artifacts = {"missionos_auto_mission_gui_dispatch_running_receipt": receipt}
    if case == "unrelated_stale_proposal":
        artifacts["missionos_runtime_recovery_last_proposal"] = {
            "schema_version": "missionos_runtime_recovery_proposal_evidence.v3",
            "proposal_id": "fixture_stale_avoid",
            "proposal_status": "stale",
        }
    if case == "hardware":
        receipt["hardware_target_allowed"] = True
    if case == "runner_missing":
        receipt.pop("operator_recovery_request_container_path")
    original = deepcopy(artifacts)
    gateway.task_store.create(
        task_id="fixture_land",
        kind="mission_designer_sitl_execution",
        title="Direct operator LAND",
        status="blocked" if case == "not_running" else "running",
        artifacts=artifacts,
    )
    body = {
        "task_id": "fixture_land",
        "recovery_action": "land",
        "explicit_recovery_dispatch_approval": case != "approval_missing",
        "operator_direct_land": case != "agent_request",
    }
    if case == "wrong_action":
        body["recovery_action"] = "return_to_launch"
    with TestClient(gateway.app) as client:
        response = client.post("/px4-gazebo/mission-scenarios/recovery-dispatch", json=body)
    if case not in {"no_proposal", "unrelated_stale_proposal"}:
        assert response.status_code in {400, 409}, response.text
        assert queued == []
        return
    assert response.status_code == 200, response.text
    assert len(queued) == 1
    request = queued[0]["request_payload"]
    assert request["operator_direct_land"] is True
    assert request["proposal_id"] == ""
    assert request["recovery_action"] == "land"
    receipt = response.json()["missionos_runtime_recovery_dispatch_receipt"]
    boundary = receipt["operator_direct_land_dispatch_boundary"]
    assert boundary["human_approval_observed"] is True
    assert boundary["mission_incident_graph_required"] is False
    assert boundary["recovery_agent_invoked"] is False
    assert boundary["mission_assurance_agent_invoked"] is False
    assert receipt["dispatch_request_sent"] is True
    assert receipt["command_ack_observed"] is False
    assert receipt["effect_observed"] is False
    assert receipt["physical_execution_invoked"] is False
    assert gateway.task_store.get("fixture_land")["artifacts"].get(
        "missionos_runtime_recovery_last_proposal"
    ) == original.get("missionos_runtime_recovery_last_proposal")
