"""Read-only description of the configured graph and recorded execution."""

import json
import os

from src.agents.missionos_agents import MISSIONOS_AGENT_BUILDERS, MISSIONOS_OMITTED_AGENTS
from src.intelligence.missionos_adk_v2_shadow_graph import validate_adk_v2_graph_rollout_env
from src.intelligence import missionos_agent_runtime as runtime
from src.intelligence.missionos_mission_incident_graph import MISSION_INCIDENT_GRAPH_NODE_SEQUENCE
from src.intelligence.missionos_mission_incident_continuation_graph import (
    MISSION_INCIDENT_CONTINUATION_NODE_SEQUENCE,
)


def describe_agent_runtime(*, include_latest: bool = False) -> dict:
    rollout = validate_adk_v2_graph_rollout_env()
    mode = (
        "sequential_rollback"
        if rollout["rollback"]
        else "adk_v2_graph_primary"
        if rollout["primary"]
        else "sequential_runner"
    )
    result = {
        "schema_version": "missionos_agent_topology.v1",
        "configured_execution_mode": mode,
        "enabled": os.getenv(runtime.MISSIONOS_AGENT_RUNTIME_ADK_ENABLED_ENV) == "1",
        "agent_count": len(MISSIONOS_AGENT_BUILDERS) + 1,
        "proposal_agent_count": len(MISSIONOS_AGENT_BUILDERS),
        "agents": [*MISSIONOS_AGENT_BUILDERS, "mission_assurance"],
        "omitted_agents": list(MISSIONOS_OMITTED_AGENTS),
        "specialist_by_intent": dict(runtime._CHIEF_TO_SPECIALIST),
        "stages": ["normalize", "chief", "selected_specialist", "safety_critic", "finalize"],
        "proposal_only": True,
        "mission_assurance": {
            "enabled": os.getenv("MISSIONOS_MISSION_ASSURANCE_ADK_ENABLED") == "1"
            or os.getenv("MISSIONOS_JEV_MODE") == "primary",
            "jev_mode": os.getenv("MISSIONOS_JEV_MODE", "off"),
            "llm_health": "not_probed",
        },
        "workflows": {
            "proposal": ["normalize", "chief", "selected_specialist", "safety_critic", "finalize"],
            "mission_incident": list(MISSION_INCIDENT_GRAPH_NODE_SEQUENCE),
            "approved_continuation": list(MISSION_INCIDENT_CONTINUATION_NODE_SEQUENCE),
            "stacking_mission": [
                "prediction",
                "admit_prediction_evidence",
                "mission_assurance",
                "bounded_policy_revalidation",
                "simulator_executor",
                "observe_and_verify",
                "next_observation",
            ],
        },
        "prediction": {
            "admission": "bound_optional_evidence",
            "provider_health": "not_probed",
            "stacking_entrypoint": "prediction serve-stacking-mission",
            "navigation_wam_mode": os.getenv("MISSIONOS_NAVIGATION_WAM_MODE", "off"),
            "navigation_wam_configured": bool(os.getenv("MISSIONOS_NAVIGATION_WAM_CONFIG")),
            "navigation_backends": ["px4", "nav2"],
        },
        "execution": {
            "requires_separate_approval": True,
            "requires_current_feasibility": True,
            "simulator_is_opt_in": True,
            "executor_health": "not_probed",
        },
        "llm_health": "not_probed",
        "deployment_revision": os.getenv("MISSIONOS_DEPLOYMENT_REVISION", ""),
    }
    if include_latest:
        result["latest_recorded_graph"] = None
        root = runtime.ARTIFACT_ROOT / "missionos_agent_graph"
        paths = sorted(root.glob("*.json"), reverse=True)
        if paths:
            try:
                receipt = json.loads(paths[0].read_text())
                result["latest_recorded_graph"] = {
                    key: receipt.get(key)
                    for key in (
                        "graph_run_id",
                        "graph_runtime_status",
                        "workflow_execution_mode",
                        "workflow_node_paths",
                        "artifact_path",
                        "blocking_reasons",
                    )
                }
            except (OSError, ValueError):
                result["latest_recorded_graph"] = {"status": "unreadable"}
    return result
