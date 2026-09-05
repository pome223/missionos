"""PX4 projection for the backend-neutral Mission Assurance Agent.

This adapter observes PX4-shaped evidence and compiles semantic responses into
PX4 candidate verbs.  It does not decide which semantic response is correct.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from src.intelligence.mission_assurance_agent import (
    MISSION_RESPONSE_KINDS,
    MissionResponseProposal,
    MissionSituation,
)

PX4_MISSION_ASSURANCE_ADAPTER_ID = "missionos.px4.mission_assurance.v1"
PX4_RESPONSE_COMPILATION_SCHEMA_VERSION = (
    "missionos_px4_mission_response_compilation.v1"
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def observe_form1_mission_situation(
    *,
    form1: Mapping[str, Any],
    source_check: Mapping[str, Any],
    source_ref: str,
    input_digest: str,
    operator_instruction: Mapping[str, Any] | None = None,
) -> MissionSituation:
    """Project source-bound Form 1 evidence without making a response choice."""

    metrics = _mapping(form1.get("metrics"))
    requested = _mapping(form1.get("requested"))
    observed_at = str(
        form1.get("generated_at")
        or form1.get("completed_at")
        or datetime.now(timezone.utc).isoformat()
    )
    observations = {
        "behavior_delta": {
            "condition_kind": form1.get("condition_kind"),
            "metrics": metrics,
            "requested_condition": requested,
            "causal_form": form1.get("causal_form"),
            "source_supported": source_check.get("source_supported") is True,
        }
    }
    constraints = {
        "source_check": dict(source_check),
        "observed_thresholds": {
            "delta_threshold_m": metrics.get("delta_threshold_m"),
            "climb_time_delta_threshold_seconds": metrics.get(
                "climb_time_delta_threshold_seconds"
            ),
        },
        "thresholds_are_inputs_not_final_judgment": True,
        "operator_instruction": dict(operator_instruction or {}),
    }
    return MissionSituation(
        situation_id=f"mission_situation_{uuid.uuid4().hex[:12]}",
        observed_at=observed_at,
        mission_contract={
            "contract_status": "source_bound_runtime_delta",
            "objective": "preserve the declared mission within active constraints",
        },
        progress={
            "source_progress_counted": form1.get("progress_counted") is True,
            "source_form1_claim_supported": form1.get("form1_claim_supported")
            is True,
        },
        observations=observations,
        constraints=constraints,
        uncertainty={
            "unsupported_reasons": list(
                source_check.get("unsupported_reasons") or []
            ),
            "unverified_is_not_continuable": True,
        },
        source_refs=(source_ref,),
        source_schema_version=str(form1.get("schema_version") or ""),
        input_digest=input_digest,
        execution_scope="simulator",
        allowed_response_kinds=tuple(sorted(MISSION_RESPONSE_KINDS)),
    )


def compile_mission_response_proposal(
    proposal: MissionResponseProposal | Mapping[str, Any],
) -> dict[str, Any]:
    """Compile semantics into a candidate verb without approving or dispatching."""

    payload = (
        proposal.to_dict()
        if isinstance(proposal, MissionResponseProposal)
        else dict(proposal)
    )
    response_kind = str(payload.get("proposed_response_kind") or "")
    action_map: dict[str, str | None] = {
        "continue": None,
        "hold": None,
        "replan": "reroute",
        "return": "return_to_launch",
        "abort": None,
        "operator_escalation": None,
    }
    bounded_action_kind = action_map.get(response_kind)
    if response_kind not in action_map:
        compile_status = "blocked"
        blocking_reasons = ["mission_response_kind_not_supported"]
    elif response_kind == "abort":
        compile_status = "unverified"
        blocking_reasons = [
            f"px4_{response_kind}_candidate_not_bound_to_existing_action_contract"
        ]
    elif bounded_action_kind:
        compile_status = "candidate_compiled"
        blocking_reasons = []
    else:
        compile_status = "no_action_required"
        blocking_reasons = []
    result = {
        "schema_version": PX4_RESPONSE_COMPILATION_SCHEMA_VERSION,
        "adapter_id": PX4_MISSION_ASSURANCE_ADAPTER_ID,
        "compile_status": compile_status,
        "proposal_ref": f"mission_response_proposal:{payload.get('proposal_id')}",
        "proposed_response_kind": response_kind,
        "bounded_action_kind": bounded_action_kind,
        "candidate_parameters": _mapping(payload.get("parameters")),
        "blocking_reasons": blocking_reasons,
        "operator_approval_required": bool(bounded_action_kind),
        "approval_recorded": False,
        "dispatch_authority_created": False,
        "dispatch_request_sent": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    digest = _canonical_sha256(result)
    return {
        **result,
        "compilation_sha256": digest,
        "compilation_id": f"px4_mission_response_compilation_{digest[:12]}",
    }


__all__ = [
    "PX4_MISSION_ASSURANCE_ADAPTER_ID",
    "PX4_RESPONSE_COMPILATION_SCHEMA_VERSION",
    "compile_mission_response_proposal",
    "observe_form1_mission_situation",
]
