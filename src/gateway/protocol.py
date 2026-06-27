"""
Typed Gateway Protocol v1

Envelope format (all messages):
  {
    "v": 1,
    "event": "<event_name>",
    "request_id": "<optional correlation id>",
    "ts": <unix timestamp>,
    ...event-specific fields
  }

Client -> Server:
  chat.send           text, request_id?
  control.run         goal, constraints?, request_id?
  chat.inject         text, role?, request_id?
  chat.abort          request_id?
  chat.history        session_id?, limit?, before?
  presence.ping       (no data)
  tools.approval      request_id, approved (bool), reason?, scope?, tool_pattern?, path_scope?, expires_at?, propagate_to_subagents?

HTTP surfaces:
  GET  /runtime/resources
  GET  /runtime/resources/{resource_id}
  GET  /runtime/capabilities
  POST /runtime/capabilities/invoke

  Server -> Client:
  connected           session_id, user_id, protocol_version
  chat.done           text, request_id?, aborted
  chat.token          text, request_id?
  chat.history        entries[], session_id
  tool.start          tool_name, agent_name, args, metadata?
  tool.result         tool_name, agent_name, ok, result, metadata?
  task.update         task_id, task, timeline_event?
  tools.approval_update  request_id, approval, approval_event
  audit.append        entry
  system.event        source, status, message, run_id?, task_id?, agent_name?
  health.tick         active_sessions, ts
  cron.update         job_id, status, message
  tools.approval_request  request_id, tool_name, agent_name, args, reason, state, scope, tool_pattern, path_scope, expires_at, propagate_to_subagents
  control.approval_request  request_id, plan_id, goal, risk_level, required_capabilities, plan
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional

PROTOCOL_VERSION = 1

# --------------------------------------------------------------------------
# JSON Schema definitions for protocol validation
# --------------------------------------------------------------------------

EVENT_SCHEMAS: dict[str, dict[str, Any]] = {
    # --- Client -> Server ---
    "chat.send": {
        "type": "object",
        "required": ["event", "text"],
        "properties": {
            "event": {"const": "chat.send"},
            "v": {"type": "integer"},
            "text": {"type": "string", "minLength": 1},
            "request_id": {"type": "string"},
        },
    },
    "chat.inject": {
        "type": "object",
        "required": ["event", "text"],
        "properties": {
            "event": {"const": "chat.inject"},
            "v": {"type": "integer"},
            "text": {"type": "string", "minLength": 1},
            "role": {"type": "string", "enum": ["system", "context", "user"]},
            "request_id": {"type": "string"},
        },
    },
    "control.run": {
        "type": "object",
        "required": ["event", "goal"],
        "properties": {
            "event": {"const": "control.run"},
            "v": {"type": "integer"},
            "goal": {"type": "string", "minLength": 1},
            "constraints": {"type": "array"},
            "request_id": {"type": "string"},
        },
    },
    "chat.abort": {
        "type": "object",
        "required": ["event"],
        "properties": {
            "event": {"const": "chat.abort"},
            "v": {"type": "integer"},
            "request_id": {"type": "string"},
        },
    },
    "chat.history": {
        "type": "object",
        "required": ["event"],
        "properties": {
            "event": {"const": "chat.history"},
            "v": {"type": "integer"},
            "session_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            "before": {"type": "number"},
            "request_id": {"type": "string"},
        },
    },
    "presence.ping": {
        "type": "object",
        "required": ["event"],
        "properties": {
            "event": {"const": "presence.ping"},
            "v": {"type": "integer"},
        },
    },
    "tools.approval": {
        "type": "object",
        "required": ["event", "request_id", "approved"],
        "properties": {
            "event": {"const": "tools.approval"},
            "v": {"type": "integer"},
            "request_id": {"type": "string"},
            "approved": {"type": "boolean"},
            "reason": {"type": "string"},
            "scope": {"type": "string", "enum": ["single", "session"]},
            "tool_pattern": {"type": "string"},
            "path_scope": {"type": "string"},
            "expires_at": {"type": "number"},
            "propagate_to_subagents": {"type": "boolean"},
        },
    },
    # --- Server -> Client ---
    "connected": {
        "type": "object",
        "required": ["event", "session_id", "user_id"],
        "properties": {
            "event": {"const": "connected"},
            "v": {"type": "integer"},
            "session_id": {"type": "string"},
            "user_id": {"type": "string"},
            "protocol_version": {"type": "integer"},
            "ts": {"type": "number"},
        },
    },
    "chat.done": {
        "type": "object",
        "required": ["event", "text", "aborted"],
        "properties": {
            "event": {"const": "chat.done"},
            "v": {"type": "integer"},
            "text": {"type": "string"},
            "request_id": {"type": "string"},
            "aborted": {"type": "boolean"},
            "ts": {"type": "number"},
        },
    },
    "chat.token": {
        "type": "object",
        "required": ["event", "text"],
        "properties": {
            "event": {"const": "chat.token"},
            "v": {"type": "integer"},
            "text": {"type": "string"},
            "request_id": {"type": "string"},
            "ts": {"type": "number"},
        },
    },
    "tool.start": {
        "type": "object",
        "required": ["event", "tool_name", "agent_name"],
        "properties": {
            "event": {"const": "tool.start"},
            "v": {"type": "integer"},
            "request_id": {"type": "string"},
            "tool_name": {"type": "string"},
            "agent_name": {"type": "string"},
            "args": {"type": "object"},
            "metadata": {"type": "object"},
            "ts": {"type": "number"},
        },
    },
    "tool.result": {
        "type": "object",
        "required": ["event", "tool_name", "agent_name", "ok"],
        "properties": {
            "event": {"const": "tool.result"},
            "v": {"type": "integer"},
            "request_id": {"type": "string"},
            "tool_name": {"type": "string"},
            "agent_name": {"type": "string"},
            "ok": {"type": "boolean"},
            "result": {"type": "object"},
            "metadata": {"type": "object"},
            "ts": {"type": "number"},
        },
    },
    "task.update": {
        "type": "object",
        "required": ["event", "task_id", "task"],
        "properties": {
            "event": {"const": "task.update"},
            "v": {"type": "integer"},
            "task_id": {"type": "string"},
            "task": {"type": "object"},
            "timeline_event": {"type": "object"},
            "ts": {"type": "number"},
        },
    },
    "tools.approval_update": {
        "type": "object",
        "required": ["event", "request_id", "approval"],
        "properties": {
            "event": {"const": "tools.approval_update"},
            "v": {"type": "integer"},
            "request_id": {"type": "string"},
            "approval": {"type": "object"},
            "approval_event": {"type": "string"},
            "ts": {"type": "number"},
        },
    },
    "audit.append": {
        "type": "object",
        "required": ["event", "entry"],
        "properties": {
            "event": {"const": "audit.append"},
            "v": {"type": "integer"},
            "entry": {"type": "object"},
            "ts": {"type": "number"},
        },
    },
    "health.tick": {
        "type": "object",
        "required": ["event", "active_sessions", "ts"],
        "properties": {
            "event": {"const": "health.tick"},
            "v": {"type": "integer"},
            "active_sessions": {"type": "integer"},
            "ts": {"type": "number"},
        },
    },
    "system.event": {
        "type": "object",
        "required": ["event", "source", "status", "message"],
        "properties": {
            "event": {"const": "system.event"},
            "v": {"type": "integer"},
            "source": {"type": "string"},
            "status": {"type": "string"},
            "message": {"type": "string"},
            "run_id": {"type": "string"},
            "task_id": {"type": "string"},
            "agent_name": {"type": "string"},
            "ts": {"type": "number"},
        },
    },
    "cron.update": {
        "type": "object",
        "required": ["event", "job_id", "status"],
        "properties": {
            "event": {"const": "cron.update"},
            "v": {"type": "integer"},
            "job_id": {"type": "string"},
            "status": {"type": "string"},
            "message": {"type": "string"},
            "ts": {"type": "number"},
        },
    },
    "tools.approval_request": {
        "type": "object",
        "required": ["event", "request_id", "tool_name", "agent_name"],
        "properties": {
            "event": {"const": "tools.approval_request"},
            "v": {"type": "integer"},
            "request_id": {"type": "string"},
            "tool_name": {"type": "string"},
            "agent_name": {"type": "string"},
            "args": {"type": "object"},
            "reason": {"type": "string"},
            "state": {"type": "string", "enum": ["pending", "approved", "denied", "propagated", "expired"]},
            "scope": {"type": "string", "enum": ["single", "session"]},
            "tool_pattern": {"type": "string"},
            "path_scope": {"type": "string"},
            "expires_at": {"type": "number"},
            "propagate_to_subagents": {"type": "boolean"},
            "source_request_id": {"type": "string"},
            "ts": {"type": "number"},
        },
    },
    "control.approval_request": {
        "type": "object",
        "required": ["event", "request_id", "plan_id", "goal", "risk_level"],
        "properties": {
            "event": {"const": "control.approval_request"},
            "v": {"type": "integer"},
            "request_id": {"type": "string"},
            "plan_id": {"type": "string"},
            "goal": {"type": "string"},
            "risk_level": {"type": "string"},
            "required_capabilities": {"type": "array"},
            "plan": {"type": "object"},
            "reason": {"type": "string"},
            "ts": {"type": "number"},
        },
    },
}


HTTP_ROUTE_SCHEMAS: dict[str, dict[str, Any]] = {
    "POST /tasks/supervisors/control-loop": {
        "description": "Start an opt-in long-running supervisor from a goal or first-class mission_contract.",
        "request": {
            "type": "object",
            "anyOf": [
                {"required": ["goal"]},
                {"required": ["mission_contract"]},
            ],
            "properties": {
                "goal": {"type": "string"},
                "mission_contract": {
                    "type": "object",
                    "properties": {
                        "contract_id": {"type": "string"},
                        "objective": {"type": "string"},
                        "allowed_actions": {"type": "array"},
                        "forbidden_actions": {"type": "array"},
                        "abort_conditions": {
                            "type": "array",
                            "items": {
                                "oneOf": [
                                    {"type": "string"},
                                    {
                                        "type": "object",
                                        "required": ["type"],
                                        "properties": {
                                            "type": {"type": "string"},
                                            "reason": {"type": "string"},
                                            "metadata": {"type": "object"},
                                        },
                                    },
                                ]
                            },
                        },
                        "completion_criteria": {"type": "array"},
                        "evidence_requirements": {"type": "array"},
                        "metadata": {"type": "object"},
                    },
                },
                "constraints": {"type": "array"},
                "approved_promotion_artifacts": {
                    "description": "Optional approved promotion artifacts used to record an operator-visible reuse_plan.v1. Selected entries are not applied at runtime.",
                    "oneOf": [{"type": "object"}, {"type": "array"}],
                },
                "duration_seconds": {"type": "integer"},
                "interval_seconds": {"type": "integer"},
                "maintenance_goal": {"type": "string"},
            },
        },
        "response": {
            "type": "object",
            "properties": {
                "accepted": {"type": "boolean"},
                "task": {"type": "object"},
                "control_session_id": {"type": "string"},
                "duration_seconds": {"type": "integer"},
                "interval_seconds": {"type": "integer"},
                "max_iterations": {"type": "integer"},
                "ends_at": {"type": "number"},
                "next_run_at": {"type": "number"},
                "mission_contract": {"type": "object"},
                "reuse_plan": {"type": "object"},
            },
        },
    },
    "POST /tasks/{task_id}/cancel": {
        "description": "Request a graceful stop for a running control supervisor task.",
        "response": {
            "type": "object",
            "properties": {
                "accepted": {"type": "boolean"},
                "task": {"type": "object"},
                "message": {"type": "string"},
            },
        },
    },
    "GET /tasks/{task_id}/timeline": {
        "description": "Return a merged task timeline combining task state changes, approval lifecycle, and related audit events.",
        "query": {
            "page": {"type": "integer"},
            "page_size": {"type": "integer"},
        },
        "response": {
            "type": "object",
            "properties": {
                "task": {"type": "object"},
                "entries": {"type": "array"},
                "pagination": {"type": "object"},
            },
        },
    },
    "GET /runtime/resources": {
        "description": "List runtime resources such as bridge surfaces and loaded skills.",
        "response": {
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
                "resources": {"type": "array"},
            },
        },
    },
    "GET /runtime/resources/{resource_id}": {
        "description": "Read a runtime resource by id (for example skill:<name> or bridge:host).",
        "query": {"refresh": {"type": "boolean"}},
        "response": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "resource": {"type": "object"},
                "message": {"type": "string"},
            },
        },
    },
    "GET /runtime/capabilities": {
        "description": "List canonical runtime capabilities across skills, browser, current_tab, desktop, and host surfaces.",
        "query": {"refresh": {"type": "boolean"}},
        "response": {
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
                "refresh": {"type": "boolean"},
                "capabilities": {"type": "array"},
            },
        },
    },
    "POST /runtime/capabilities/invoke": {
        "description": "Invoke a canonical runtime capability by dot name with JSON params.",
        "request": {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string"},
                "params": {"type": "object"},
            },
        },
        "response": {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "capability": {"type": "string"},
                "provider": {"type": "string"},
                "transport": {"type": "string"},
                "result": {"type": "object"},
                "error": {"type": "string"},
            },
        },
    },
    "POST /px4-gazebo/mission-scenarios/propose": {
        "description": "Convert an operator prompt into a review-only PX4/Gazebo mission scenario proposal. This endpoint does not execute Gazebo or dispatch commands.",
        "request": {
            "type": "object",
            "required": ["prompt"],
            "properties": {
                "prompt": {"type": "string"},
            },
        },
        "response": {
            "type": "object",
            "properties": {
                "prompt_request": {"type": "object"},
                "scenario_proposal": {"type": "object"},
                "validation_result": {"type": "object"},
                "dry_run_result": {"type": "object"},
                "real_world_mission_target": {"type": "object"},
                "real_world_geocode_candidate": {"type": "object"},
                "terrain_dem_tile_request_candidate": {"type": "object"},
                "terrain_dem_tile_snapshot": {"type": "object"},
                "tile_backed_terrain_environment_snapshot": {"type": "object"},
                "terrain_heightmap_candidate": {"type": "object"},
                "terrain_heightmap_artifact": {"type": "object"},
                "terrain_heightmap_file_artifact": {"type": "object"},
                "gazebo_world_candidate": {"type": "object"},
                "gazebo_world_artifact": {"type": "object"},
                "coordinate_transform_candidate": {"type": "object"},
                "digital_twin_mission_anchor_candidate": {"type": "object"},
                "digital_twin_px4_mission_item_candidate": {"type": "object"},
                "digital_twin_sitl_binding_gate": {"type": "object"},
                "terrain_environment_snapshot": {"type": "object"},
                "weather_environment_snapshot": {"type": "object"},
                "digital_twin_route_feasibility": {"type": "object"},
                "weather_environment_policy_gate": {"type": "object"},
                "digital_twin_route_plan": {"type": "object"},
                "summary": {"type": "object"},
            },
        },
    },
    "POST /px4-gazebo/mission-scenarios/prepare-sitl-execution": {
        "description": "Persist a prepared PX4/Gazebo SITL execution request from an approved Mission Designer bounded request. This endpoint does not execute Gazebo, dispatch MAVLink, or upload a mission.",
        "request": {
            "type": "object",
            "required": [
                "scenario_proposal",
                "validation_result",
                "scenario_approval",
                "scenario_compile_result",
                "bounded_simulation_request",
            ],
            "properties": {
                "scenario_proposal": {"type": "object"},
                "validation_result": {"type": "object"},
                "scenario_approval": {"type": "object"},
                "scenario_compile_result": {"type": "object"},
                "bounded_simulation_request": {"type": "object"},
                "owner_session_id": {"type": "string"},
                "owner_user_id": {"type": "string"},
                "parent_task_id": {"type": "string"},
            },
        },
        "response": {
            "type": "object",
            "properties": {
                "sitl_execution_request": {"type": "object"},
                "task": {"type": "object"},
                "summary": {"type": "object"},
            },
        },
    },
    "POST /px4-gazebo/mission-scenarios/execute-sitl": {
        "description": "Run a prepared Mission Designer SITL execution request through the existing PX4/Gazebo SITL mission upload machinery. Optional live_flight_mode additionally requires RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_EXECUTION=1 and RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_LIVE_FLIGHT=1, then persists a live flight artifact; without the relevant opt-ins the task receives a blocked receipt and no dispatch occurs.",
        "request": {
            "type": "object",
            "required": ["task_id", "explicit_execution_approval"],
            "properties": {
                "task_id": {"type": "string"},
                "explicit_execution_approval": {"type": "boolean"},
                "live_flight_mode": {"type": "boolean"},
            },
        },
        "response": {
            "type": "object",
            "properties": {
                "sitl_execution_opted_in": {"type": "boolean"},
                "live_flight_mode_requested": {"type": "boolean"},
                "live_flight_opted_in": {"type": "boolean"},
                "task": {"type": "object"},
                "delivery_mission_contract": {"type": "object"},
                "simulated_command_proposal": {"type": "object"},
                "simulated_command_approval": {"type": "object"},
                "simulator_command_execution_preflight": {"type": "object"},
                "px4_gazebo_sitl_mission_upload_receipt": {"type": "object"},
                "px4_gazebo_mission_designer_sitl_execution_result": {
                    "type": "object"
                },
                "px4_gazebo_mission_designer_sitl_live_flight_run": {
                    "type": "object"
                },
                "px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt": {
                    "type": "object"
                },
                "environment_condition_profile": {"type": "object"},
                "simulator_capability_matrix": {"type": "object"},
                "simulator_condition_application": {"type": "object"},
                "observed_environment_evidence": {"type": "object"},
                "scenario_cleanup_receipt": {"type": "object"},
                "vehicle_condition_profile": {"type": "object"},
                "payload_simulator_capability_matrix": {"type": "object"},
                "payload_simulator_condition_application": {"type": "object"},
                "observed_vehicle_condition_evidence": {"type": "object"},
                "summary": {"type": "object"},
            },
        },
    },
}

RUNTIME_SUBSTRATE_SCHEMA: dict[str, Any] = {
    "resources": {
        "list_route": "GET /runtime/resources",
        "read_route": "GET /runtime/resources/{resource_id}",
        "resource_kinds": ["bridge", "skill"],
    },
    "capabilities": {
        "list_route": "GET /runtime/capabilities",
        "invoke_route": "POST /runtime/capabilities/invoke",
        "canonical_prefixes": [
            "skill",
            "shell",
            "file",
            "browser",
            "control_ui_chat",
            "current_tab",
            "desktop",
        ],
    },
}


def _type_matches(value: Any, expected_type: str) -> bool:
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return (
            (isinstance(value, int) and not isinstance(value, bool))
            or isinstance(value, float)
        )
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    return True


def validate_event(data: Any, schema: Optional[dict[str, Any]]) -> list[str]:
    """Validate a payload against a minimal JSON-schema subset.

    The protocol schemas are intentionally simple, so a lightweight validator is
    enough here and avoids adding a runtime dependency.
    """
    if not isinstance(data, dict):
        return ["payload must be a JSON object"]
    if schema is None:
        return [f"unknown event: {data.get('event', '')!r}"]

    errors: list[str] = []
    if schema.get("type") == "object" and not isinstance(data, dict):
        return ["payload must be a JSON object"]

    for key in schema.get("required", []):
        if key not in data:
            errors.append(f"missing required field: {key}")

    properties = schema.get("properties", {})
    for key, value in data.items():
        prop = properties.get(key)
        if prop is None:
            continue

        expected_type = prop.get("type")
        if expected_type and not _type_matches(value, expected_type):
            errors.append(
                f"field {key!r} must be of type {expected_type}, "
                f"got {type(value).__name__}"
            )
            continue

        if "const" in prop and value != prop["const"]:
            errors.append(f"field {key!r} must equal {prop['const']!r}")
        if "enum" in prop and value not in prop["enum"]:
            errors.append(
                f"field {key!r} must be one of {', '.join(map(repr, prop['enum']))}"
            )
        if expected_type == "string" and "minLength" in prop and len(value) < prop["minLength"]:
            errors.append(
                f"field {key!r} must be at least {prop['minLength']} characters"
            )
        if expected_type in {"integer", "number"}:
            if "minimum" in prop and value < prop["minimum"]:
                errors.append(f"field {key!r} must be >= {prop['minimum']}")
            if "maximum" in prop and value > prop["maximum"]:
                errors.append(f"field {key!r} must be <= {prop['maximum']}")

    return errors


def validate_client_event(data: Any) -> list[str]:
    """Validate an incoming client envelope."""
    if not isinstance(data, dict):
        return ["payload must be a JSON object"]
    event_name = data.get("event", "")
    return validate_event(data, EVENT_SCHEMAS.get(event_name))


def make_request_id() -> str:
    return uuid.uuid4().hex[:12]


def _base(event: str, request_id: Optional[str] = None) -> dict[str, Any]:
    d: dict[str, Any] = {"v": PROTOCOL_VERSION, "event": event, "ts": time.time()}
    if request_id:
        d["request_id"] = request_id
    return d


# --------------------------------------------------------------------------
# Server -> Client event builders
# --------------------------------------------------------------------------

def ev_connected(session_id: str, user_id: str) -> dict[str, Any]:
    d = _base("connected")
    d["session_id"] = session_id
    d["user_id"] = user_id
    d["protocol_version"] = PROTOCOL_VERSION
    return d


def ev_chat_done(
    text: str,
    request_id: Optional[str] = None,
    aborted: bool = False,
) -> dict[str, Any]:
    d = _base("chat.done", request_id)
    d["text"] = text
    d["aborted"] = aborted
    return d


def ev_chat_token(text: str, request_id: Optional[str] = None) -> dict[str, Any]:
    d = _base("chat.token", request_id)
    d["text"] = text
    return d


def ev_tool_start(
    tool_name: str,
    agent_name: str,
    args: Optional[dict[str, Any]] = None,
    request_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    d = _base("tool.start", request_id)
    d["tool_name"] = tool_name
    d["agent_name"] = agent_name
    d["args"] = args or {}
    if metadata:
        d["metadata"] = metadata
    return d


def ev_tool_result(
    tool_name: str,
    agent_name: str,
    ok: bool,
    result: Optional[dict[str, Any]] = None,
    request_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    d = _base("tool.result", request_id)
    d["tool_name"] = tool_name
    d["agent_name"] = agent_name
    d["ok"] = ok
    d["result"] = result or {}
    if metadata:
        d["metadata"] = metadata
    return d


def ev_task_update(
    task: dict[str, Any],
    timeline_event: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    d = _base("task.update")
    d["task_id"] = str(task.get("task_id") or "")
    d["task"] = task
    if timeline_event:
        d["timeline_event"] = timeline_event
    return d


def ev_tools_approval_update(
    approval: dict[str, Any],
    *,
    approval_event: Optional[str] = None,
) -> dict[str, Any]:
    d = _base("tools.approval_update", str(approval.get("request_id") or ""))
    d["request_id"] = str(approval.get("request_id") or "")
    d["approval"] = approval
    if approval_event:
        d["approval_event"] = approval_event
    return d


def ev_audit_append(entry: dict[str, Any]) -> dict[str, Any]:
    d = _base("audit.append")
    d["entry"] = entry
    return d


def ev_chat_history(
    entries: list[dict[str, Any]],
    session_id: str,
    request_id: Optional[str] = None,
) -> dict[str, Any]:
    d = _base("chat.history", request_id)
    d["session_id"] = session_id
    d["entries"] = entries
    return d


def ev_system_event(
    source: str,
    status: str,
    message: str,
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
    agent_name: Optional[str] = None,
) -> dict[str, Any]:
    d = _base("system.event")
    d["source"] = source
    d["status"] = status
    d["message"] = message
    if run_id:
        d["run_id"] = run_id
    if task_id:
        d["task_id"] = task_id
    if agent_name:
        d["agent_name"] = agent_name
    return d


def ev_health_tick(active_sessions: int) -> dict[str, Any]:
    d = _base("health.tick")
    d["active_sessions"] = active_sessions
    return d


def ev_cron_update(job_id: str, status: str, message: str = "") -> dict[str, Any]:
    d = _base("cron.update")
    d["job_id"] = job_id
    d["status"] = status
    d["message"] = message
    return d


def ev_tools_approval_request(
    request_id: str,
    tool_name: str,
    agent_name: str,
    args: Optional[dict[str, Any]] = None,
    reason: str = "",
    state: str = "pending",
    scope: str = "single",
    tool_pattern: Optional[str] = None,
    path_scope: Optional[str] = None,
    expires_at: Optional[float] = None,
    propagate_to_subagents: bool = False,
    source_request_id: Optional[str] = None,
) -> dict[str, Any]:
    d = _base("tools.approval_request", request_id)
    d["tool_name"] = tool_name
    d["agent_name"] = agent_name
    d["args"] = args or {}
    d["reason"] = reason
    d["state"] = state
    d["scope"] = scope
    d["tool_pattern"] = tool_pattern or tool_name
    if path_scope:
        d["path_scope"] = path_scope
    if expires_at is not None:
        d["expires_at"] = expires_at
    d["propagate_to_subagents"] = propagate_to_subagents
    if source_request_id:
        d["source_request_id"] = source_request_id
    return d


def ev_control_approval_request(
    request_id: str,
    plan_id: str,
    goal: str,
    risk_level: str,
    required_capabilities: Optional[list[str]] = None,
    plan: Optional[dict[str, Any]] = None,
    reason: str = "",
) -> dict[str, Any]:
    d = _base("control.approval_request", request_id)
    d["plan_id"] = plan_id
    d["goal"] = goal
    d["risk_level"] = risk_level
    d["required_capabilities"] = required_capabilities or []
    d["plan"] = plan or {}
    d["reason"] = reason
    return d


# --------------------------------------------------------------------------
# Parsing / normalization
# --------------------------------------------------------------------------

def normalize_client_event(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize incoming client message to v1 envelope.

    Supports legacy format (type=message, message=...) as well as v1 format.
    """
    event = data.get("event") or data.get("type", "")

    # Legacy: type=message -> chat.send
    if event == "message":
        event = "chat.send"

    # Legacy: "message" field -> "text" field
    if "text" not in data and "message" in data:
        data["text"] = data["message"]

    # Legacy: type=ping -> presence.ping
    if event == "ping":
        event = "presence.ping"

    data["event"] = event
    if "v" not in data:
        data["v"] = PROTOCOL_VERSION

    return data


def get_schema(event_name: str) -> Optional[dict[str, Any]]:
    """Return JSON Schema for a given event name, or None."""
    return EVENT_SCHEMAS.get(event_name)
