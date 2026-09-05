"""Opt-in real Gateway/Nav2 comparison run. No model or executor fixtures.

Run inside the dedicated simulator container after restarting it between modes.
All approvals are explicit test-harness actions authorized by the operator;
counts describe required approval boundaries, not a human usability study.
"""

from __future__ import annotations

# ROS Humble simulator compatibility (Python 3.10).
# ruff: noqa: UP017
import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import request as urlrequest

import yaml

from src.intelligence.mission_assurance_policy import AssurancePolicy, PolicyStore, digest
from src.runtime.turtlebot3_assurance_policy import approval_binding, mission_contract
from src.runtime.turtlebot3_chat_e2e_runner import (
    _approve_turtlebot3_recovery_checkpoint,
    _post_conversation,
)

INSTRUCTION = (
    "TurtleBot3 indoor delivery route. During the mission, "
    "if an obstacle appears, the Recovery Agent should propose "
    "avoid_obstacle, MissionOS should dispatch the bounded "
    "recovery waypoint, then resume delivery."
)


def find(value, key):
    if isinstance(value, dict):
        if isinstance(value.get(key), dict):
            return value[key]
        for item in value.values():
            found = find(item, key)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = find(item, key)
            if found:
                return found
    return {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["human", "bounded"], required=True)
    parser.add_argument("--url", default="http://127.0.0.1:18791")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args()
    # One bounded simulator route may outlast the interactive client's default.
    os.environ.setdefault("MISSIONOS_CHAT_TURTLEBOT3_HOME_MISSION_HTTP_TIMEOUT_SECONDS", "900")
    args.output.mkdir(parents=True, exist_ok=True)
    session = "assurance-policy-" + args.mode + "-" + str(time.time_ns())
    events = []

    def record(name, value):
        (args.output / (name + ".json")).write_text(json.dumps(value, indent=2, default=str))
        print(name, "recorded", flush=True)

    start = time.monotonic()
    for attempt in range(180):
        try:
            with urlrequest.urlopen(args.url + "/health", timeout=1) as response:
                if response.status == 200:
                    break
        except OSError:
            time.sleep(1)
    else:
        raise RuntimeError("simulator_gateway_not_ready")
    plan = _post_conversation(base_url=args.url, session_id=session, instruction=INSTRUCTION)
    record("plan", plan)
    proposal = find(plan, "scenario_proposal")
    if not proposal:
        raise RuntimeError("scenario_proposal_missing")
    approved = _post_conversation(
        base_url=args.url,
        session_id=session,
        instruction="approve",
        context=plan.get("mission_designer") or {},
        route_hint="approve",
    )
    record("mission-approval", approved)
    approval = find(approved, "turtlebot3_home_mission_approval")
    if not approval or approval.get("operator_approved") is not True:
        raise RuntimeError("initial_mission_approval_missing")
    events.append("initial_mission_approval")
    record("approval-events", events)
    if args.mode == "bounded":
        bound_proposal = {
            **proposal,
            "assurance_policy_mission_approval_sha256": approval_binding(approval),
        }
        # Values are fixed before observing any recovery candidate. They bound
        # simulator map-frame target positions and do not replace live path checks.
        point = {
            "target_x_m": {"minimum": -8.0, "maximum": 8.0},
            "target_y_m": {"minimum": -8.0, "maximum": 8.0},
        }
        single = {
            **point,
            "target_yaw_rad": {"minimum": -3.15, "maximum": 3.15},
            "obstacle_avoidance_required": {"equals": True},
        }
        multiple = {
            "recovery_waypoints": {"items": {"properties": point}, "min_items": 1, "max_items": 2},
            "obstacle_avoidance_required": {"equals": True},
        }
        policy = AssurancePolicy.model_validate(
            {
                "version": 1,
                "policy_id": session,
                "mission_id": proposal["proposal_id"],
                "mission_contract_sha256": digest(mission_contract(bound_proposal)),
                "execution_scope": "simulator",
                "mode": "bounded",
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=60)).isoformat(),
                "max_observation_age_seconds": 60,
                "max_total_actions": 3,
                "preserve": ["nav2_path_feasible", "mission_contract_unchanged"],
                "actions": {
                    "avoid_obstacle": {"parameter_variants": [single, multiple], "max_uses": 3},
                    "reroute": {
                        "parameter_variants": [
                            single,
                            {**point, "target_yaw_rad": {"minimum": -3.15, "maximum": 3.15}},
                            point,
                            {
                                **point,
                                "retry_failed_segment_required": {"equals": True},
                                "retry_count": {"minimum": 1.0, "maximum": 1.0},
                            },
                        ],
                        "max_uses": 3,
                    },
                },
                "on_unresolved": "request_human",
            }
        )
        (args.output / "policy.yaml").write_text(
            yaml.safe_dump(policy.model_dump(), sort_keys=False)
        )
        store = PolicyStore(args.db)
        store.approve(
            policy, operator="operator-authorized-simulator-e2e", expected_sha256=policy.sha256
        )
        record("policy-approval", {"policy_sha256": policy.sha256, "scope": "simulator"})
        events.append("initial_policy_approval")
        record("approval-events", events)
    executed = _post_conversation(
        base_url=args.url,
        session_id=session,
        instruction="run",
        context=approved.get("mission_designer") or {},
        route_hint="execute",
    )
    record("execution-initial", executed)
    if args.mode == "human":
        for index in range(3):
            checkpoint = find(executed, "turtlebot3_recovery_checkpoint")
            if checkpoint.get("checkpoint_status") != "awaiting_operator_approval":
                break
            if checkpoint.get("approval_eligible") is not True:
                break
            events.append("individual_recovery_approval")
            record("approval-events", events)
            executed = _approve_turtlebot3_recovery_checkpoint(base_url=args.url, executed=executed)
            record("recovery-" + str(index + 1), executed)
    operation = executed.get("operation_result") or {}
    summary = operation.get("summary") or {}
    policy_execution = find(executed, "assurance_policy_execution")
    comparison = {
        "mode": args.mode,
        "approval_events": events,
        "required_approval_count": len(events),
        "individual_recovery_approval_count": events.count("individual_recovery_approval"),
        "policy_dispatch_count": policy_execution.get("policy_dispatch_count", 0),
        "status": summary.get("status"),
        "completion_claimed": summary.get("completion_claimed"),
        "completed_segment_count": summary.get("segment_completion_count"),
        "planned_segment_count": summary.get("planned_segment_count"),
        "recovery_closed_loop_cycles": summary.get("recovery_closed_loop_cycles"),
        "policy_execution": policy_execution,
        "wall_seconds": round(time.monotonic() - start, 3),
        "physical_execution_invoked": False,
        "count_scope": "operator-authorized harness approval boundaries; not human interaction timing",
    }
    record("comparison", comparison)
    print(
        json.dumps(
            {
                k: v
                for k, v in comparison.items()
                if k not in ["policy_execution", "recovery_closed_loop_cycles"]
            },
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
