from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


def _load_smoke_module() -> Any:
    script_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "smoke_missionos_chat_nova_carter_live_proof.py"
    )
    spec = importlib.util.spec_from_file_location(
        "smoke_missionos_chat_nova_carter_live_proof",
        script_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(
    *,
    dispatch_request_sent: bool = True,
    command_ack_observed: bool = True,
    ack_status: str = "accepted",
    robot_motion_observed: bool = True,
    odom_delta_m: float | None = 0.42,
    completion_claimed: bool = True,
    completion_scope: str = "sim_action",
    physical_execution_invoked: bool = False,
    mission_delivery_completion_claimed: bool = False,
) -> dict[str, Any]:
    execution: dict[str, Any] = {
        "dispatch_request_sent": dispatch_request_sent,
        "robot_motion_observed": robot_motion_observed,
        "completion_claimed": completion_claimed,
        "completion_scope": completion_scope,
        "physical_execution_invoked": physical_execution_invoked,
        "mission_delivery_completion_claimed": mission_delivery_completion_claimed,
        "adapter_evidence": {
            "command_ack_observed": command_ack_observed,
            "ack_status": ack_status,
        },
    }
    if odom_delta_m is not None:
        execution["odom_delta_m"] = odom_delta_m
    return {
        "routed_action": "execute",
        "mission_designer": {
            "summary": {
                "status": "completed" if completion_claimed else "blocked",
                "task_id": "task_nova_carter_live",
                "robot_profile": "nova_carter",
                "execution_target": "isaac_ros_nav2_nova_carter_sim",
                "runtime_substrate": "NVIDIA Isaac Sim + Isaac ROS/Nav2",
                "completion_claimed": completion_claimed,
                "completion_scope": completion_scope,
                "physical_execution_invoked": physical_execution_invoked,
                "mission_delivery_completion_claimed": (
                    mission_delivery_completion_claimed
                ),
            },
            "turtlebot3_home_mission_execution": execution,
            "turtlebot3_home_mission_task": {"task_id": "task_nova_carter_live"},
        },
    }


def test_nova_carter_live_proof_manifest_requires_full_evidence(monkeypatch) -> None:
    module = _load_smoke_module()
    monkeypatch.setenv(
        "MISSIONOS_CHAT_NOVA_CARTER_MAP_ARTIFACT",
        "output/nova_carter/map.html",
    )
    monkeypatch.setenv(
        "MISSIONOS_CHAT_NOVA_CARTER_WATCH_ARTIFACT",
        "output/nova_carter/watch.json",
    )

    manifest = module.build_live_proof_manifest(
        plan={"routed_action": "mission_designer_plan"},
        approved={"routed_action": "approve"},
        executed=_payload(),
    )

    assert manifest["runtime_evidence_ready"] is True
    assert manifest["ready_for_external_claim"] is True
    assert manifest["dispatch_request_sent"] is True
    assert manifest["ack_observed"] is True
    assert manifest["robot_motion_observed"] is True
    assert manifest["odom_delta_m"] == 0.42
    assert manifest["completion_scope"] == "sim_action"
    assert manifest["physical_execution_invoked"] is False
    assert manifest["mission_delivery_completion_claimed"] is False


def test_nova_carter_live_proof_manifest_rejects_ack_without_motion(
    monkeypatch,
) -> None:
    module = _load_smoke_module()
    monkeypatch.setenv(
        "MISSIONOS_CHAT_NOVA_CARTER_MAP_ARTIFACT",
        "output/nova_carter/map.html",
    )
    monkeypatch.setenv(
        "MISSIONOS_CHAT_NOVA_CARTER_WATCH_ARTIFACT",
        "output/nova_carter/watch.json",
    )

    manifest = module.build_live_proof_manifest(
        plan={"routed_action": "mission_designer_plan"},
        approved={"routed_action": "approve"},
        executed=_payload(
            robot_motion_observed=False,
            odom_delta_m=0.0,
            completion_claimed=False,
            completion_scope="none",
        ),
    )

    assert manifest["runtime_evidence_ready"] is False
    assert manifest["ready_for_external_claim"] is False
    assert manifest["required_runtime_evidence"]["ack_observed"] is True
    assert manifest["required_runtime_evidence"]["robot_motion_observed"] is False
    assert manifest["required_runtime_evidence"]["odom_delta_present"] is False
    assert manifest["completion_claimed"] is False


def test_nova_carter_live_proof_manifest_separates_runtime_from_external_artifacts(
    monkeypatch,
) -> None:
    module = _load_smoke_module()
    monkeypatch.delenv("MISSIONOS_CHAT_NOVA_CARTER_MAP_ARTIFACT", raising=False)
    monkeypatch.delenv("MISSIONOS_CHAT_NOVA_CARTER_WATCH_ARTIFACT", raising=False)

    manifest = module.build_live_proof_manifest(
        plan={"routed_action": "mission_designer_plan"},
        approved={"routed_action": "approve"},
        executed=_payload(),
    )

    assert manifest["runtime_evidence_ready"] is True
    assert manifest["ready_for_external_claim"] is False
    assert manifest["required_external_proof"]["map_artifact_recorded"] is False
    assert manifest["required_external_proof"]["watch_artifact_recorded"] is False
    assert manifest["dispatch_request_sent"] is True
    assert manifest["physical_execution_invoked"] is False
