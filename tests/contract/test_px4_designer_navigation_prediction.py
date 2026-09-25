"""PX4 designer and Gateway prediction boundaries with fixture model IO only."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.smoke_navigation_wam_jev import navigation_fixture, recovery_fixture
from src.gateway import server as gateway
from src.intelligence.missionos_mission_incident_graph import run_missionos_mission_incident_graph
from src.runtime import px4_gazebo_mission_designer_sitl_live_flight_run as designer


def fixtures():
    path = Path(__file__).with_name("test_runtime_recovery_action_feasibility.py")
    spec = importlib.util.spec_from_file_location("px4_action_feasibility_fixtures", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def telemetry_fixture():
    telemetry = fixtures()._telemetry()
    telemetry["observed_at"] = datetime.now(timezone.utc).isoformat()
    return telemetry


@pytest.mark.parametrize("observed_at", [None, "2026-09-01T00:00:00+00:00"])
def test_both_px4_projections_preserve_source_time(observed_at):
    snapshot = {"sample_index": 5, "observed_at": observed_at}
    assert designer._auto_runtime_recovery_agent_telemetry_snapshot(snapshot)["observed_at"] == observed_at
    assert gateway._runtime_recovery_telemetry_from_runtime_snapshot({
        "missionos_auto_mission_runtime_snapshot": snapshot,
    })["observed_at"] == observed_at


@pytest.mark.parametrize("condition", ["valid", "service_failure"])
def test_live_auto_designer_invokes_prediction_before_jev(monkeypatch, tmp_path, condition):
    monkeypatch.setattr(designer, "run_missionos_runtime_recovery_agent_pipeline", lambda **_: recovery_fixture())
    with navigation_fixture(
        tmp_path, backend="px4", policy=designer._runtime_recovery_policy(), condition=condition,
    ) as fixture:
        result = designer._execute_auto_runtime_recovery_agent_with_timeout(
            telemetry_snapshot=telemetry_fixture(), task_id="px4-designer-fixture", timeout_seconds=10,
        )
    graph = result["missionos_mission_incident_graph"]
    assert fixture["calls"]["wam"] == 1
    assert fixture["calls"]["jev"] == int(condition == "valid")
    assert graph["graph_runtime_status"] == (
        "proposal_guardrail_passed" if condition == "valid" else "guardrail_blocked"
    )
    assert graph["dispatch_authority_created"] is False


def test_px4_recompile_requests_fresh_prediction(monkeypatch, tmp_path):
    monkeypatch.setattr(designer, "run_missionos_runtime_recovery_agent_pipeline", lambda **_: recovery_fixture())
    with navigation_fixture(
        tmp_path, backend="px4", policy=designer._runtime_recovery_policy(), jev_mode="off",
    ) as fixture:
        telemetry = telemetry_fixture()
        result = designer._execute_auto_runtime_recovery_agent_with_timeout(
            telemetry_snapshot=telemetry, task_id="px4-designer-fixture", timeout_seconds=10,
        )
        recompiled = recovery_fixture()
        recompiled["agent_invocations"] = []
        telemetry["sample_index"] += 1
        telemetry["observed_at"] = datetime.now(timezone.utc).isoformat()
        graph = designer._run_recompiled_mission_incident_graph(
            telemetry_snapshot=telemetry, task_id="px4-designer-fixture",
            recovery_result=recompiled,
            source_proposal={
                "schema_version": "missionos_runtime_recovery_proposal_evidence.v4",
                "proposal_id": "prior-fixture-proposal",
                "missionos_mission_incident_graph": result["missionos_mission_incident_graph"],
            },
        )
    assert fixture["calls"] == {"wam": 2, "primary": 2, "jev": 0}
    assert graph["graph_runtime_status"] == "proposal_guardrail_passed"
    assert graph["recovery_judgment_inherited"] is True
    assert graph["navigation_prediction"]["status"] == "adopted"
    assert fixture["requests"][0]["request_id"] != fixture["requests"][1]["request_id"]


@pytest.mark.parametrize("change", ["none", "state", "stale", "missing_time", "policy", "missing_prediction"])
def test_gateway_dispatch_revalidates_internal_prediction_against_current_px4_state(
    monkeypatch, tmp_path, change,
):
    helper = fixtures()
    telemetry = telemetry_fixture()
    proposal, policy = helper._v3_proposal(telemetry=telemetry)
    result = helper._operator_chat_agent_result(telemetry=telemetry)
    with navigation_fixture(tmp_path, backend="px4", policy=policy, jev_mode="off"):
        with patch.dict(os.environ, {"MISSIONOS_NAVIGATION_WAM_MODE": "off"} if change == "missing_prediction" else {}):
            graph = run_missionos_mission_incident_graph(
                telemetry_snapshot=telemetry,
                mission_context={"task_id": "px4-dispatch-fixture", "execution_scope": "fixture"},
                recovery_policy=policy, recovery_runner=lambda **_: result, navigation_backend="px4",
            )
        proposal["schema_version"] = "missionos_runtime_recovery_proposal_evidence.v4"
        proposal["missionos_mission_incident_graph"] = graph
        current = deepcopy(telemetry)
        current["sample_index"] += 1
        current["elapsed_seconds"] += 1
        now = datetime.now(timezone.utc)
        if change == "state":
            current["position"]["local_x_m"] = 0.01
        elif change == "stale":
            now += timedelta(seconds=35)
        elif change == "missing_time":
            current.pop("observed_at")
        elif change == "policy":
            monkeypatch.setattr(gateway, "_current_recovery_policy_for_ref", lambda _: {**policy, "navigation_revision": 2})
        parameters = gateway._bounded_operator_recovery_parameters(
            recovery_action="avoid_obstacle",
            body={"recovery_parameters": proposal["intent_compilation"]["compiled_parameters"]},
        )
        check = gateway._runtime_recovery_proposal_revalidation(
            artifacts=helper._dispatch_artifacts(proposal, current),
            recovery_action="avoid_obstacle", recovery_parameters=parameters, now=now,
        )
    assert check["navigation_prediction_revalidation"]["status"] == ("valid" if change == "none" else "blocked"), check
    assert check["validation_status"] == ("valid" if change == "none" else "blocked"), check
    assert not any("dispatch_prediction_rejected:missing" in reason for reason in check["reasons"])
    assert check["dispatch_authority_created"] is False


def test_gateway_operator_recovery_route_uses_px4_provider(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from src.config.settings import reset_settings
    from src.runtime.task_store import reset_task_store
    from src.security import audit

    monkeypatch.chdir(tmp_path)
    for env, filename in (
        ("TASK_STORE_DB_PATH", "tasks.db"), ("MEMORY_DB_PATH", "memory.db"),
        ("AUDIT_LOG_PATH", "audit.log"), ("COMPUTER_TRAJECTORY_DB_PATH", "trajectory.db"),
        ("PHYSICAL_AI_VALIDATION_DB_PATH", "physical.db"),
    ):
        monkeypatch.setenv(env, str(tmp_path / filename))
    monkeypatch.setenv("GATEWAY_API_KEY", "")
    reset_settings()
    reset_task_store()
    audit._audit_logger = None
    telemetry = telemetry_fixture()
    result = fixtures()._operator_chat_agent_result(telemetry=telemetry)
    monkeypatch.setattr(gateway, "run_missionos_runtime_recovery_agent", lambda **_: result)
    with navigation_fixture(tmp_path, backend="px4", policy=gateway._operator_recovery_proposal_policy(), jev_mode="off") as fixture:
        app = gateway.create_missionos_gateway()
        app.task_store.create(
            task_id="px4-operator-fixture", kind="mission_designer_sitl_execution",
            title="PX4 WAM operator route fixture", status="running",
            artifacts={"missionos_runtime_recovery_agent_live_bridge": {"telemetry_snapshot": telemetry}},
        )
        response = TestClient(app.app).post(
            "/missionos/runtime-recovery-agent/propose-for-task",
            json={"task_id": "px4-operator-fixture", "requested_action": "avoid_obstacle"},
        )
        stored = app.task_store.get("px4-operator-fixture")
    assert response.status_code == 200, response.json()
    assert fixture["calls"] == {"wam": 1, "primary": 1, "jev": 0}
    graph = stored["artifacts"]["missionos_mission_incident_graph"]
    assert graph["prediction_admission"]["status"] == "adopted"
    assert graph["dispatch_authority_created"] is False
