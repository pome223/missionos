#!/usr/bin/env python3
"""Opt-in MissionOS -> ROS2/Nav2 TurtleBot3 bounded dispatch smoke."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path

from src.runtime.hardware_adapter_contract import HardwareExecutionMode
from src.runtime.nav2_turtlebot3_predicate_package import (
    Nav2TurtleBot3BoundedDispatchResult,
    Nav2TurtleBot3EvidenceBindings,
    Nav2TurtleBot3PredicateContent,
    build_nav2_turtlebot3_replay_contract,
    build_nav2_turtlebot3_replay_input,
    evaluate_nav2_turtlebot3_predicate,
)
from src.runtime.ros2_nav2_dispatch_bridge import (
    ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV,
    ROS2_NAV2_BRIDGE_COMMAND_ENV,
    Ros2Nav2BridgeCommandClient,
)
from src.runtime.ros2_nav2_hardware_adapter import (
    Nav2GoalPose,
    Ros2Nav2HardwareAdapter,
    Ros2Nav2HardwareAdapterConfig,
)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_ARTIFACT_ROOT_ENV = "ROS2_NAV2_MISSION_CONTRACT_ARTIFACT_ROOT"


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256_path(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _new_run_dir() -> Path:
    root = Path(
        os.environ.get(
            _ARTIFACT_ROOT_ENV,
            "artifacts/ros2_nav2_mission_contract",
        )
    )
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = root / f"nav2_turtlebot3_{stamp}"
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = root / f"nav2_turtlebot3_{stamp}_{suffix}"
    candidate.mkdir()
    return candidate


def build_nav2_turtlebot3_bounded_goal() -> Nav2GoalPose:
    """Build the exact bounded goal used by the opt-in Nav2 smoke."""

    return Nav2GoalPose(
        frame_id=os.environ.get("ROS2_NAV2_GOAL_FRAME_ID", "map"),
        x_m=float(os.environ.get("ROS2_NAV2_GOAL_X_M", "0.75")),
        y_m=float(os.environ.get("ROS2_NAV2_GOAL_Y_M", "0.0")),
        yaw_rad=float(os.environ.get("ROS2_NAV2_GOAL_YAW_RAD", "0.0")),
        tolerance_m=float(os.environ.get("ROS2_NAV2_GOAL_TOLERANCE_M", "0.25")),
        max_speed_mps=0.25,
        max_distance_m=3.0,
        label="turtlebot3_short_nav2_goal",
    )


def build_nav2_turtlebot3_bounded_mission_contract(
    goal_pose: Nav2GoalPose,
):
    """Build the exact frozen Mission Contract used by the opt-in Nav2 smoke."""

    return build_nav2_turtlebot3_replay_contract(
        contract_id="nav2-turtlebot3-bounded-goal",
        contract_version="2026-07-29",
        approved_goal_pose=goal_pose.model_dump(mode="json"),
        approved_goal_frame={"frame_id": goal_pose.frame_id},
        maximum_observation_age_seconds=30.0,
    )


def main() -> int:
    if not _truthy_env(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV):
        print(
            json.dumps(
                {
                    "smoke": "ros2_nav2_turtlebot3_bounded_dispatch",
                    "ran": False,
                    "reason": (
                        f"{ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV} is not set "
                        "to 1; no Nav2 goal was sent."
                    ),
                    "bridge_command_present": bool(
                        os.environ.get(ROS2_NAV2_BRIDGE_COMMAND_ENV, "").strip()
                    ),
                    "dispatch_request_sent": False,
                    "completion_claimed": False,
                    "completion_scope": "none",
                    "physical_execution_invoked": False,
                },
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    goal_pose = build_nav2_turtlebot3_bounded_goal()
    client = Ros2Nav2BridgeCommandClient()
    adapter = Ros2Nav2HardwareAdapter(
        config=Ros2Nav2HardwareAdapterConfig(
            missionos_action_ref="missionos_plan_turtlebot3_bounded_nav2_goal",
            goal_pose=goal_pose,
            execution_mode=HardwareExecutionMode.SIM,
            operator_approval_ref="smoke_operator_approval_nav2_turtlebot3_001",
            approval_actor="smoke-operator",
            approval_timestamp=datetime.now(timezone.utc),
        ),
        client=client,
    )
    evidence = adapter.dispatch_approved_action()
    bridge_responses = client.collect_responses()
    if len(bridge_responses) != 1:
        raise SystemExit(
            "Nav2 TurtleBot3 predicate replay requires exactly one bridge response"
        )
    observed_at = datetime.now(timezone.utc)
    run_dir = _new_run_dir()
    bridge_response_path = run_dir / "bridge_response.json"
    adapter_evidence_path = run_dir / "adapter_evidence.json"
    result_path = run_dir / "bounded_dispatch_result.json"
    _write_json(bridge_response_path, bridge_responses[0])
    _write_json(adapter_evidence_path, evidence.model_dump(mode="json"))
    result = Nav2TurtleBot3BoundedDispatchResult(
        result_id=f"nav2-turtlebot3-{observed_at.strftime('%Y%m%dT%H%M%S%fZ')}",
        observed_at=observed_at,
        requested_goal_pose=goal_pose.model_dump(mode="json"),
        bridge_response=bridge_responses[0],
        adapter_evidence=evidence,
    )
    _write_json(result_path, result.model_dump(mode="json"))
    replayed_result = Nav2TurtleBot3BoundedDispatchResult.model_validate_json(
        result_path.read_text(encoding="utf-8")
    )
    predicate_content = Nav2TurtleBot3PredicateContent.from_result(
        replayed_result,
        evidence_bindings=Nav2TurtleBot3EvidenceBindings(
            bridge_response_sha256=_sha256_path(bridge_response_path),
            adapter_evidence_sha256=_sha256_path(adapter_evidence_path),
        ),
    )
    contract = build_nav2_turtlebot3_bounded_mission_contract(goal_pose)
    replay = build_nav2_turtlebot3_replay_input(
        contract=contract,
        content=predicate_content,
    )
    predicate_evaluation = evaluate_nav2_turtlebot3_predicate(
        contract=contract,
        replay=replay,
        evaluated_at=datetime.now(timezone.utc).isoformat(),
    )
    _write_json(run_dir / "mission_contract.json", contract.to_material())
    _write_json(
        run_dir / "mission_contract_predicate_content.json",
        predicate_content.to_material(),
    )
    _write_json(
        run_dir / "mission_contract_predicate_evaluation.json",
        predicate_evaluation.to_dict(),
    )
    summary = {
        "smoke": "ros2_nav2_turtlebot3_bounded_dispatch",
        "ran": True,
        "bridge_command_present": bool(
            os.environ.get(ROS2_NAV2_BRIDGE_COMMAND_ENV, "").strip()
        ),
        "artifact_dir": str(run_dir),
        "bridge_responses": bridge_responses,
        "evidence": evidence.model_dump(mode="json"),
        "mission_contract_predicate_evaluation": (
            predicate_evaluation.to_dict()
        ),
    }
    _write_json(run_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))

    if evidence.physical_execution_invoked:
        raise SystemExit("Nav2 TurtleBot3 smoke claimed physical execution")
    if evidence.dispatch_request_sent and evidence.completion_scope not in {
        "sim_action",
        "none",
    }:
        raise SystemExit("Nav2 TurtleBot3 smoke used an invalid completion scope")
    if predicate_evaluation.evaluated_outcome_claim:
        if predicate_evaluation.dispatch_authority_created:
            raise SystemExit("predicate evaluation created dispatch authority")
        if predicate_evaluation.runtime_effect_requested:
            raise SystemExit("predicate evaluation requested a runtime effect")
        if predicate_evaluation.operational_closure_created:
            raise SystemExit("predicate evaluation created operational closure")
        if predicate_evaluation.physical_execution_invoked:
            raise SystemExit("predicate evaluation claimed physical execution")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
