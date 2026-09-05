"""Opt-in policy continuation of the normal TurtleBot3 mission runtime.

Only the trusted local PolicyStore supplies authority. Request/proposal fields
cannot create a grant. Simulator dispatch keeps the existing Nav2 feasibility,
checkpoint integrity, executor and outcome verification boundaries.
"""

from __future__ import annotations

# The maintained ROS Humble simulator image uses Python 3.10.
# ruff: noqa: UP017
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.intelligence.mission_assurance_policy import PolicyStore, digest

POLICY_DB_ENV = "MISSIONOS_ASSURANCE_POLICY_DB"


def mission_contract(proposal):
    contract = {
        "operator_instruction": proposal.get("operator_instruction"),
        "objective": proposal.get("mission_objective"),
        "route": proposal.get("indoor_delivery_route"),
    }
    if proposal.get("assurance_policy_mission_approval_sha256"):
        contract["mission_approval_sha256"] = proposal["assurance_policy_mission_approval_sha256"]
    return contract


def approval_binding(approval):
    return digest(
        {
            "ref": approval.get("operator_approval_ref"),
            "approved_at": approval.get("approved_at"),
            "route_authority_sha256": (approval.get("route_authority") or {}).get(
                "route_authority_sha256"
            ),
        }
    )


def bind_policy(proposal, approval):
    # Never accept an injected policy context, even if the local feature is off.
    proposal = dict(proposal)
    proposal.pop("assurance_policy", None)
    proposal.pop("assurance_policy_mission_approval_sha256", None)
    path = os.environ.get(POLICY_DB_ENV, "").strip()
    if not path:
        return proposal, None, None
    store = PolicyStore(Path(path))
    policy = store.for_mission(str(proposal.get("proposal_id") or ""))
    if policy is None:
        return proposal, None, None
    if (
        proposal.get("robot_profile") != "turtlebot3"
        or proposal.get("execution_target") != "ros2_nav2_turtlebot3_sim"
        or policy.execution_scope != "simulator"
    ):
        raise ValueError("assurance_policy_requires_turtlebot3_simulator")
    if not set(policy.preserve).issubset({"nav2_path_feasible", "mission_contract_unchanged"}):
        raise ValueError("unsupported_turtlebot3_policy_preserve_condition")
    # The first mission approval is still required by the normal entrypoint.
    proposal["assurance_policy_mission_approval_sha256"] = approval_binding(approval)
    if digest(mission_contract(proposal)) != policy.mission_contract_sha256:
        raise ValueError("assurance_policy_mission_approval_or_contract_mismatch")
    if not approval.get("operator_approval_ref") or approval.get("operator_approved") is not True:
        raise ValueError("assurance_policy_initial_mission_approval_required")
    proposal["assurance_policy"] = store.context(policy.sha256)
    envelope = dict(proposal.get("autonomy_envelope") or {})
    if envelope.get("preapproved_recovery_actions") or (
        approval.get("autonomy_envelope") or {}
    ).get("preapproved_recovery_actions"):
        raise ValueError("assurance_policy_requires_checkpointed_recovery")
    return proposal, store, policy


@dataclass(frozen=True)
class PolicyGrant:
    """In-process capability bound to one reserved checkpoint, never deserialized."""

    checkpoint_digest: str
    mission_id: str
    policy_sha256: str
    reservation_json: str

    def matches(self, checkpoint, proposal):
        return self.checkpoint_digest == digest(checkpoint) and self.mission_id == proposal.get(
            "proposal_id"
        )

    def receipt(self, checkpoint):
        reservation = json.loads(self.reservation_json)
        return {
            "schema_version": "missionos_turtlebot3_policy_authority.v1",
            "authority_source": "human_approved_policy",
            "policy_sha256": self.policy_sha256,
            "policy_authorization": reservation,
            "policy_authority_ref": f"policy:{self.policy_sha256}:{checkpoint['checkpoint_id']}",
            "operator_approved": False,
            "explicit_recovery_dispatch_approval": False,
            "approval_actor": "human_approved_policy",
            # Compatibility reference for the lower bounded executor. This is
            # a policy reference, never an invented individual approval record.
            "operator_approval_ref": f"policy:{self.policy_sha256}:{checkpoint['checkpoint_id']}",
            "checkpoint_id": checkpoint.get("checkpoint_id"),
            "checkpoint_hash": checkpoint.get("checkpoint_hash"),
            "approved_action": checkpoint.get("selected_action"),
            "approved_parameters": checkpoint.get("approved_parameters"),
        }


def _continue_once(
    *, checkpoint, previous, proposal, approval, store, policy, execute, progress_callback, now
):
    from src.runtime import turtlebot3_home_mission as runtime
    from src.runtime.turtlebot3_mission_incident import continue_turtlebot3_incident

    revalidation = {}
    observed_at = ""

    def validate():
        nonlocal revalidation, observed_at
        reasons = runtime._validate_turtlebot3_recovery_resume(
            checkpoint=checkpoint,
            resume_state=runtime._recovery_resume_payload(previous),
            proposal=proposal,
            goals=runtime._planned_segment_goals_from_proposal(proposal),
            recovery_operator_approval=None,
            policy_precheck=True,
        )
        if not reasons:
            revalidation = runtime._revalidate_approved_recovery_candidate(
                checkpoint=checkpoint,
                obstacle_scenario=runtime._recovery_resume_payload(previous).get(
                    "runtime_recovery_obstacle_scenario"
                )
                or {},
            )
            observed_at = datetime.now(timezone.utc).isoformat()
            if revalidation.get("revalidation_status") != "validated":
                reasons.extend(
                    revalidation.get("blocking_reasons") or ["nav2_fresh_feasibility_required"]
                )
        return reasons

    def facts():
        feasible = (
            revalidation.get("revalidation_status") == "validated"
            and bool(revalidation.get("global_costmap_snapshot_hash"))
            and bool(revalidation.get("local_costmap_snapshot_hash"))
        )
        return {
            "mission_id": proposal["proposal_id"],
            "execution_scope": "simulator",
            "mission_contract_sha256": digest(mission_contract(proposal)),
            "observed_at": observed_at,
            "source_ref": revalidation.get("global_costmap_snapshot_hash"),
            "predicates": {
                "nav2_path_feasible": feasible,
                "mission_contract_unchanged": digest(mission_contract(proposal))
                == policy.mission_contract_sha256,
            },
        }

    def dispatch(state):
        reservation = state.get("policy_authorization") or {}
        if reservation.get("policy_authorized") is not True:
            raise ValueError("reserved_policy_authority_required")
        grant = PolicyGrant(
            digest(checkpoint),
            proposal["proposal_id"],
            policy.sha256,
            json.dumps(reservation, sort_keys=True),
        )
        return execute(
            proposal=proposal,
            approval=approval,
            now=datetime.now(timezone.utc),
            progress_callback=progress_callback,
            resume_execution=runtime._recovery_resume_payload(previous),
            recovery_operator_approval=None,
            policy_grant=grant,
        )

    continued = continue_turtlebot3_incident(
        checkpoint=checkpoint,
        approval={},
        validate=validate,
        execute=dispatch,
        policy_authorization_handler=store.handler(policy.sha256, facts),
    )
    return continued


def run_policy_continuations(
    *, result, proposal, approval, store, policy, execute, progress_callback=None, now=None
):
    from src.runtime import turtlebot3_home_mission as runtime

    attempts = []
    # One extra iteration records why a fully consumed budget cannot continue.
    for _ in range(policy.max_total_actions + 1):
        checkpoint = runtime._recovery_checkpoint_from_execution(result)
        if checkpoint.get("checkpoint_status") != "awaiting_operator_approval":
            break
        previous = result
        continued = _continue_once(
            checkpoint=checkpoint,
            previous=previous,
            proposal=proposal,
            approval=approval,
            store=store,
            policy=policy,
            execute=execute,
            progress_callback=progress_callback,
            now=now,
        )
        graph = continued["missionos_mission_incident_continuation_graph"]
        attempts.append(graph)
        if not graph.get("executor_invoked"):
            # Keep the real paused execution and checkpoint available to the
            # operator. A blocked policy is not a replacement mission result.
            result = previous
            break
        result = continued
        if policy.mode != "bounded":
            break
    summary = result.setdefault("summary", {})
    record = {
        "schema_version": "missionos_turtlebot3_policy_continuation.v1",
        "policy_sha256": policy.sha256,
        "mode": policy.mode,
        "policy_approval_count": 1,
        "individual_recovery_approval_count": 0,
        "policy_executor_invocation_count": sum(
            item.get("executor_invoked") is True for item in attempts
        ),
        "policy_dispatch_count": sum(
            item.get("dispatch_request_sent") is True for item in attempts
        ),
        "continuations": attempts,
        "blocking_reasons": attempts[-1].get("blocking_reasons", []) if attempts else [],
        "physical_execution_invoked": False,
    }
    result["assurance_policy_execution"] = record
    summary["assurance_policy_execution"] = record
    if progress_callback:
        progress_callback(result)
    return result
