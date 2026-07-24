"""MissionOS operator CLI backed by the Gateway HTTP and WebSocket routes."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote
import hashlib
import json
import math
import re
import secrets
import shlex
import subprocess as subprocess
import sys
import time

import click
import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markup import escape as rich_escape
from rich.panel import Panel
from rich.table import Table

from .chat_companions import (
    CHAT_COMPANION_TERMINAL_ROOT as CHAT_COMPANION_TERMINAL_ROOT,
    CHAT_COMPANION_TERMINAL_SURFACES as CHAT_COMPANION_TERMINAL_SURFACES,
    TERMINAL_TASK_STATUSES as TERMINAL_TASK_STATUSES,
    TURTLEBOT3_CHAT_TASK_STATUS_POLL_INTERVAL as TURTLEBOT3_CHAT_TASK_STATUS_POLL_INTERVAL,
    _chat_companion_terminal_script as _chat_companion_terminal_script,
    _chat_companion_terminals_enabled as _chat_companion_terminals_enabled,
    _close_macos_companion_terminal_titles as _close_macos_companion_terminal_titles,
    _ensure_chat_companion_terminals_impl,
    _listed_home_robot_task_ids as _listed_home_robot_task_ids_impl,
    _launch_macos_terminal_script as _launch_macos_terminal_script,
    _maybe_open_home_robot_companion_terminals_impl,
    _maybe_start_home_robot_chat_task_status_monitor_impl,
    _missionos_chat_companion_command_prefix as _missionos_chat_companion_command_prefix,
    _print_turtlebot3_chat_task_terminal_update as _print_turtlebot3_chat_task_terminal_update,
    _safe_chat_companion_slug as _safe_chat_companion_slug,
    _run_home_robot_conversation_with_companion_monitor_impl,
    _start_turtlebot3_chat_task_status_monitor_impl,
    _stop_chat_companion_terminals_impl,
    _stop_turtlebot3_chat_task_status_monitor as _stop_turtlebot3_chat_task_status_monitor,
)
from .chat_interaction import (
    CHAT_HELP_LINES as CHAT_HELP_LINES,
    _chat_back_available as _chat_back_available,
    _chat_back_stack as _chat_back_stack,
    _chat_help_panel as _chat_help_panel,
    _chat_prompt_fragment as _chat_prompt_fragment,
    _chat_state_snapshot as _chat_state_snapshot,
    _chat_suggestion as _chat_suggestion,
    _clear_chat_back_stack as _clear_chat_back_stack,
    _clear_chat_suggestion as _clear_chat_suggestion,
    _is_chat_back_request as _is_chat_back_request,
    _looks_like_mission_planning_request as _looks_like_mission_planning_request,
    _natural_language_recovery_request as _natural_language_recovery_request,
    _normalize_recovery_natural_language as _normalize_recovery_natural_language,
    _print_chat_followup as _print_chat_followup,
    _print_recovery_agent_request_proposal as _print_recovery_agent_request_proposal,
    _push_chat_back_state as _push_chat_back_state,
    _recovery_command_number as _recovery_command_number,
    _recovery_natural_language_number as _recovery_natural_language_number,
    _recovery_natural_language_xy as _recovery_natural_language_xy,
    _recovery_proposal_command as _recovery_proposal_command,
    _recovery_proposal_summary as _recovery_proposal_summary,
    _restore_chat_back_state as _restore_chat_back_state,
    _set_chat_suggestion as _set_chat_suggestion,
)
from .chat_state import (
    _load_coordinate_route_file as _load_coordinate_route_file,
    _load_json_object as _load_json_object,
    _load_state as _load_state,
    _mission_designer_context_ref as _mission_designer_context_ref,
    _mission_designer_payload as _mission_designer_payload,
    _mission_designer_sitl_task_id as _mission_designer_sitl_task_id,
    _payload_task_id as _payload_task_id,
    _remember_mission_designer_context as _remember_mission_designer_context,
    _remember_sitl_task_id as _remember_sitl_task_id,
    _remember_sitl_task_id_from_payload as _remember_sitl_task_id_from_payload,
    _save_state as _save_state,
    _stored_mission_designer_context as _stored_mission_designer_context,
    _stored_sitl_task_id as _stored_sitl_task_id,
)
from .console_output import (
    _print_conversation_result as _print_conversation_result,
    _print_job_status as _print_job_status,
    _print_json as _print_json,
    _print_recovery_result as _print_recovery_result,
    _print_sitl_execution_result as _print_sitl_execution_result,
    _print_sitl_start_result as _print_sitl_start_result,
    _print_status as _print_status,
    _recovery_runner_observation_lines as _recovery_runner_observation_lines,
    _safe_get as _safe_get,
)
from .flight_map_html import (
    _json_for_html_script as _json_for_html_script,
    _mission_map_html as _mission_map_html,
)
from .gateway_client import (
    SITL_DISPATCH_TIMEOUT as SITL_DISPATCH_TIMEOUT,
    SITL_EXECUTION_APPROVAL_ROUTE as SITL_EXECUTION_APPROVAL_ROUTE,
    MissionOSGatewayClient,
    _join_url,
)
from .gateway_process import (
    GATEWAY_PID_RECORD_SCHEMA_VERSION as GATEWAY_PID_RECORD_SCHEMA_VERSION,
    _apply_gateway_llm_env as _apply_gateway_llm_env,
    _build_gateway_pid_record as _build_gateway_pid_record,
    _dotenv_process_values as _dotenv_process_values,
    _gateway_argv as _gateway_argv,
    _gateway_command_signature as _gateway_command_signature,
    _gateway_pid_record_matches_running_process as _gateway_pid_record_matches_running_process,
    _gateway_process_env as _gateway_process_env,
    _llm_backend_default_adk_enabled as _llm_backend_default_adk_enabled,
    _llm_backend_from_env as _llm_backend_from_env,
    _llm_backend_uses_google_credentials as _llm_backend_uses_google_credentials,
    _process_command as _process_command,
    _process_group_id as _process_group_id,
    _process_running as _process_running,
    _process_start_time as _process_start_time,
    _read_gateway_pid as _read_gateway_pid,
    _read_gateway_pid_record as _read_gateway_pid_record,
    _stop_gateway_pid as _stop_gateway_pid,
)
from .gateway_runtime import (
    _ensure_gateway as _ensure_gateway,
    _gateway_health_payload as _gateway_health_payload,
    _gateway_is_fixture_backend as _gateway_is_fixture_backend,
    _gateway_reachable as _gateway_reachable,
    _spawn_gateway as _spawn_gateway,
    _start_managed_gateway as _start_managed_gateway,
    _terminate_gateway as _terminate_gateway,
    make_client as make_client,
)
from .indoor_map_html import (
    _mission_indoor_map_html as _mission_indoor_map_html,
)
from .job_status import (
    _artifacts_with_latest_runtime_snapshot as _artifacts_with_latest_runtime_snapshot,
    _as_bool as _as_bool,
    _as_float as _as_float,
    _as_int as _as_int,
    _auto_process_status_text as _auto_process_status_text,
    _battery_display_text as _battery_display_text,
    _first_numeric as _first_numeric,
    _first_present as _first_present,
    _fmt_metres as _fmt_metres,
    _fmt_signed_metres as _fmt_signed_metres,
    _format_degrees as _format_degrees,
    _format_distance as _format_distance,
    _format_duration as _format_duration,
    _format_flag as _format_flag,
    _format_hpa as _format_hpa,
    _format_mm_per_hour as _format_mm_per_hour,
    _format_mps as _format_mps,
    _format_percent as _format_percent,
    _format_temperature_c as _format_temperature_c,
    _job_eta_seconds as _job_eta_seconds,
    _job_operator_summary as _job_operator_summary,
    _job_progress_percent as _job_progress_percent,
    _job_realism_condition_text as _job_realism_condition_text,
    _job_route_distance_m as _job_route_distance_m,
    _job_weather_compact_text as _job_weather_compact_text,
    _job_weather_condition_text as _job_weather_condition_text,
    _operate_altitude_text as _operate_altitude_text,
    _operator_recovery_ack_text as _operator_recovery_ack_text,
    _operator_recovery_assist_status_text as _operator_recovery_assist_status_text,
    _operator_recovery_cli_command as _operator_recovery_cli_command,
    _operator_recovery_console_command as _operator_recovery_console_command,
    _operator_recovery_dispatch_command as _operator_recovery_dispatch_command,
    _operator_recovery_dispatch_hint as _operator_recovery_dispatch_hint,
    _operator_recovery_dispatch_status_text as _operator_recovery_dispatch_status_text,
    _operator_recovery_maneuver_evidence_snapshot as _operator_recovery_maneuver_evidence_snapshot,
    _progress_bar as _progress_bar,
    _recovery_parameter_text as _recovery_parameter_text,
    _runtime_recovery_agent_action as _runtime_recovery_agent_action,
    _runtime_recovery_agent_parameters as _runtime_recovery_agent_parameters,
    _runtime_recovery_effective_status as _runtime_recovery_effective_status,
    _runtime_snapshot_with_latest_file as _runtime_snapshot_with_latest_file,
    _task_artifacts as _task_artifacts,
    _task_record as _task_record,
    _task_status as _task_status,
    _terrain_profile_samples_for_watch as _terrain_profile_samples_for_watch,
    _timeline_detail_text as _timeline_detail_text,
    _timeline_events as _timeline_events,
    _timeline_time_text as _timeline_time_text,
)
from .map_model import (
    MISSION_MAP_POLL_INTERVAL as MISSION_MAP_POLL_INTERVAL,
    MISSION_MAP_PROVIDERS as MISSION_MAP_PROVIDERS,
    _FLIGHT_MAP_TRAIL_LIMIT as _FLIGHT_MAP_TRAIL_LIMIT,
    _dropoff_ned_from_route as _dropoff_ned_from_route,
    _mission_command_label as _mission_command_label,
    _mission_indoor_map_model as _mission_indoor_map_model,
    _mission_map_flight_samples as _mission_map_flight_samples,
    _mission_map_latlon_from_route as _mission_map_latlon_from_route,
    _mission_map_latlon_to_local as _mission_map_latlon_to_local,
    _mission_map_local_to_latlon as _mission_map_local_to_latlon,
    _mission_map_maneuver as _mission_map_maneuver,
    _mission_map_model as _mission_map_model,
    _mission_map_obstacles as _mission_map_obstacles,
    _mission_map_planned_points as _mission_map_planned_points,
    _mission_map_sample_latlon as _mission_map_sample_latlon,
    _mission_map_telemetry_model as _mission_map_telemetry_model,
    _mission_map_weather_model as _mission_map_weather_model,
    _mission_obstacle_records_from_artifacts as _mission_obstacle_records_from_artifacts,
    _normalize_turtlebot_robot_profile as _normalize_turtlebot_robot_profile,
    _operator_recovery_local_maneuver_model as _operator_recovery_local_maneuver_model,
    _overlay_turtlebot3_live_telemetry as _overlay_turtlebot3_live_telemetry,
    _project_flight_points as _project_flight_points,
    _repair_turtlebot3_indoor_map_display_alignment as _repair_turtlebot3_indoor_map_display_alignment,
    _repair_turtlebot3_indoor_map_points as _repair_turtlebot3_indoor_map_points,
    _turtlebot3_indoor_map_dropoff_xy as _turtlebot3_indoor_map_dropoff_xy,
    _turtlebot3_indoor_map_model_from_artifacts as _turtlebot3_indoor_map_model_from_artifacts,
    _turtlebot3_recovery_candidate_resolution_from_artifacts as _turtlebot3_recovery_candidate_resolution_from_artifacts,
    _turtlebot3_xy as _turtlebot3_xy,
    _turtlebot_robot_label_from_artifacts as _turtlebot_robot_label_from_artifacts,
    _turtlebot_robot_label_from_profile as _turtlebot_robot_label_from_profile,
    _turtlebot_robot_profile_from_artifacts as _turtlebot_robot_profile_from_artifacts,
)
from .map_runtime import (
    FLIGHT_MAP_POLL_INTERVAL as FLIGHT_MAP_POLL_INTERVAL,
    MISSION_MAP_OUTPUT_DIR as MISSION_MAP_OUTPUT_DIR,
    _serve_authenticated_live_mission_map as _serve_authenticated_live_mission_map,
    _watch_flight_map as _watch_flight_map,
    _write_mission_map_html as _write_mission_map_html,
    _write_terminal_route_evidence as _write_terminal_route_evidence,
)
from .map_terminal import (
    FLIGHT_MAP_HEIGHT as FLIGHT_MAP_HEIGHT,
    FLIGHT_MAP_WIDTH as FLIGHT_MAP_WIDTH,
    FLIGHT_PROFILE_HEIGHT as FLIGHT_PROFILE_HEIGHT,
    TURTLEBOT3_MAP_ICON as TURTLEBOT3_MAP_ICON,
    _indoor_xy_points as _indoor_xy_points,
    _interpolate_watch_profile_value as _interpolate_watch_profile_value,
    _project_indoor_xy_points as _project_indoor_xy_points,
    _render_elevation_profile as _render_elevation_profile,
    _render_flight_map as _render_flight_map,
    _render_turtlebot3_indoor_map as _render_turtlebot3_indoor_map,
    _watch_altitude_status as _watch_altitude_status,
    _watch_overlay_status_text as _watch_overlay_status_text,
    _watch_planned_route_points as _watch_planned_route_points,
    _watch_process_status as _watch_process_status,
)
from .play_command import (
    PLAY_STATUS_STYLE as PLAY_STATUS_STYLE,
    _play_apply_command as _play_apply_command,
    _play_exposure_style as _play_exposure_style,
    _play_help_panel as _play_help_panel,
    _play_plan_table as _play_plan_table,
    _render_play_compare as _render_play_compare,
    _render_play_flight_result as _render_play_flight_result,
    _render_play_plan as _render_play_plan,
    _render_play_weather as _render_play_weather,
    run_play_command,
)
from .tutorial_runtime import (
    TutorialOutcome as TutorialOutcome,
    TutorialReader as TutorialReader,
    TutorialStep as TutorialStep,
    _print_tutorial_step as _print_tutorial_step,
    build_tutorial_steps as _build_tutorial_steps_impl,
    run_tutorial_steps,
)
from .turtlebot3_runtime import (
    DEFAULT_TURTLEBOT3_CHAT_INSTRUCTION as DEFAULT_TURTLEBOT3_CHAT_INSTRUCTION,
    TURTLEBOT3_CHAT_TIMEOUT as TURTLEBOT3_CHAT_TIMEOUT,
    _find_repo_root_for_turtlebot3_smoke as _find_repo_root_for_turtlebot3_smoke,
    _floor_turtlebot3_chat_timeout as _floor_turtlebot3_chat_timeout,
    _run_turtlebot3_chat_smoke as _run_turtlebot3_chat_smoke,
    _start_turtlebot3_gateway_container as _start_turtlebot3_gateway_container,
    _stop_turtlebot3_gateway_container as _stop_turtlebot3_gateway_container,
    _turtlebot3_gateway_container_name as _turtlebot3_gateway_container_name,
    _turtlebot3_gateway_start_script as _turtlebot3_gateway_start_script,
)
from . import turtlebot3_runtime as _turtlebot3_runtime
from .operate_commands import (
    _OPERATE_CONSOLE_COMMANDS as _OPERATE_CONSOLE_COMMANDS,
    _OPERATE_PARAMETER_ALIASES as _OPERATE_PARAMETER_ALIASES,
    _OPERATE_RECOVERY_ACTION_ALIASES as _OPERATE_RECOVERY_ACTION_ALIASES,
    PROPOSAL_REDISPLAY_SECONDS as PROPOSAL_REDISPLAY_SECONDS,
    OperateConsoleCommand as OperateConsoleCommand,
    ProposalGate as ProposalGate,
    _RECOVERY_RISK_LABELS as _RECOVERY_RISK_LABELS,
    _build_operate_session as _build_operate_session,
    _float_operate_argument as _float_operate_argument,
    _humanize_risks as _humanize_risks,
    _OPERATOR_RECOVERY_ACTIONS as _OPERATOR_RECOVERY_ACTIONS,
    _normalize_operate_parameter_key as _normalize_operate_parameter_key,
    _operate_console_help_panel as _operate_console_help_panel,
    _parse_operate_console_command as _parse_operate_console_command,
    _parse_operate_console_parameters as _parse_operate_console_parameters,
    _proposal_signature as _proposal_signature,
    _render_action_panel as _render_action_panel,
)
from .operate_tasks import (
    _is_home_robot_nav2_execution_target as _is_home_robot_nav2_execution_target,
    _is_real_mission_designer_sitl_task as _is_real_mission_designer_sitl_task,
    _is_turtlebot3_task_artifacts as _is_turtlebot3_task_artifacts,
    _latest_running_sitl_task_id as _latest_running_sitl_task_id,
    _resolve_live_task_id as _resolve_live_task_id,
    _resolve_operator_recovery_task_id as _resolve_operator_recovery_task_id,
    _task_has_active_auto_runner_request_path as _task_has_active_auto_runner_request_path,
)
from .operate_runtime import run_operate_console
from .operate_view import (
    _build_operate_status_group as _build_operate_status_group_view,
    _humanize_recovery_summary as _humanize_recovery_summary,
    _operate_robot_from_task_payload as _operate_robot_from_task_payload,
    _render_operate_status_line as _render_operate_status_line,
    _render_recovery_agent_console as _render_recovery_agent_console_view,
)


SITL_EXECUTION_POLL_INTERVAL = 5.0
SITL_EXECUTION_POLL_TIMELINE_LIMIT = 5
ACTIVE_RUNNER_RECOVERY_OBSERVATION_TIMEOUT_SECONDS = 95.0
LIVE_SITL_RESPONSE_WAIT_EXCEEDED_MESSAGE = (
    "Execute Live SITL Gateway response exceeded the client wait window; "
    "showing observed task state."
)

DEFAULT_GATEWAY_URL = "http://127.0.0.1:18791"
DEFAULT_SESSION_ID = "missionos-cli"
DEFAULT_STATE_PATH = "data/missionos_cli_state.json"
DEFAULT_HISTORY_PATH = "data/missionos_cli_history"
DEFAULT_OPERATE_HISTORY_PATH = "data/missionos_operate_history"
DEFAULT_GATEWAY_PID_PATH = Path("data/missionos_gateway.pid")
DEFAULT_GATEWAY_LOG_PATH = Path("data/missionos_gateway.log")
DEFAULT_TURTLEBOT4_CHAT_INSTRUCTION = (
    "TurtleBot4で屋内配送ルートを走って。障害物を避けて、目的地まで届けて。"
)
DEFAULT_NOVA_CARTER_CHAT_INSTRUCTION = (
    "Nova CarterでIsaac Sim内の短いNav2ルートを走って。"
    "承認、dispatch、ACK、odom evidenceの境界を保って。"
)
CHAT_SLASH_COMMANDS = (
    "/status",
    "/approve",
    "/reject",
    "/revision",
    "/run",
    "/repair",
    "/start-sitl",
    "/execute-sitl",
    "/job-status",
    "/map",
    "/land",
    "/rtl",
    "/review-recovery",
    "/approve-recovery",
    "/climb",
    "/speed",
    "/reroute",
    "/avoid",
    "/avoid-obstacle",
    "/back",
    "/help",
    "/clear",
    "/quit",
)
INTENT_INSTRUCTIONS = {
    "approve": "Approve the current MissionOS plan.",
    "reject": "Reject the current MissionOS plan.",
    "revision": "Revise the current MissionOS plan.",
    "run": "Run the current bounded action through the MissionOS execution gate.",
    "repair": "Diagnose the current MissionOS plan and draft a repair.",
}
INTENT_ROUTE_HINTS = {
    "approve": "approve",
    "reject": "reject",
    "revision": "revision",
    "run": "execute",
    "repair": "repair",
}

# Bundled Mt. Fuji delivery coordinate route used by `missionos tutorial`.
# Same values as docs/mission_os/fuji_delivery_route.yaml, embedded so the
# tutorial does not depend on the current working directory.
FUJI_DELIVERY_ROUTE: dict[str, Any] = {
    "takeoff_latitude": 35.3195,
    "takeoff_longitude": 138.7435,
    "dropoff_latitude": 35.3606,
    "dropoff_longitude": 138.7274,
    "dropoff_roof_height_agl_m": 10,
    "payload_weight_kg": 1,
    "wind_speed_mps": 8,
    "wind_direction_deg": 0,
}
TUTORIAL_PLAN_INSTRUCTION = (
    "Plan the Mt. Fuji delivery. Use the Mt. Fuji coordinate route and prepare "
    "it through payload delivery SITL readiness."
)
DEFAULT_TUTORIAL_SESSION_ID = "missionos-cli-tutorial"

console = Console()


def _status_text(value: Any, default: str = "-") -> str:
    if value is None or value == "":
        return default
    return str(value)


def _wait_for_active_runner_recovery_observation(
    client: MissionOSGatewayClient,
    payload: dict[str, Any],
    *,
    timeout_seconds: float = ACTIVE_RUNNER_RECOVERY_OBSERVATION_TIMEOUT_SECONDS,
    poll_interval: float = 0.5,
) -> dict[str, Any] | None:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    if summary.get("active_runner_request_queued") is not True:
        return None
    task_id = _payload_task_id(payload)
    if not task_id:
        return None
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    last_payload: dict[str, Any] | None = None
    recovery_action = str(summary.get("recovery_action") or "").strip().lower()
    maneuver_actions = {
        "adjust_altitude",
        "adjust_speed",
        "reroute",
        "avoid_obstacle",
        "calibrate_offboard",
    }
    expected_parameters = (
        summary.get("recovery_parameters")
        if isinstance(summary.get("recovery_parameters"), dict)
        else {}
    )

    def _parameters_match(observed: Any) -> bool:
        if not expected_parameters:
            return True
        if not isinstance(observed, dict):
            return False
        for key, expected_value in expected_parameters.items():
            if key not in observed:
                return False
            observed_value = observed.get(key)
            if isinstance(expected_value, bool) or isinstance(observed_value, bool):
                if bool(expected_value) != bool(observed_value):
                    return False
                continue
            expected_number = _as_float(expected_value)
            observed_number = _as_float(observed_value)
            if expected_number is not None and observed_number is not None:
                if abs(expected_number - observed_number) > 1e-3:
                    return False
                continue
            if str(expected_value) != str(observed_value):
                return False
        return True

    while time.monotonic() <= deadline:
        try:
            task_payload = client.get(f"/tasks/{quote(task_id, safe='')}")
        except click.ClickException:
            return last_payload
        last_payload = task_payload
        snapshot = _task_artifacts(task_payload).get("missionos_auto_mission_runtime_snapshot")
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        outcome = str(snapshot.get("post_abort_outcome_status") or "")
        if outcome and outcome not in {
            "recovery_outcome_pending",
            "return_observation_pending",
            "landing_observation_pending",
        }:
            return task_payload
        if snapshot.get("operator_recovery_command_ack_observed") is False:
            return task_payload
        request_matches = snapshot.get(
            "operator_recovery_request_observed"
        ) is True and _parameters_match(snapshot.get("operator_recovery_parameters"))
        if (
            recovery_action in maneuver_actions
            and request_matches
            and (
                snapshot.get("operator_recovery_assist_status") is not None
                or snapshot.get("operator_recovery_target_reached") is True
                or snapshot.get("operator_recovery_resume_auto_status") is not None
            )
        ):
            return task_payload
        if request_matches:
            last_payload = task_payload
        time.sleep(max(0.1, poll_interval))
    return last_payload


def _task_and_timeline(
    client: MissionOSGatewayClient,
    task_id: str,
    *,
    timeline_limit: int = SITL_EXECUTION_POLL_TIMELINE_LIMIT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    encoded_task_id = quote(task_id, safe="")
    task_payload = client.get(f"/tasks/{encoded_task_id}")
    timeline_payload = (
        client.get(f"/tasks/{encoded_task_id}/timeline?limit={timeline_limit}")
        if timeline_limit
        else {"events": []}
    )
    return task_payload, timeline_payload


def _job_progress_status_text(task_payload: dict[str, Any] | None) -> str:
    if not isinstance(task_payload, dict):
        return "Execute Live SITL is running... waiting for Gateway response"
    task = _task_record(task_payload)
    artifacts = _task_artifacts(task_payload)
    snapshot = artifacts.get("missionos_auto_mission_runtime_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    running_receipt = artifacts.get("missionos_auto_mission_gui_dispatch_running_receipt")
    running_receipt = running_receipt if isinstance(running_receipt, dict) else {}
    dispatch_receipt = artifacts.get("missionos_auto_mission_gui_dispatch_receipt")
    dispatch_receipt = dispatch_receipt if isinstance(dispatch_receipt, dict) else {}
    agent_bridge = artifacts.get("missionos_runtime_recovery_agent_live_bridge")
    agent_bridge = agent_bridge if isinstance(agent_bridge, dict) else {}
    agent_result = agent_bridge.get("runtime_recovery_agent_result")
    agent_result = agent_result if isinstance(agent_result, dict) else {}
    agent_assessment = agent_result.get("assessment")
    agent_assessment = agent_assessment if isinstance(agent_assessment, dict) else {}

    status = _status_text(task.get("status") or task.get("task_status"))
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    dispatch_status = _status_text(
        dispatch_receipt.get("dispatch_status")
        or running_receipt.get("dispatch_status")
        or metadata.get("missionos_auto_mission_gui_dispatch_status")
    )
    progress_m = _as_float(snapshot.get("progress_m"))
    route_distance_m = _job_route_distance_m(artifacts)
    reached_seq = _as_int(snapshot.get("mission_reached_seq"))
    waypoint_total = _as_int(snapshot.get("waypoint_total"))
    battery = _battery_display_text(
        snapshot=snapshot,
        artifacts=artifacts,
        diagnostics=False,
    )
    terrain_clearance = snapshot.get("terrain_clearance_m")
    elapsed = snapshot.get("elapsed_seconds")
    monitor_ended = snapshot.get("monitor_window_ended") is True or (
        snapshot.get("snapshot_status") == "monitor_window_ended"
    )

    parts = [f"task={_status_text(task.get('task_id'))}", f"status={status}"]
    if dispatch_status != "-":
        parts.append(f"dispatch={dispatch_status}")
    if progress_m is not None:
        if route_distance_m is not None:
            parts.append(f"{_format_distance(progress_m)}/{_format_distance(route_distance_m)}")
        else:
            parts.append(_format_distance(progress_m))
    if reached_seq is not None or waypoint_total is not None:
        parts.append(f"wp {_status_text(reached_seq)}/{_status_text(waypoint_total)}")
    if battery != "-":
        parts.append(f"battery {battery}")
    if terrain_clearance is not None:
        parts.append(f"terrain_clearance {_format_distance(terrain_clearance)}")
    weather_text = _job_weather_compact_text(artifacts)
    if weather_text:
        parts.append(weather_text)
    if elapsed is not None:
        parts.append(_format_duration(elapsed))
    operator_dispatch_text = _operator_recovery_dispatch_status_text(
        artifacts=artifacts,
        snapshot=snapshot,
        compact=True,
    )
    if operator_dispatch_text:
        parts.append(operator_dispatch_text)
    agent_action = _first_present(
        agent_assessment.get("selected_bounded_action"),
        agent_assessment.get("recommended_action"),
        agent_assessment.get("recovery_action"),
    )
    if agent_action:
        proposal_status = _runtime_recovery_effective_status(
            agent_result,
            agent_bridge,
            agent_assessment,
        )
        agent_risk = agent_assessment.get("observed_risk_reasons")
        if isinstance(agent_risk, (list, tuple)):
            risk_text = ",".join(str(item) for item in agent_risk[:2])
            if len(agent_risk) > 2:
                risk_text += ",..."
        else:
            risk_text = _status_text(
                agent_risk
                or agent_assessment.get("trigger_reasons")
                or agent_assessment.get("risk_level")
            )
        parts.append(
            f"agent_proposal {proposal_status}:{_status_text(agent_action)}"
            + (f" risk={risk_text}" if risk_text != "-" else "")
        )
        if not operator_dispatch_text and not monitor_ended and status == "running":
            recovery_hint = _operator_recovery_dispatch_hint(
                task_id=task.get("task_id"),
                action=agent_action,
                parameters=agent_assessment.get("proposed_parameters")
                if isinstance(agent_assessment.get("proposed_parameters"), dict)
                else None,
                compact=True,
            )
            if recovery_hint:
                parts.append(recovery_hint)
    if monitor_ended and status == "running":
        parts.append("finalizing")
    return "Execute Live SITL is running... " + " · ".join(parts)


def _execute_sitl_with_task_polling(
    client: MissionOSGatewayClient,
    *,
    task_id: str,
    live_flight_mode: bool,
    poll_interval: float = SITL_EXECUTION_POLL_INTERVAL,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Run Execute Live SITL while polling task state.

    Gateway's execute boundary is intentionally authoritative and can run for a
    long AUTO mission. The CLI keeps the HTTP request alive in a worker while the
    foreground renders task state, so a client-side read timeout does not become
    a raw traceback.
    """

    executor = ThreadPoolExecutor(max_workers=1)
    future: Future[dict[str, Any]] = executor.submit(
        client.execute_sitl,
        task_id=task_id,
        live_flight_mode=live_flight_mode,
    )
    last_task_payload: dict[str, Any] | None = None
    last_timeline_payload: dict[str, Any] | None = None
    http_timed_out = False
    try:
        while True:
            if http_timed_out:
                try:
                    last_task_payload, last_timeline_payload = _task_and_timeline(client, task_id)
                except click.ClickException:
                    time.sleep(max(0.01, poll_interval))
                    continue
                if progress_callback:
                    progress_callback(last_task_payload)
                status = _task_status(last_task_payload)
                if status in TERMINAL_TASK_STATUSES:
                    return None, last_task_payload, last_timeline_payload
                time.sleep(max(0.01, poll_interval))
                continue

            try:
                payload = future.result(timeout=max(0.01, poll_interval))
                return payload, last_task_payload, last_timeline_payload
            except FutureTimeout:
                try:
                    last_task_payload, last_timeline_payload = _task_and_timeline(client, task_id)
                except click.ClickException:
                    continue
                if progress_callback:
                    progress_callback(last_task_payload)
                status = _task_status(last_task_payload)
                if status in TERMINAL_TASK_STATUSES:
                    try:
                        payload = future.result(timeout=0.01)
                    except (FutureTimeout, httpx.ReadTimeout):
                        payload = None
                    return payload, last_task_payload, last_timeline_payload
            except httpx.ReadTimeout:
                try:
                    last_task_payload, last_timeline_payload = _task_and_timeline(client, task_id)
                except click.ClickException as exc:
                    raise click.ClickException(
                        "Execute Live SITL Gateway response exceeded the client wait window and task status "
                        f"could not be read: {exc.message}"
                    ) from exc
                if progress_callback:
                    progress_callback(last_task_payload)
                status = _task_status(last_task_payload)
                if status in TERMINAL_TASK_STATUSES:
                    return None, last_task_payload, last_timeline_payload
                http_timed_out = True
    finally:
        executor.shutdown(wait=future.done(), cancel_futures=not future.done())


@click.group(name="missionos")
@click.option("--gateway-url", default=DEFAULT_GATEWAY_URL, show_default=True)
@click.option("--timeout", default=45.0, show_default=True, type=float)
@click.option("--json-output", "json_output", is_flag=True, help="Print raw JSON.")
@click.option(
    "--state-path",
    default=DEFAULT_STATE_PATH,
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Persist source-bound Mission Designer context between CLI commands.",
)
@click.pass_context
def missionos(
    ctx: click.Context,
    gateway_url: str,
    timeout: float,
    json_output: bool,
    state_path: Path,
) -> None:
    """Operate MissionOS through Gateway-backed CLI boundaries."""
    ctx.obj = ctx.obj or {}
    ctx.obj["missionos_client"] = make_client(gateway_url, timeout)
    ctx.obj["missionos_gateway_url"] = gateway_url
    ctx.obj["missionos_json_output"] = json_output
    ctx.obj["missionos_state_path"] = state_path


@missionos.group("gateway")
def gateway_command() -> None:
    """Start, stop, or inspect the local MissionOS Gateway."""


@gateway_command.command("start")
@click.option(
    "--pid-path",
    default=DEFAULT_GATEWAY_PID_PATH,
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="PID file for the managed Gateway process.",
)
@click.option(
    "--log-path",
    default=DEFAULT_GATEWAY_LOG_PATH,
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Log file for the managed Gateway process.",
)
@click.option(
    "--wait/--no-wait",
    default=True,
    show_default=True,
    help="Wait for /health before returning.",
)
@click.option(
    "--enable-live-sitl/--planning-only",
    default=False,
    show_default=True,
    help="Explicitly enable live SITL/dispatch Gateway environment variables.",
)
@click.pass_context
def gateway_start_command(
    ctx: click.Context,
    pid_path: Path,
    log_path: Path,
    wait: bool,
    enable_live_sitl: bool,
) -> None:
    """Start a local Gateway from the MissionOS CLI."""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    base_url: str = ctx.obj["missionos_gateway_url"]
    if _gateway_reachable(client):
        if enable_live_sitl and _gateway_is_fixture_backend(client):
            raise click.ClickException(
                "A fixture Gateway is already running at this URL. Live SITL "
                "requires the production backend. Run "
                "`missionos gateway restart --enable-live-sitl` and then retry."
            )
        console.print(f"[green]Gateway is already running:[/green] {base_url}")
        return
    existing_record = _read_gateway_pid_record(pid_path)
    existing_pid = (
        int(existing_record["pid"])
        if existing_record is not None and existing_record.get("pid") is not None
        else None
    )
    if existing_pid is not None and _process_running(existing_pid):
        if _gateway_pid_record_matches_running_process(existing_record or {}):
            raise click.ClickException(
                f"Gateway PID file already points to a running process: {existing_pid}"
            )
        pid_path.unlink(missing_ok=True)
        console.print(
            "[yellow]Discarded a stale Gateway PID file that pointed at another "
            "process. No process was stopped.[/yellow]"
        )
    _start_managed_gateway(
        client=client,
        base_url=base_url,
        pid_path=pid_path,
        log_path=log_path,
        wait=wait,
        enable_live_sitl=enable_live_sitl,
    )


@gateway_command.command("status")
@click.option(
    "--pid-path",
    default=DEFAULT_GATEWAY_PID_PATH,
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="PID file for the managed Gateway process.",
)
@click.pass_context
def gateway_status_command(ctx: click.Context, pid_path: Path) -> None:
    """Show whether the local Gateway is reachable and managed."""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    base_url: str = ctx.obj["missionos_gateway_url"]
    record = _read_gateway_pid_record(pid_path)
    pid = None if record is None else _read_gateway_pid(pid_path)
    reachable = _gateway_reachable(client)
    managed = (
        pid is not None
        and _process_running(pid)
        and _gateway_pid_record_matches_running_process(record or {})
    )
    table = Table(title=f"MissionOS Gateway: {base_url}")
    table.add_column("Check")
    table.add_column("Status")
    table.add_row("HTTP health", "healthy" if reachable else "unreachable")
    table.add_row("Managed PID", str(pid) if managed else "-")
    table.add_row("PID file", str(pid_path) if pid_path.exists() else "-")
    if record is not None:
        table.add_row(
            "Live SITL env",
            "enabled" if record.get("enable_live_sitl") is True else "planning-only",
        )
        table.add_row("Backend", _status_text(record.get("backend"), "fixture"))
        if pid is not None and _process_running(pid) and not managed:
            table.add_row("PID validation", "mismatch/refused")
    console.print(table)


@gateway_command.command("stop")
@click.option(
    "--pid-path",
    default=DEFAULT_GATEWAY_PID_PATH,
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="PID file for the managed Gateway process.",
)
def gateway_stop_command(pid_path: Path) -> None:
    """Stop a Gateway previously started by `missionos gateway start`."""
    record = _read_gateway_pid_record(pid_path)
    pid = _read_gateway_pid(pid_path)
    if pid is None:
        console.print("[yellow]No managed Gateway PID is recorded.[/yellow]")
        return
    if _process_running(pid) and not _gateway_pid_record_matches_running_process(record or {}):
        pid_path.unlink(missing_ok=True)
        raise click.ClickException(
            f"Gateway PID file did not match a managed MissionOS Gateway: pid={pid}. "
            "Stale PID file was removed; no process was stopped."
        )
    if _stop_gateway_pid(pid):
        pid_path.unlink(missing_ok=True)
        console.print(f"[green]Stopped Gateway:[/green] pid={pid}")
        return
    raise click.ClickException(f"Could not stop Gateway: pid={pid}")


@gateway_command.command("restart")
@click.option(
    "--pid-path",
    default=DEFAULT_GATEWAY_PID_PATH,
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="PID file for the managed Gateway process.",
)
@click.option(
    "--log-path",
    default=DEFAULT_GATEWAY_LOG_PATH,
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Log file for the managed Gateway process.",
)
@click.option(
    "--wait/--no-wait",
    default=True,
    show_default=True,
    help="Wait for /health before returning.",
)
@click.option(
    "--enable-live-sitl/--planning-only",
    default=False,
    show_default=True,
    help="Explicitly enable live SITL/dispatch Gateway environment variables.",
)
@click.pass_context
def gateway_restart_command(
    ctx: click.Context,
    pid_path: Path,
    log_path: Path,
    wait: bool,
    enable_live_sitl: bool,
) -> None:
    """Restart a Gateway previously started by `missionos gateway start`."""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    base_url: str = ctx.obj["missionos_gateway_url"]
    record = _read_gateway_pid_record(pid_path)
    pid = _read_gateway_pid(pid_path)
    if pid is None:
        if _gateway_reachable(client):
            raise click.ClickException(
                "Gateway is reachable but has no managed MissionOS PID file. "
                "No process was stopped. Use a different --gateway-url or stop "
                "the unmanaged Gateway explicitly before restart."
            )
    elif _process_running(pid):
        if not _gateway_pid_record_matches_running_process(record or {}):
            pid_path.unlink(missing_ok=True)
            raise click.ClickException(
                f"Gateway PID file did not match a managed MissionOS Gateway: pid={pid}. "
                "Stale PID file was removed; no process was stopped."
            )
        if not _stop_gateway_pid(pid):
            raise click.ClickException(f"Could not stop Gateway: pid={pid}")
        pid_path.unlink(missing_ok=True)
        console.print(f"[green]Stopped Gateway:[/green] pid={pid}")
    elif pid_path.exists():
        pid_path.unlink(missing_ok=True)
        console.print("[yellow]Removed PID file for a stopped Gateway.[/yellow]")
    _start_managed_gateway(
        client=client,
        base_url=base_url,
        pid_path=pid_path,
        log_path=log_path,
        wait=wait,
        enable_live_sitl=enable_live_sitl,
    )


@missionos.command("status")
@click.pass_context
def status_command(ctx: click.Context) -> None:
    """Show the current operator surfaces without starting execution."""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    payloads = {
        "health": client.health(),
        "form2a": client.get("/missionos/form2a-response-selection"),
        "review": client.get("/missionos/form2a-operator-review"),
        "action": client.get("/missionos/form2a-action-consumption"),
        "repair": client.get("/missionos/llm-repair-planner"),
    }
    if ctx.obj["missionos_json_output"]:
        _print_json(payloads)
        return
    _print_status(payloads, base_url=ctx.obj["missionos_gateway_url"])


@missionos.command("say")
@click.argument("instruction", nargs=-1, required=True)
@click.option("--session-id", default=DEFAULT_SESSION_ID, show_default=True)
@click.option("--route-hint", default="", help="Gateway route hint, e.g. mission_designer_plan.")
@click.option("--coordinate-route-json", default="", help="Coordinate route JSON object.")
@click.option(
    "--coordinate-route-file",
    default="",
    type=click.Path(dir_okay=False),
    help="Path to a coordinate route JSON or YAML object.",
)
@click.pass_context
def say_command(
    ctx: click.Context,
    instruction: tuple[str, ...],
    session_id: str,
    route_hint: str,
    coordinate_route_json: str,
    coordinate_route_file: str,
) -> None:
    """Send a natural-language MissionOS instruction."""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    coordinate_route = _load_json_object(
        coordinate_route_json,
        label="--coordinate-route-json",
    ) or _load_coordinate_route_file(coordinate_route_file)
    payload = client.conversation(
        " ".join(instruction),
        session_id=session_id,
        mission_designer_context=_stored_mission_designer_context(ctx, session_id),
        coordinate_route=coordinate_route,
        route_hint=route_hint or None,
    )
    _remember_mission_designer_context(ctx, payload, session_id=session_id)
    if ctx.obj["missionos_json_output"]:
        _print_json(payload)
        return
    _print_conversation_result(payload)


def _intent_command(intent: str):
    @click.option("--session-id", default=DEFAULT_SESSION_ID, show_default=True)
    @click.pass_context
    def _run(ctx: click.Context, session_id: str) -> None:
        client: MissionOSGatewayClient = ctx.obj["missionos_client"]
        payload = client.conversation(
            INTENT_INSTRUCTIONS[intent],
            session_id=session_id,
            mission_designer_context=_stored_mission_designer_context(ctx, session_id),
            route_hint=INTENT_ROUTE_HINTS[intent],
        )
        _remember_mission_designer_context(ctx, payload, session_id=session_id)
        if ctx.obj["missionos_json_output"]:
            _print_json(payload)
            return
        _print_conversation_result(payload)

    return _run


for _intent, _help in {
    "approve": "Record operator approval through MissionOS.",
    "reject": "Record operator rejection through MissionOS.",
    "revision": "Ask MissionOS to revise the current plan.",
    "run": "Run the approved bounded action through execution gates.",
    "repair": "Ask MissionOS to diagnose and draft a repair.",
}.items():
    missionos.add_command(click.command(_intent, help=_help)(_intent_command(_intent)))


def _parse_recovery_parameters(items: tuple[str, ...] | list[str]) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise click.ClickException(f"recovery parameter must be key=value: {item}")
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise click.ClickException(f"recovery parameter key is empty: {item}")
        try:
            parameters[key] = float(value)
        except ValueError:
            parameters[key] = value
    return parameters


@missionos.command("clear-state")
@click.pass_context
def clear_state_command(ctx: click.Context) -> None:
    """Forget the stored source-bound Mission Designer context."""
    path: Path = ctx.obj["missionos_state_path"]
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise click.ClickException(f"could not remove {path}: {exc}") from exc
    if ctx.obj["missionos_json_output"]:
        _print_json({"state_cleared": True, "state_path": str(path)})
        return
    console.print(f"Cleared MissionOS CLI state at {path}")


@missionos.command("recover")
@click.option("--task-id", required=True, help="Running AUTO mission task id.")
@click.option(
    "--action",
    "recovery_action",
    required=True,
    help="Operator-approved recovery action; Gateway validates the current allowlist.",
)
@click.option(
    "--param",
    "recovery_params",
    multiple=True,
    help="Recovery parameter as key=value. Repeat for target_altitude_m, target_speed_mps, target_x_m, target_y_m.",
)
@click.pass_context
def recover_command(
    ctx: click.Context,
    task_id: str,
    recovery_action: str,
    recovery_params: tuple[str, ...],
) -> None:
    """Send an operator-approved recovery dispatch."""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    payload = client.recovery_dispatch(
        task_id=task_id,
        recovery_action=recovery_action,
        recovery_parameters=_parse_recovery_parameters(recovery_params),
    )
    if ctx.obj["missionos_json_output"]:
        _print_json(payload)
        return
    task_payload = _wait_for_active_runner_recovery_observation(client, payload)
    _print_recovery_result(payload, task_payload=task_payload)


@missionos.command("execute-sitl")
@click.option(
    "--task-id",
    default="",
    help="Prepared SITL execution task id. Defaults to the task stored by `run`.",
)
@click.option(
    "--live-flight/--upload-only",
    default=True,
    show_default=True,
    help="Request the explicit Execute Live SITL boundary.",
)
@click.option(
    "--poll-interval",
    default=SITL_EXECUTION_POLL_INTERVAL,
    show_default=True,
    type=click.FloatRange(0.1, 60.0),
    help="Seconds between task status polls during live SITL execution.",
)
@click.pass_context
def execute_sitl_command(
    ctx: click.Context,
    task_id: str,
    live_flight: bool,
    poll_interval: float,
) -> None:
    """Run the explicit Execute Live SITL boundary."""
    resolved_task_id = task_id or _stored_sitl_task_id(ctx)
    if not resolved_task_id:
        raise click.ClickException(
            "task id is required; run `missionos run` first or pass --task-id"
        )
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    if live_flight:
        with console.status(
            "[red]Execute Live SITL is running... waiting for Gateway response[/red]",
            spinner="dots",
        ) as status:
            payload, task_payload, timeline_payload = _execute_sitl_with_task_polling(
                client,
                task_id=resolved_task_id,
                live_flight_mode=True,
                poll_interval=poll_interval,
                progress_callback=lambda latest: status.update(
                    f"[red]{_job_progress_status_text(latest)}[/red]"
                ),
            )
    else:
        payload = client.execute_sitl(
            task_id=resolved_task_id,
            live_flight_mode=False,
        )
        task_payload = None
        timeline_payload = None
    latest_task_id = _remember_sitl_task_id_from_payload(
        ctx,
        task_payload if task_payload is not None else payload,
        fallback_task_id=resolved_task_id,
    )
    if ctx.obj["missionos_json_output"]:
        _print_json(
            {
                "task_id": latest_task_id,
                "execute_result": payload,
                "task": task_payload,
                "timeline": timeline_payload,
            }
            if live_flight
            else payload
        )
        return
    if payload is None and task_payload is not None and timeline_payload is not None:
        console.print(f"[yellow]{LIVE_SITL_RESPONSE_WAIT_EXCEEDED_MESSAGE}[/yellow]")
        _print_job_status(task_payload, timeline_payload)
        return
    _print_sitl_execution_result(payload)


@missionos.command("start-sitl")
@click.option(
    "--task-id",
    default="",
    help="Prepared SITL execution task id. Defaults to the task stored by `run`.",
)
@click.pass_context
def start_sitl_command(ctx: click.Context, task_id: str) -> None:
    """Start the PX4/Gazebo SITL environment readiness action."""
    resolved_task_id = task_id or _stored_sitl_task_id(ctx)
    if not resolved_task_id:
        raise click.ClickException(
            "task id is required; run `missionos run` first or pass --task-id"
        )
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    payload = client.start_sitl(task_id=resolved_task_id)
    _remember_sitl_task_id_from_payload(
        ctx,
        payload,
        fallback_task_id=resolved_task_id,
    )
    if ctx.obj["missionos_json_output"]:
        _print_json(payload)
        return
    _print_sitl_start_result(payload)


@missionos.command("job-status")
@click.option(
    "--task-id",
    default="",
    help="Task/job id to inspect. Defaults to the task stored by `run`.",
)
@click.option(
    "--timeline-limit",
    default=8,
    show_default=True,
    type=click.IntRange(0, 100),
    help="Number of recent task timeline events to show.",
)
@click.pass_context
def job_status_command(ctx: click.Context, task_id: str, timeline_limit: int) -> None:
    """Show a running or completed MissionOS task through the Gateway task API."""
    resolved_task_id = task_id or _stored_sitl_task_id(ctx)
    if not resolved_task_id:
        raise click.ClickException(
            "task id is required; run `missionos run` first or pass --task-id"
        )
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    encoded_task_id = quote(resolved_task_id, safe="")
    task_payload = client.get(f"/tasks/{encoded_task_id}")
    timeline_payload = (
        client.get(f"/tasks/{encoded_task_id}/timeline?limit={timeline_limit}")
        if timeline_limit
        else {"events": []}
    )
    if ctx.obj["missionos_json_output"]:
        _print_json(
            {
                "task_id": resolved_task_id,
                "task": task_payload,
                "timeline": timeline_payload,
            }
        )
        return
    _print_job_status(task_payload, timeline_payload)


# ── Live terminal dot-art map (`missionos watch`) ─────────────────────────────


def _projection_computed(projection: dict[str, Any]) -> bool:
    return projection.get("projection_status") == "computed"


@missionos.command("watch")
@click.option(
    "--task-id",
    default="",
    help="Task/job id to render. Defaults to the task stored by `run`.",
)
@click.option(
    "--poll-interval",
    default=FLIGHT_MAP_POLL_INTERVAL,
    show_default=True,
    type=click.FloatRange(0.2, 10.0),
    help="Seconds between telemetry polls.",
)
@click.pass_context
def watch_command(ctx: click.Context, task_id: str, poll_interval: float) -> None:
    """Render a live top-down dot-art map of the AUTO mission in the terminal."""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    resolved_task_id = _resolve_live_task_id(
        client,
        explicit_task_id=task_id,
        stored_task_id=_stored_sitl_task_id(ctx),
    )
    try:
        _watch_flight_map(client, resolved_task_id, poll_interval=poll_interval)
    except KeyboardInterrupt:
        console.print("[yellow](watch stopped)[/yellow]")


@missionos.command("map")
@click.option(
    "--task-id",
    default="",
    help="Task/job id to map. Defaults to the latest running SITL task.",
)
@click.option(
    "--provider",
    default="osm",
    show_default=True,
    type=click.Choice(sorted(MISSION_MAP_PROVIDERS)),
    help="Real basemap tile provider used by the generated browser view.",
)
@click.option(
    "--output",
    "output_path",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="HTML output path. Defaults to output/missionos_maps/<task>_<time>.html.",
)
@click.option(
    "--poll-interval",
    default=MISSION_MAP_POLL_INTERVAL,
    show_default=True,
    type=click.FloatRange(0.5, 10.0),
    help="Seconds between live Gateway polls in the generated browser map.",
)
@click.option(
    "--snapshot",
    is_flag=True,
    help="Generate a static one-time map instead of a live-polling map.",
)
@click.option(
    "--serve-live",
    is_flag=True,
    help="Serve an authenticated loopback live map until the task finishes.",
)
@click.option("--no-open", is_flag=True, help="Generate the HTML file without opening a browser.")
@click.pass_context
def map_command(
    ctx: click.Context,
    task_id: str,
    provider: str,
    output_path: Path | None,
    poll_interval: float,
    snapshot: bool,
    serve_live: bool,
    no_open: bool,
) -> None:
    """Generate a source-backed 2D browser map for the selected MissionOS task."""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    if client.api_key and not snapshot:
        # An authenticated Gateway cannot be polled from a file:// document.
        # Default to the loopback proxy so `missionos map` never presents a
        # misleading frozen snapshot unless --snapshot was explicit.
        serve_live = True
    resolved_task_id = _resolve_live_task_id(
        client,
        explicit_task_id=task_id,
        stored_task_id=_stored_sitl_task_id(ctx),
    )
    task_payload, _ = _task_and_timeline(client, resolved_task_id, timeline_limit=0)
    task_record = _task_record(task_payload)
    if (
        serve_live
        and task_record.get("kind") == "turtlebot3_home_mission_execution"
        and not _turtlebot3_indoor_map_model_from_artifacts(_task_artifacts(task_payload))
    ):
        # The companion can start as soon as the task record is created, a few
        # seconds before the first progress payload contains the indoor model.
        # Wait for that source-backed artifact instead of exiting into a stale
        # generic-map fallback.
        map_ready_deadline = time.monotonic() + 60.0
        while time.monotonic() < map_ready_deadline:
            time.sleep(0.5)
            task_payload, _ = _task_and_timeline(
                client,
                resolved_task_id,
                timeline_limit=0,
            )
            if _turtlebot3_indoor_map_model_from_artifacts(_task_artifacts(task_payload)):
                break
            if str(_task_record(task_payload).get("status") or "") in (TERMINAL_TASK_STATUSES):
                break
    live_task_url = None
    authenticated_file_snapshot = bool(client.api_key and not snapshot and not serve_live)
    if not snapshot and not authenticated_file_snapshot:
        encoded_task_id = quote(resolved_task_id, safe="")
        live_task_url = _join_url(client.base_url, f"/tasks/{encoded_task_id}")
    model = _mission_map_model(
        task_payload=task_payload,
        provider=provider,
        live_task_url=live_task_url,
        poll_interval=poll_interval,
    )
    if serve_live and not snapshot:
        _serve_authenticated_live_mission_map(
            client=client,
            task_id=resolved_task_id,
            model=model,
            no_open=no_open,
        )
        return
    path = _write_mission_map_html(model=model, output_path=output_path)
    evidence = _write_terminal_route_evidence(
        model=model,
        output_dir=path.parent,
        stem=f"{path.stem}_e2e_route_evidence",
    )
    file_url = path.resolve().as_uri()
    if ctx.obj["missionos_json_output"]:
        display_points = len(
            model.get("points") or model.get("observed_points") or model.get("planned_points") or []
        )
        _print_json(
            {
                "task_id": resolved_task_id,
                "map_kind": model.get("map_kind", "wgs84_route_overlay"),
                "map_provider": model["provider"]["label"],
                "output_path": str(path),
                "file_url": file_url,
                "evidence_image_path": (
                    str(evidence["svg_path"]) if evidence is not None else None
                ),
                "evidence_manifest_path": (
                    str(evidence["manifest_path"]) if evidence is not None else None
                ),
                "point_count": display_points,
                "planned_point_count": len(model.get("planned_points") or []),
                "observed_point_count": len(model.get("observed_points") or []),
                "obstacle_count": len(model.get("obstacles") or []),
                "avoidance_sample_count": _mission_map_avoidance_sample_count(model),
                "live": bool(model.get("live", {}).get("enabled")),
                "opened": False,
            }
        )
        return
    if authenticated_file_snapshot:
        console.print(
            "[yellow]The Gateway requires authentication, so this file:// map is "
            "a snapshot. Use the automatically opened map companion or "
            "`missionos map --serve-live` for authenticated live updates.[/yellow]"
        )
    opened = False
    if not no_open:
        opened = click.launch(file_url) == 0
    display_points = len(
        model.get("points") or model.get("observed_points") or model.get("planned_points") or []
    )
    boundary_text = (
        "boundary=indoor local-XY MissionOS/Nav2 evidence display; read-only, not verifier/dispatch/delivery/physical claim"
        if model.get("map_kind") == "indoor_local_xy"
        else "boundary=real basemap tiles + MissionOS route/telemetry overlay; read-only, not verifier/dispatch/delivery claim"
    )
    console.print(
        Panel(
            "\n".join(
                [
                    f"task_id={resolved_task_id}",
                    f"map_kind={model.get('map_kind', 'wgs84_route_overlay')}",
                    f"provider={model['provider']['label']}",
                    f"points={display_points}",
                    f"planned={len(model.get('planned_points') or [])}",
                    f"observed={len(model.get('observed_points') or [])}",
                    f"obstacles={len(model.get('obstacles') or [])}",
                    f"avoidance_samples={_mission_map_avoidance_sample_count(model)}",
                    f"html={path}",
                    "evidence_image="
                    + (
                        str(evidence["svg_path"])
                        if evidence is not None
                        else "not_generated_task_not_terminal_or_unsupported"
                    ),
                    "evidence_manifest="
                    + (str(evidence["manifest_path"]) if evidence is not None else "-"),
                    f"url={file_url}",
                    "live=" + ("true" if model.get("live", {}).get("enabled") else "false"),
                    "opened=" + ("true" if opened else "false"),
                    boundary_text,
                ]
            ),
            title="MissionOS 2D Map",
            border_style="cyan",
        )
    )


def _mission_map_avoidance_sample_count(model: dict[str, Any]) -> int:
    """Count the vehicle-specific saved Recovery observations shown on a map."""

    if model.get("map_kind") == "indoor_local_xy":
        recovery = model.get("recovery")
        recovery = recovery if isinstance(recovery, dict) else {}
        observed = recovery.get("observed_points")
        return len(observed) if isinstance(observed, list) else 0
    avoidance = model.get("avoidance")
    avoidance = avoidance if isinstance(avoidance, dict) else {}
    samples = avoidance.get("samples")
    return len(samples) if isinstance(samples, list) else 0


# ── Interactive operator view (`missionos operate`) ──────────────────────────
# Non-modal: live telemetry keeps refreshing while an agent proposal is shown.
# Dismissing ("view status") re-surfaces the proposal after a cooldown. A real
# LAND/RTL dispatch always requires an explicit `y` confirmation — Enter/any key
# never fires recovery. Dispatch still goes through the same recovery-dispatch
# route with explicit approval; the agent never gains dispatch authority.


def _agent_proposal_from_task(task_payload: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the proposal-only runtime recovery-agent recommendation, if any."""
    artifacts = _task_artifacts(task_payload)
    bridge = artifacts.get("missionos_runtime_recovery_agent_live_bridge")
    bridge = bridge if isinstance(bridge, dict) else {}
    result = bridge.get("runtime_recovery_agent_result")
    result = result if isinstance(result, dict) else {}
    assessment = result.get("assessment")
    assessment = assessment if isinstance(assessment, dict) else {}
    action = _first_present(
        assessment.get("selected_bounded_action"),
        assessment.get("recommended_action"),
        assessment.get("recovery_action"),
    )
    if not action:
        return None
    risks = assessment.get("observed_risk_reasons")
    if not isinstance(risks, (list, tuple)):
        risks = [risks] if risks else []
    return {
        "task_id": str(_task_record(task_payload).get("task_id") or ""),
        "action": str(action),
        "status": _runtime_recovery_effective_status(result, bridge, assessment),
        "risks": [str(r) for r in risks if r],
        "parameters": dict(assessment.get("proposed_parameters"))
        if isinstance(assessment.get("proposed_parameters"), dict)
        else {},
    }


def _first_mapping_item(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
    return {}


def _turtlebot3_recovery_summary_from_artifacts(
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    summary = artifacts.get("summary")
    if isinstance(summary, dict):
        return summary
    execution = artifacts.get("turtlebot3_home_mission_execution")
    execution = execution if isinstance(execution, dict) else {}
    nested = execution.get("summary")
    return nested if isinstance(nested, dict) else {}


def _turtlebot3_recovery_decision_summary_from_artifacts(
    artifacts: dict[str, Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    decision = artifacts.get("turtlebot3_recovery_decision_summary")
    if isinstance(decision, dict):
        return decision
    nested = summary.get("turtlebot3_recovery_decision_summary")
    return nested if isinstance(nested, dict) else {}


def _recovery_dispatch_action_from_proposal_action(action: Any) -> str:
    normalized = str(action or "").strip().lower().replace("-", "_")
    if normalized in {"return_home", "return_to_home", "rtl"}:
        return "return_to_launch"
    return normalized


def _recovery_proposal_parameters(
    *,
    action: str,
    summary: dict[str, Any],
    proposal: dict[str, Any],
) -> dict[str, Any]:
    if action in {"return_to_launch", "land", "hold", "safe_stop"}:
        return {}
    for key in ("recovery_parameters", "proposed_parameters"):
        value = proposal.get(key)
        if isinstance(value, dict) and value:
            return dict(value)
    observations = proposal.get("input_observations")
    observations = observations if isinstance(observations, dict) else {}
    if action == "avoid_obstacle":
        scenario = summary.get("runtime_recovery_obstacle_scenario")
        scenario = scenario if isinstance(scenario, dict) else {}
        x_value = _first_present(
            observations.get("recommended_avoidance_target_x_m"),
            scenario.get("recommended_avoidance_target_x_m"),
        )
        y_value = _first_present(
            observations.get("recommended_avoidance_target_y_m"),
            scenario.get("recommended_avoidance_target_y_m"),
        )
        parameters: dict[str, Any] = {}
        if x_value is not None and y_value is not None:
            parameters["target_x_m"] = x_value
            parameters["target_y_m"] = y_value
        if observations.get("target_altitude_m") is not None:
            parameters["target_altitude_m"] = observations["target_altitude_m"]
        return parameters
    if action == "reroute":
        x_value = _first_present(observations.get("target_x_m"), observations.get("x_m"))
        y_value = _first_present(observations.get("target_y_m"), observations.get("y_m"))
        if x_value is not None and y_value is not None:
            return {"target_x_m": x_value, "target_y_m": y_value}
    if action == "adjust_altitude":
        altitude = observations.get("target_altitude_m")
        return {"target_altitude_m": altitude} if altitude is not None else {}
    if action == "adjust_speed":
        speed = observations.get("target_speed_mps")
        return {"target_speed_mps": speed} if speed is not None else {}
    return {}


def _pending_recovery_approval_from_task(
    task_payload: dict[str, Any],
) -> dict[str, Any] | None:
    artifacts = _task_artifacts(task_payload)
    summary = _turtlebot3_recovery_summary_from_artifacts(artifacts)
    decision = _turtlebot3_recovery_decision_summary_from_artifacts(artifacts, summary)
    task = _task_record(task_payload)
    task_id = str(task.get("task_id") or "").strip()
    task_kind = str(task.get("kind") or "")
    task_status = str(task.get("status") or "").strip().lower()
    runtime_proposal = artifacts.get("missionos_runtime_recovery_last_proposal")
    runtime_proposal = runtime_proposal if isinstance(runtime_proposal, Mapping) else {}
    runtime_proposal_schema = str(runtime_proposal.get("schema_version") or "")
    if (
        task_kind != "turtlebot3_home_mission_execution"
        and task_status == "running"
        and runtime_proposal_schema
        in {
            "missionos_runtime_recovery_proposal_evidence.v1",
            "missionos_runtime_recovery_proposal_evidence.v2",
            "missionos_runtime_recovery_proposal_evidence.v3",
        }
        and runtime_proposal.get("proposal_status") == "awaiting_operator_approval"
    ):
        runtime_result = runtime_proposal.get("runtime_recovery_agent_result")
        runtime_result = runtime_result if isinstance(runtime_result, Mapping) else {}
        runtime_assessment = runtime_result.get("assessment")
        runtime_assessment = runtime_assessment if isinstance(runtime_assessment, Mapping) else {}
        compilation = runtime_assessment.get("intent_compilation")
        compilation = compilation if isinstance(compilation, Mapping) else {}
        reachability = runtime_assessment.get("reachability_verification")
        reachability = reachability if isinstance(reachability, Mapping) else {}
        if runtime_proposal_schema.endswith(".v2") and (
            compilation.get("compilation_status") != "compiled"
            or reachability.get("verification_status") != "verified"
            or reachability.get("reachability_verified") is not True
        ):
            return None
        if runtime_proposal_schema.endswith(".v3"):
            hazard_state = runtime_assessment.get("hazard_state")
            hazard_state = hazard_state if isinstance(hazard_state, Mapping) else {}
            action_feasibility = runtime_assessment.get("action_feasibility")
            action_feasibility = (
                action_feasibility if isinstance(action_feasibility, Mapping) else {}
            )
            if (
                compilation.get("compilation_status") != "compiled"
                or reachability.get("verification_status") != "verified"
                or reachability.get("reachability_verified") is not True
                or hazard_state.get("hazard_state_status") != "verified"
                or action_feasibility.get("feasibility_status") != "verified_feasible"
            ):
                return None
        selected_action = str(
            compilation.get("compiled_action")
            or runtime_assessment.get("selected_bounded_action")
            or ""
        ).strip()
        dispatch_action = _recovery_dispatch_action_from_proposal_action(selected_action)
        proposed_parameters = compilation.get("compiled_parameters") or runtime_assessment.get(
            "proposed_parameters"
        )
        proposed_parameters = (
            dict(proposed_parameters) if isinstance(proposed_parameters, Mapping) else {}
        )
        receipt = artifacts.get("missionos_runtime_recovery_dispatch_receipt")
        receipt = receipt if isinstance(receipt, Mapping) else {}
        receipt_revalidation = receipt.get("proposal_revalidation")
        receipt_revalidation = (
            receipt_revalidation if isinstance(receipt_revalidation, Mapping) else {}
        )
        proposal_id = str(runtime_proposal.get("proposal_id") or "")
        matching_authority_exists = bool(
            proposal_id
            and receipt_revalidation.get("proposal_id") == proposal_id
            and receipt.get("dispatch_authority_created") is True
        )
        if task_id and dispatch_action and not matching_authority_exists:
            agent_output = runtime_result.get("agent_output")
            agent_output = agent_output if isinstance(agent_output, Mapping) else {}
            invocations = runtime_result.get("agent_invocations")
            invocations = (
                invocations
                if isinstance(invocations, Sequence) and not isinstance(invocations, (str, bytes))
                else []
            )
            invocation = next(
                (dict(item) for item in invocations if isinstance(item, Mapping)),
                {},
            )
            proposal_origin = runtime_proposal.get("proposal_origin")
            proposal_origin = (
                dict(proposal_origin) if isinstance(proposal_origin, Mapping) else {}
            )
            bridge = artifacts.get("missionos_runtime_recovery_agent_live_bridge")
            bridge = bridge if isinstance(bridge, Mapping) else {}
            observations = bridge.get("telemetry_snapshot")
            observations = dict(observations) if isinstance(observations, Mapping) else {}
            return {
                "task_id": task_id,
                "selected_action": selected_action,
                "recovery_action": dispatch_action,
                "recovery_parameters": proposed_parameters,
                "proposal_source": str(runtime_proposal.get("proposal_source") or ""),
                "rules_execution_class": str(runtime_assessment.get("assessment_status") or ""),
                "requires_new_human_approval": True,
                "checkpoint_id": "",
                "checkpoint_hash": "",
                "checkpoint_approval_supported": True,
                "runtime_proposal_approval_supported": True,
                "checkpoint_revision_supported": False,
                "checkpoint_dispatch_supported": True,
                "operator_guidance_required": False,
                "recovery_proposal_id": proposal_id,
                "proposal_reason": str(agent_output.get("rationale") or ""),
                "input_observations": observations,
                "llm_provider": str(
                    proposal_origin.get("provider") or invocation.get("provider") or ""
                ),
                "llm_model_id": str(
                    proposal_origin.get("model_id") or invocation.get("model_id") or ""
                ),
                "proposal_origin": proposal_origin,
                "source_obstacle_name": str(
                    runtime_proposal.get("source_obstacle_name")
                    or proposed_parameters.get("source_obstacle_name")
                    or ""
                ),
                "action_feasibility": (
                    dict(action_feasibility)
                    if runtime_proposal_schema.endswith(".v3")
                    else {}
                ),
                "dispatch_authority_created": False,
                "physical_execution_invoked": False,
            }
    checkpoint = artifacts.get("turtlebot3_recovery_checkpoint")
    if not isinstance(checkpoint, dict):
        checkpoint = summary.get("turtlebot3_recovery_checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    is_turtlebot3_recovery = (
        task_kind == "turtlebot3_home_mission_execution"
        or _is_home_robot_nav2_execution_target(summary.get("execution_target"))
        or bool(checkpoint)
    )
    if is_turtlebot3_recovery:
        if (
            task_status != "pending"
            or checkpoint.get("schema_version") != "turtlebot3_recovery_checkpoint.v1"
            or checkpoint.get("checkpoint_status") != "awaiting_operator_approval"
        ):
            return None
        selected_action = str(checkpoint.get("selected_action") or "")
        # TurtleBot3 checkpoint actions are native Nav2 recovery actions. In
        # particular, return_home must not be rewritten to PX4's
        # return_to_launch command.
        dispatch_action = selected_action.strip().lower().replace("-", "_")
        parameters = checkpoint.get("approved_parameters")
        parameters = dict(parameters) if isinstance(parameters, dict) else {}
        proposal_id = str(checkpoint.get("recovery_proposal_id") or "")
        classification_id = str(checkpoint.get("recovery_classification_id") or "")
        proposals = summary.get("recovery_proposals")
        proposals = proposals if isinstance(proposals, list) else []
        proposal = next(
            (
                dict(item)
                for item in proposals
                if proposal_id
                and isinstance(item, dict)
                and str(item.get("proposal_id") or "") == proposal_id
            ),
            {},
        )
        classifications = summary.get("recovery_proposal_classifications")
        classifications = classifications if isinstance(classifications, list) else []
        classification = next(
            (
                dict(item)
                for item in classifications
                if classification_id
                and isinstance(item, dict)
                and str(item.get("classification_id") or "") == classification_id
            ),
            {},
        )
        llm_evidence = proposal.get("llm_invocation_evidence")
        llm_evidence = dict(llm_evidence) if isinstance(llm_evidence, dict) else {}
        observations = proposal.get("input_observations")
        observations = dict(observations) if isinstance(observations, dict) else {}
        execution_target = str(
            checkpoint.get("execution_target") or summary.get("execution_target") or ""
        )
        robot_profile = str(checkpoint.get("robot_profile") or summary.get("robot_profile") or "")
        plan = artifacts.get("turtlebot3_home_mission_plan")
        plan = plan if isinstance(plan, Mapping) else {}
        stored_checkpoint = artifacts.get("turtlebot3_recovery_checkpoint")
        stored_checkpoint = stored_checkpoint if isinstance(stored_checkpoint, Mapping) else {}
        strict_turtlebot3_scope = task_kind == "turtlebot3_home_mission_execution" and all(
            str(view.get("robot_profile") or "") == "turtlebot3"
            and str(view.get("execution_target") or "") == "ros2_nav2_turtlebot3_sim"
            for view in (plan, stored_checkpoint, summary)
        )
        operator_guidance_required = checkpoint.get(
            "operator_guidance_required"
        ) is True or dispatch_action in {"ask_human", "hold", "safe_stop"}
        checkpoint_dispatch_supported = (
            dispatch_action in {"avoid_obstacle", "return_home", "reroute"}
            and not operator_guidance_required
        )
        if not task_id or not dispatch_action:
            return None
        return {
            "task_id": task_id,
            "selected_action": selected_action,
            "recovery_action": dispatch_action,
            "recovery_parameters": parameters,
            "proposal_source": str(proposal.get("proposal_source") or ""),
            "rules_execution_class": str(classification.get("execution_class") or ""),
            "requires_new_human_approval": True,
            "checkpoint_id": str(checkpoint.get("checkpoint_id") or ""),
            "checkpoint_hash": str(checkpoint.get("checkpoint_hash") or ""),
            "parent_checkpoint_id": str(checkpoint.get("parent_checkpoint_id") or ""),
            "parent_checkpoint_hash": str(checkpoint.get("parent_checkpoint_hash") or ""),
            "revision_id": str(checkpoint.get("revision_id") or ""),
            "operator_instruction_sha256": str(checkpoint.get("operator_instruction_sha256") or ""),
            "robot_profile": robot_profile,
            "execution_target": execution_target,
            "checkpoint_approval_supported": (
                strict_turtlebot3_scope and checkpoint_dispatch_supported
            ),
            "checkpoint_revision_supported": strict_turtlebot3_scope,
            "checkpoint_dispatch_supported": checkpoint_dispatch_supported,
            "operator_guidance_required": operator_guidance_required,
            "recovery_proposal_id": proposal_id,
            "recovery_classification_id": classification_id,
            "proposal_reason": str(proposal.get("reason") or ""),
            "input_observations": observations,
            "llm_provider": str(llm_evidence.get("provider") or ""),
            "llm_model_id": str(llm_evidence.get("model_id") or ""),
            "dispatch_authority_created": False,
            "physical_execution_invoked": False,
        }
    if not summary and not decision:
        return None
    classification = _first_mapping_item(summary.get("recovery_proposal_classifications"))
    proposal = _first_mapping_item(summary.get("recovery_proposals"))
    requires_approval = (
        decision.get("requires_new_human_approval") is True
        or classification.get("requires_new_human_approval") is True
    )
    if not requires_approval:
        return None
    already_dispatched = (
        decision.get("recovery_dispatch_request_sent") is True
        or summary.get("recovery_dispatch_request_sent") is True
    )
    if already_dispatched:
        return None
    selected_action = _first_present(
        decision.get("selected_action"),
        summary.get("runtime_recovery_action_kind"),
        summary.get("recovery_action_suggested"),
        proposal.get("selected_action"),
    )
    dispatch_action = _recovery_dispatch_action_from_proposal_action(selected_action)
    if not dispatch_action:
        return None
    parameters = _recovery_proposal_parameters(
        action=dispatch_action,
        summary=summary,
        proposal=proposal,
    )
    return {
        "task_id": task_id,
        "selected_action": str(selected_action or ""),
        "recovery_action": dispatch_action,
        "recovery_parameters": parameters,
        "proposal_source": decision.get("recovery_proposal_source")
        or proposal.get("proposal_source"),
        "rules_execution_class": decision.get("rules_execution_class")
        or classification.get("execution_class"),
        "requires_new_human_approval": True,
        "checkpoint_id": "",
        "checkpoint_hash": "",
        "checkpoint_approval_supported": True,
        "checkpoint_revision_supported": False,
        "proposal_reason": str(proposal.get("reason") or ""),
        "input_observations": dict(proposal.get("input_observations"))
        if isinstance(proposal.get("input_observations"), dict)
        else {},
        "llm_provider": "",
        "llm_model_id": "",
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
    }


def _is_recovery_approval_text(raw: str) -> bool:
    text = raw.strip().lower()
    compact = text.replace(" ", "").replace("　", "")
    if compact in {
        "承認",
        "承認します",
        "リカバリ承認",
        "回復承認",
        "承認して",
        "承認する",
    }:
        return True
    return text in {
        "approve recovery",
        "approve the recovery",
        "approve recovery proposal",
        "approve the recovery proposal",
        "operator approve recovery",
    }


def _lookup_pending_recovery_approval(
    client: MissionOSGatewayClient,
    *,
    task_id: str,
) -> dict[str, Any] | None:
    task_payload, _ = _task_and_timeline(client, task_id, timeline_limit=0)
    pending = _pending_recovery_approval_from_task(task_payload)
    if pending and not pending.get("task_id"):
        pending["task_id"] = task_id
    return pending


def _chat_recovery_revision_context(ctx: click.Context) -> dict[str, str]:
    value = ctx.obj.get("missionos_chat_recovery_revision_context")
    if not isinstance(value, dict):
        return {}
    context = {
        "task_id": str(value.get("task_id") or "").strip(),
        "checkpoint_id": str(value.get("checkpoint_id") or "").strip(),
        "checkpoint_hash": str(value.get("checkpoint_hash") or "").strip(),
    }
    if not all(context.values()):
        return {}
    return context


def _set_chat_recovery_revision_context(
    ctx: click.Context,
    *,
    pending: Mapping[str, Any],
) -> bool:
    context = {
        "task_id": str(pending.get("task_id") or "").strip(),
        "checkpoint_id": str(pending.get("checkpoint_id") or "").strip(),
        "checkpoint_hash": str(pending.get("checkpoint_hash") or "").strip(),
    }
    if not all(context.values()):
        return False
    ctx.obj["missionos_chat_recovery_revision_context"] = context
    return True


def _clear_chat_recovery_revision_context(ctx: click.Context) -> None:
    ctx.obj.pop("missionos_chat_recovery_revision_context", None)


def _turtlebot3_recovery_checkpoint_content_hash(
    checkpoint: Mapping[str, Any],
) -> str:
    mutable_fields = {
        "checkpoint_id",
        "checkpoint_hash",
        "checkpoint_status",
        "claimed_at",
        "claimed_by_approval_ref",
        "consumed_at",
        "consumed_by_approval_ref",
        "failed_at",
        "failure_reasons",
    }
    payload = {
        str(key): value
        for key, value in checkpoint.items()
        if str(key) not in mutable_fields and not str(key).startswith("superseded_")
    }
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _refetched_turtlebot3_revision_state(
    client: MissionOSGatewayClient,
    *,
    task_id: str,
) -> tuple[dict[str, Any] | None, bool, bool, dict[str, str]]:
    task_payload, _ = _task_and_timeline(client, task_id, timeline_limit=0)
    task = _task_record(task_payload)
    artifacts = _task_artifacts(task_payload)
    summary = _turtlebot3_recovery_summary_from_artifacts(artifacts)
    checkpoint = artifacts.get("turtlebot3_recovery_checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    receipt = artifacts.get("missionos_runtime_recovery_dispatch_receipt")
    receipt = receipt if isinstance(receipt, dict) else {}
    pending = _pending_recovery_approval_from_task(task_payload)
    checkpoints = artifacts.get("turtlebot3_recovery_checkpoints")
    checkpoints = checkpoints if isinstance(checkpoints, dict) else {}
    parent_id = str(checkpoint.get("parent_checkpoint_id") or "")
    parent_hash = str(checkpoint.get("parent_checkpoint_hash") or "")
    checkpoint_id = str(checkpoint.get("checkpoint_id") or "")
    checkpoint_hash = str(checkpoint.get("checkpoint_hash") or "")

    def _binds_current_checkpoint(value: Any) -> bool:
        if not isinstance(value, Mapping):
            return False
        return (
            str(value.get("checkpoint_id") or "") == checkpoint_id
            and str(value.get("checkpoint_hash") or "") == checkpoint_hash
        )

    operator_approval = artifacts.get("turtlebot3_recovery_operator_approval")
    bounded_action = artifacts.get("turtlebot3_recovery_bounded_action")
    receipt_approval = receipt.get("turtlebot3_recovery_operator_approval")
    receipt_bounded_action = receipt.get("turtlebot3_recovery_bounded_action")
    receipt_directly_binds_current = (
        str(receipt.get("reviewed_recovery_checkpoint_id") or "") == checkpoint_id
        and str(receipt.get("reviewed_recovery_checkpoint_hash") or "") == checkpoint_hash
    )
    receipt_has_current_authority = (
        _binds_current_checkpoint(receipt_approval)
        or _binds_current_checkpoint(receipt_bounded_action)
        or (
            receipt_directly_binds_current
            and (
                receipt.get("dispatch_authority_created") is True
                or receipt.get("operator_approved") is True
                or receipt.get("explicit_recovery_dispatch_approval") is True
            )
        )
    )
    durable_child = checkpoints.get(checkpoint_id)
    durable_child = durable_child if isinstance(durable_child, dict) else {}
    durable_parent = checkpoints.get(parent_id)
    durable_parent = durable_parent if isinstance(durable_parent, dict) else {}
    revision_id = str(checkpoint.get("revision_id") or "")
    revision_records = artifacts.get("turtlebot3_recovery_revisions")
    revision_records = revision_records if isinstance(revision_records, dict) else {}
    current_revision_record = artifacts.get("turtlebot3_recovery_revision")
    current_revision_record = (
        current_revision_record if isinstance(current_revision_record, dict) else {}
    )
    durable_revision_record = revision_records.get(revision_id)
    durable_revision_record = (
        durable_revision_record if isinstance(durable_revision_record, dict) else {}
    )
    execution = artifacts.get("turtlebot3_home_mission_execution")
    execution = execution if isinstance(execution, dict) else {}
    embedded_checkpoint = execution.get("turtlebot3_recovery_checkpoint")
    embedded_checkpoint = embedded_checkpoint if isinstance(embedded_checkpoint, dict) else {}
    summary_checkpoint = summary.get("turtlebot3_recovery_checkpoint")
    summary_checkpoint = summary_checkpoint if isinstance(summary_checkpoint, dict) else {}
    execution_revision_lineage = execution.get("recovery_checkpoint_revision")
    execution_revision_lineage = (
        execution_revision_lineage if isinstance(execution_revision_lineage, dict) else {}
    )
    summary_revision_lineage = summary.get("recovery_checkpoint_revision")
    summary_revision_lineage = (
        summary_revision_lineage if isinstance(summary_revision_lineage, dict) else {}
    )
    computed_checkpoint_hash = _turtlebot3_recovery_checkpoint_content_hash(checkpoint)
    current_checkpoint_integrity_valid = (
        bool(checkpoint_hash)
        and checkpoint_hash == computed_checkpoint_hash
        and checkpoint_id == f"turtlebot3_recovery_checkpoint_{computed_checkpoint_hash[:12]}"
        and durable_child == checkpoint
        and embedded_checkpoint == checkpoint
        and summary_checkpoint == checkpoint
    )
    computed_parent_hash = _turtlebot3_recovery_checkpoint_content_hash(durable_parent)
    durable_parent_integrity_valid = (
        bool(parent_id and parent_hash)
        and parent_hash == computed_parent_hash
        and parent_id == f"turtlebot3_recovery_checkpoint_{computed_parent_hash[:12]}"
    )
    durable_revision_lineage_valid = (
        bool(revision_id)
        and current_revision_record == durable_revision_record
        and current_revision_record.get("schema_version")
        == "missionos_turtlebot3_recovery_checkpoint_revision.v1"
        and current_revision_record.get("revision_status") == "proposed"
        and str(current_revision_record.get("revision_id") or "") == revision_id
        and str(current_revision_record.get("parent_checkpoint_id") or "") == parent_id
        and str(current_revision_record.get("parent_checkpoint_hash") or "") == parent_hash
        and current_revision_record.get("turtlebot3_recovery_checkpoint") == checkpoint
        and current_revision_record.get("superseded_checkpoint") == durable_parent
        and current_revision_record.get("turtlebot3_home_mission_execution") == execution
        and current_revision_record.get("summary") == summary
        and execution_revision_lineage == summary_revision_lineage
        and str(execution_revision_lineage.get("revision_id") or "") == revision_id
        and str(execution_revision_lineage.get("parent_checkpoint_id") or "") == parent_id
        and str(execution_revision_lineage.get("child_checkpoint_id") or "") == checkpoint_id
        and str(execution_revision_lineage.get("revision_intent") or "")
        == str(checkpoint.get("revision_intent") or "")
        and execution_revision_lineage.get("operator_approval_created") is False
        and execution_revision_lineage.get("dispatch_authority_created") is False
        and execution_revision_lineage.get("physical_execution_invoked") is False
        and execution_revision_lineage.get("progress_counted") is False
    )
    lineage_valid = (
        bool(parent_id and parent_hash and checkpoint_id)
        and current_checkpoint_integrity_valid
        and durable_parent_integrity_valid
        and durable_revision_lineage_valid
        and durable_parent.get("checkpoint_status") == "superseded"
        and str(durable_parent.get("checkpoint_hash") or "") == parent_hash
        and str(durable_parent.get("superseded_by_checkpoint_id") or "") == checkpoint_id
        and str(durable_parent.get("superseded_by_checkpoint_hash") or "")
        == str(checkpoint.get("checkpoint_hash") or "")
        and str(durable_parent.get("superseded_by_revision_id") or "") == revision_id
        and str(durable_parent.get("superseded_by_revision_ref") or "") == revision_id
    )
    no_authority = (
        str(task.get("status") or "").strip().lower() == "pending"
        and checkpoint.get("checkpoint_status") == "awaiting_operator_approval"
        and checkpoint.get("dispatch_authority_created") is False
        and checkpoint.get("physical_execution_invoked") is False
        and summary.get("recovery_dispatch_request_sent") is not True
        and not _binds_current_checkpoint(operator_approval)
        and not _binds_current_checkpoint(bounded_action)
        and not receipt_has_current_authority
    )
    return (
        pending,
        no_authority,
        lineage_valid,
        {
            "task_status": str(task.get("status") or "").strip().lower(),
            "checkpoint_status": str(checkpoint.get("checkpoint_status") or "").strip(),
            "checkpoint_id": checkpoint_id,
            "checkpoint_hash": checkpoint_hash,
        },
    )


def _pending_revision_is_child_of_context(
    pending: Mapping[str, Any] | None,
    context: Mapping[str, str],
    *,
    operator_instruction_sha256: str = "",
) -> bool:
    if not isinstance(pending, Mapping):
        return False
    child_matches = (
        str(pending.get("checkpoint_id") or "") != context.get("checkpoint_id")
        and str(pending.get("checkpoint_hash") or "") != context.get("checkpoint_hash")
        and str(pending.get("parent_checkpoint_id") or "") == context.get("checkpoint_id")
        and str(pending.get("parent_checkpoint_hash") or "") == context.get("checkpoint_hash")
    )
    if not child_matches:
        return False
    if operator_instruction_sha256:
        return str(pending.get("operator_instruction_sha256") or "") == operator_instruction_sha256
    return True


def _show_concurrent_turtlebot3_recovery_revision(
    ctx: click.Context,
    *,
    task_id: str,
    pending: Mapping[str, Any],
) -> None:
    _clear_chat_recovery_revision_context(ctx)
    console.print(
        "[yellow]A different checkpoint-bound operator instruction replaced "
        "the reviewed checkpoint first. Your instruction is not claimed as "
        "accepted; no approval or dispatch was sent. Review the durable latest "
        "proposal before deciding.[/yellow]"
    )
    console.print(_render_chat_recovery_review(dict(pending)))
    _set_chat_suggestion(
        ctx,
        raw=f"/review-recovery {task_id}",
        label="review latest recovery",
    )


def _reviewed_turtlebot3_revision_binding_is_active(
    revision_state: Mapping[str, str],
    context: Mapping[str, str],
) -> bool:
    return (
        revision_state.get("task_status") == "pending"
        and revision_state.get("checkpoint_status") == "awaiting_operator_approval"
        and revision_state.get("checkpoint_id") == context.get("checkpoint_id")
        and revision_state.get("checkpoint_hash") == context.get("checkpoint_hash")
    )


def _show_unaccepted_turtlebot3_recovery_revision(
    ctx: click.Context,
    *,
    context: Mapping[str, str],
    revision_state: Mapping[str, str],
    latest_pending: Mapping[str, Any] | None,
    reason: str,
) -> None:
    if _reviewed_turtlebot3_revision_binding_is_active(revision_state, context):
        console.print(
            "[yellow]No replacement recovery checkpoint was accepted; no "
            "approval or dispatch was sent by this revision request. The reviewed "
            "checkpoint was still pending at refetch, so its exact binding "
            f"remains in revision mode. reason={rich_escape(reason)}[/yellow]"
        )
        return

    _clear_chat_recovery_revision_context(ctx)
    task_id = str(context.get("task_id") or "")
    task_status = revision_state.get("task_status") or "unknown"
    checkpoint_status = revision_state.get("checkpoint_status") or "unknown"
    console.print(
        "[yellow]The reviewed checkpoint binding is no longer active. Your "
        "revision is not claimed as accepted; this revision request did not "
        "send approval or dispatch. "
        f"task_status={rich_escape(task_status)}; "
        f"checkpoint_status={rich_escape(checkpoint_status)}; "
        f"reason={rich_escape(reason)}[/yellow]"
    )
    if (
        isinstance(latest_pending, Mapping)
        and task_status == "pending"
        and checkpoint_status == "awaiting_operator_approval"
    ):
        console.print(_render_chat_recovery_review(dict(latest_pending)))
        _set_chat_suggestion(
            ctx,
            raw=f"/review-recovery {task_id}",
            label="review latest recovery",
        )
        return
    _set_chat_suggestion(
        ctx,
        raw=f"/job-status {task_id}",
        label="check recovery task status",
    )


def _handle_chat_recovery_revision_instruction(
    ctx: click.Context,
    client: MissionOSGatewayClient,
    *,
    operator_instruction: str,
) -> bool:
    context = _chat_recovery_revision_context(ctx)
    if not context:
        return False
    operator_instruction_sha256 = hashlib.sha256(operator_instruction.encode("utf-8")).hexdigest()
    _clear_chat_suggestion(ctx)
    try:
        with console.status(
            "[cyan]Recovery Agent: revising the pending checkpoint…[/cyan]",
            spinner="dots",
        ):
            payload = client.turtlebot3_recovery_revision(
                task_id=context["task_id"],
                operator_instruction=operator_instruction,
                expected_recovery_checkpoint_id=context["checkpoint_id"],
                expected_recovery_checkpoint_hash=context["checkpoint_hash"],
            )
            if not isinstance(payload, dict):
                raise TypeError("recovery revision response must be an object")
            (
                pending,
                no_refetched_authority,
                refetched_lineage_valid,
                refetched_revision_state,
            ) = _refetched_turtlebot3_revision_state(
                client,
                task_id=context["task_id"],
            )
    except Exception as exc:
        try:
            (
                recovered_pending,
                recovered_no_authority,
                recovered_lineage_valid,
                recovered_revision_state,
            ) = _refetched_turtlebot3_revision_state(
                client,
                task_id=context["task_id"],
            )
        except Exception:
            recovered_pending = None
            recovered_no_authority = False
            recovered_lineage_valid = False
            recovered_revision_state = {}
        if (
            recovered_no_authority
            and recovered_lineage_valid
            and _pending_revision_is_child_of_context(
                recovered_pending,
                context,
            )
        ):
            if not _pending_revision_is_child_of_context(
                recovered_pending,
                context,
                operator_instruction_sha256=operator_instruction_sha256,
            ):
                _show_concurrent_turtlebot3_recovery_revision(
                    ctx,
                    task_id=context["task_id"],
                    pending=recovered_pending or {},
                )
                return True
            _clear_chat_recovery_revision_context(ctx)
            console.print(
                "[green]The response was interrupted, but the durable task shows "
                "one source-bound child checkpoint with no approval or dispatch. "
                "Reviewing that child now.[/green]"
            )
            console.print(_render_chat_recovery_review(recovered_pending))
            _set_chat_suggestion(
                ctx,
                raw=f"/review-recovery {context['task_id']}",
                label="review revised recovery",
            )
            return True
        if recovered_revision_state:
            _show_unaccepted_turtlebot3_recovery_revision(
                ctx,
                context=context,
                revision_state=recovered_revision_state,
                latest_pending=(recovered_pending if recovered_lineage_valid else None),
                reason=type(exc).__name__,
            )
            return True
        console.print(
            "[yellow]Recovery revision could not be verified; no approval or "
            "dispatch was sent by this request. The client retains only its local "
            "review binding because current task state could not be refetched. "
            f"error={rich_escape(type(exc).__name__)}[/yellow]"
        )
        return True

    durable_child_of_context = (
        no_refetched_authority
        and refetched_lineage_valid
        and _pending_revision_is_child_of_context(pending, context)
    )
    if durable_child_of_context and not _pending_revision_is_child_of_context(
        pending,
        context,
        operator_instruction_sha256=operator_instruction_sha256,
    ):
        _show_concurrent_turtlebot3_recovery_revision(
            ctx,
            task_id=context["task_id"],
            pending=pending or {},
        )
        return True
    if durable_child_of_context:
        _clear_chat_recovery_revision_context(ctx)
        console.print(
            "[green]A new checkpoint-bound recovery proposal is ready for review. "
            "No approval artifact or dispatch was created.[/green]"
        )
        console.print(_render_chat_recovery_review(pending))
        _set_chat_suggestion(
            ctx,
            raw=f"/review-recovery {context['task_id']}",
            label="review revised recovery",
        )
        return True

    summary = payload.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    blocked_reasons = [str(item) for item in summary.get("blocked_reasons") or []]
    reason_text = ", ".join(blocked_reasons) or "revision_result_not_source_bound"
    _show_unaccepted_turtlebot3_recovery_revision(
        ctx,
        context=context,
        revision_state=refetched_revision_state,
        latest_pending=pending if refetched_lineage_valid else None,
        reason=reason_text,
    )
    return True


def _has_bounded_recovery_xy(parameters: Mapping[str, Any]) -> bool:
    def _finite_number(value: Any) -> bool:
        number = _as_float(value)
        return number is not None and math.isfinite(number)

    if _finite_number(parameters.get("target_x_m")) and _finite_number(
        parameters.get("target_y_m")
    ):
        return True
    waypoints = parameters.get("recovery_waypoints")
    return (
        isinstance(waypoints, list)
        and bool(waypoints)
        and all(
            isinstance(waypoint, dict)
            and _finite_number(waypoint.get("target_x_m"))
            and _finite_number(waypoint.get("target_y_m"))
            for waypoint in waypoints
        )
    )


def _render_chat_recovery_review(pending: dict[str, Any]) -> Panel:
    parameters = pending.get("recovery_parameters")
    parameters = parameters if isinstance(parameters, dict) else {}
    parameter_text = (
        ", ".join(
            f"{key}={_recovery_command_number(value)}" for key, value in sorted(parameters.items())
        )
        if parameters
        else "-"
    )
    observations = pending.get("input_observations")
    observations = observations if isinstance(observations, dict) else {}
    evidence_keys = (
        "runtime_obstacle_observed",
        "costmap_obstacle_observed",
        "robot_motion_observed",
        "odom_delta_m",
        "motion_observation_source",
    )
    evidence_text = (
        ", ".join(
            f"{key}={_status_text(observations.get(key))}"
            for key in evidence_keys
            if observations.get(key) is not None
        )
        or "exact referenced evidence unavailable"
    )
    provider = str(pending.get("llm_provider") or "")
    model = str(pending.get("llm_model_id") or "")
    planner_text = "/".join(value for value in (provider, model) if value) or str(
        pending.get("proposal_source") or "-"
    )
    checkpoint_id = str(pending.get("checkpoint_id") or "")
    checkpoint_hash = str(pending.get("checkpoint_hash") or "")
    checkpoint_text = checkpoint_id or "not-applicable"
    if checkpoint_hash:
        checkpoint_text += f" hash={checkpoint_hash[:12]}"
    reason = str(pending.get("proposal_reason") or "").strip() or "-"
    approval_supported = pending.get("checkpoint_approval_supported") is True
    runtime_proposal_approval = pending.get("runtime_proposal_approval_supported") is True
    revision_supported = pending.get("checkpoint_revision_supported") is True
    operator_guidance_required = pending.get("operator_guidance_required") is True
    if runtime_proposal_approval:
        decision_text = (
            "[bold]y[/bold]=approve exact proposal  "
            "[bold]d/Enter[/bold]=defer with no dispatch  "
            "change unavailable for this robot profile"
        )
    elif approval_supported and revision_supported:
        decision_text = (
            "[bold]y[/bold]=approve exact checkpoint  "
            "[bold]d/Enter[/bold]=defer with no dispatch  "
            "[bold]c[/bold]=change by natural-language proposal"
        )
    elif approval_supported:
        decision_text = (
            "[bold]y[/bold]=approve exact checkpoint  "
            "[bold]d/Enter[/bold]=defer with no dispatch  "
            "change unavailable for this robot profile"
        )
    elif revision_supported and operator_guidance_required:
        decision_text = (
            "Recovery Agent needs guidance; this checkpoint cannot dispatch.  "
            "[bold]c[/bold]=give a bounded change in natural language  "
            "[bold]d/Enter[/bold]=defer with no dispatch"
        )
    else:
        decision_text = (
            "[bold]d/Enter[/bold]=defer with no dispatch  "
            "approval/change unavailable for this robot scope"
        )
    lines = [
        f"task_id={rich_escape(str(pending.get('task_id') or '-'))}",
        f"action={rich_escape(str(pending.get('recovery_action') or '-'))}",
        f"parameters={rich_escape(parameter_text)}",
        f"reason={rich_escape(reason)}",
        f"evidence={rich_escape(evidence_text)}",
        f"planner={rich_escape(planner_text)}",
        (
            "proposal=" + rich_escape(str(pending.get("recovery_proposal_id") or "not-applicable"))
            if runtime_proposal_approval
            else f"checkpoint={rich_escape(checkpoint_text)}"
        ),
        "dispatch_authority=False · physical_execution_invoked=False",
        "",
        decision_text,
    ]
    return Panel(
        "\n".join(lines),
        title="Recovery Agent Proposal Review",
        border_style="yellow",
    )


def _handle_chat_recovery_approval(
    ctx: click.Context,
    client: MissionOSGatewayClient,
    *,
    explicit_task_id: str,
    quiet_if_missing: bool = False,
    expected_checkpoint_id: str = "",
    expected_checkpoint_hash: str = "",
) -> bool:
    try:
        task_id = _resolve_operator_recovery_task_id(
            client,
            explicit_task_id=explicit_task_id,
            stored_task_id=_stored_sitl_task_id(ctx),
        )
        pending = _lookup_pending_recovery_approval(client, task_id=task_id)
    except click.ClickException as exc:
        if quiet_if_missing:
            return False
        console.print(f"[yellow]{exc.message}[/yellow]")
        return True
    if not pending:
        if quiet_if_missing:
            return False
        console.print(
            "[yellow]No pending Recovery Agent proposal requires new human approval.[/yellow]"
        )
        return True
    if pending.get("operator_guidance_required") is True:
        if _set_chat_recovery_revision_context(ctx, pending=pending):
            console.print(
                "[yellow]Recovery Agent needs guidance. Proposal cannot be approved "
                "or dispatched. Type a bounded "
                "natural-language change such as '右へ大きく迂回して障害物を避けて'. "
                "No approval artifact or dispatch was created.[/yellow]"
            )
        else:
            console.print(
                "[yellow]This proposal-only checkpoint cannot be approved or "
                "dispatched, and its revision binding is incomplete. No approval "
                "artifact or dispatch was created.[/yellow]"
            )
        return True
    if pending.get("checkpoint_approval_supported") is not True:
        console.print(
            "[yellow]Checkpoint approval is unavailable because the durable plan, "
            "current checkpoint, and summary do not all identify TurtleBot3 on "
            "ros2_nav2_turtlebot3_sim. No approval artifact or dispatch was "
            "created.[/yellow]"
        )
        return True
    current_checkpoint_id = str(pending.get("checkpoint_id") or "")
    current_checkpoint_hash = str(pending.get("checkpoint_hash") or "")
    if (expected_checkpoint_id and current_checkpoint_id != expected_checkpoint_id) or (
        expected_checkpoint_hash and current_checkpoint_hash != expected_checkpoint_hash
    ):
        console.print(
            "[yellow]The pending recovery checkpoint changed after review; "
            "no dispatch was sent. Review the latest proposal again.[/yellow]"
        )
        _set_chat_suggestion(
            ctx,
            raw=f"/review-recovery {pending.get('task_id') or task_id}",
            label="review latest recovery",
        )
        return True
    action = str(pending.get("recovery_action") or "")
    parameters = pending.get("recovery_parameters")
    parameters = parameters if isinstance(parameters, dict) else {}
    if action in {"avoid_obstacle", "reroute"} and not _has_bounded_recovery_xy(parameters):
        console.print(
            "[yellow]Pending recovery proposal is missing bounded recovery "
            "coordinates; no dispatch was sent.[/yellow]"
        )
        return True
    _clear_chat_recovery_revision_context(ctx)
    _clear_chat_back_stack(ctx)
    with console.status("[cyan]approving recovery proposal…[/cyan]", spinner="dots"):
        payload = client.recovery_dispatch(
            task_id=str(pending.get("task_id") or task_id),
            recovery_action=action,
            recovery_parameters=parameters,
            expected_recovery_checkpoint_id=(expected_checkpoint_id or current_checkpoint_id),
            expected_recovery_checkpoint_hash=(expected_checkpoint_hash or current_checkpoint_hash),
        )
        response_summary = payload.get("summary")
        response_summary = response_summary if isinstance(response_summary, dict) else {}
        blocked_reasons = [str(item) for item in response_summary.get("blocked_reasons") or []]
        reviewed_checkpoint_changed = any(
            reason.startswith("reviewed_turtlebot3_recovery_checkpoint_")
            or reason == "turtlebot3_recovery_checkpoint_claim_conflict"
            for reason in blocked_reasons
        )
        task_payload = (
            payload
            if reviewed_checkpoint_changed and isinstance(payload.get("task"), dict)
            else _wait_for_active_runner_recovery_observation(client, payload)
        )
    _print_recovery_result(payload, task_payload=task_payload)
    if reviewed_checkpoint_changed:
        console.print(
            "[yellow]The reviewed checkpoint was no longer current; no approval "
            "authority or dispatch was created. Review the latest task state.[/yellow]"
        )
        _set_chat_suggestion(
            ctx,
            raw=f"/review-recovery {pending.get('task_id') or task_id}",
            label="review latest recovery",
        )
        return True
    _set_chat_suggestion(
        ctx,
        raw=f"/job-status {pending.get('task_id') or task_id}",
        label="show status",
    )
    return True


def _handle_chat_recovery_review(
    ctx: click.Context,
    client: MissionOSGatewayClient,
    *,
    explicit_task_id: str,
) -> bool:
    try:
        task_id = _resolve_operator_recovery_task_id(
            client,
            explicit_task_id=explicit_task_id,
            stored_task_id=_stored_sitl_task_id(ctx),
        )
        pending = _lookup_pending_recovery_approval(client, task_id=task_id)
    except click.ClickException as exc:
        console.print(f"[yellow]{exc.message}[/yellow]")
        _clear_chat_suggestion(ctx)
        return True
    if not pending:
        console.print(
            "[yellow]No pending Recovery Agent proposal is available for review.[/yellow]"
        )
        _clear_chat_suggestion(ctx)
        return True
    _clear_chat_suggestion(ctx)
    console.print(_render_chat_recovery_review(pending))
    approval_supported = pending.get("checkpoint_approval_supported") is True
    revision_supported = pending.get("checkpoint_revision_supported") is True
    operator_guidance_required = pending.get("operator_guidance_required") is True
    if approval_supported and revision_supported:
        decision_prompt = "Recovery decision [y=approve, d/Enter=defer, c=change]"
    elif approval_supported:
        decision_prompt = "Recovery decision [y=approve, d/Enter=defer]"
    elif revision_supported and operator_guidance_required:
        decision_prompt = "Recovery decision [c=bounded change, d/Enter=defer]"
    else:
        decision_prompt = "Recovery decision [d/Enter=defer]"
    choice = str(
        click.prompt(
            decision_prompt,
            default="d",
            show_default=False,
        )
    ).strip()
    normalized = choice.lower()
    compact = normalized.replace(" ", "").replace("　", "")
    if compact in {"y", "yes", "はい", "承認", "承認します"}:
        if not approval_supported:
            _clear_chat_recovery_revision_context(ctx)
            if operator_guidance_required and revision_supported:
                _set_chat_recovery_revision_context(ctx, pending=pending)
                console.print(
                    "[yellow]This proposal-only checkpoint cannot be approved or "
                    "dispatched. Type a bounded natural-language change. No approval "
                    "artifact or dispatch was created.[/yellow]"
                )
            else:
                console.print(
                    "[yellow]Approval is unavailable for this robot scope; no approval "
                    "artifact or dispatch was created. Choose d/Enter to defer.[/yellow]"
                )
            return True
        _clear_chat_recovery_revision_context(ctx)
        return _handle_chat_recovery_approval(
            ctx,
            client,
            explicit_task_id=str(pending.get("task_id") or task_id),
            expected_checkpoint_id=str(pending.get("checkpoint_id") or ""),
            expected_checkpoint_hash=str(pending.get("checkpoint_hash") or ""),
        )
    if compact in {
        "",
        "d",
        "defer",
        "n",
        "no",
        "いいえ",
        "保留",
        "キャンセル",
    }:
        _clear_chat_recovery_revision_context(ctx)
        console.print(
            "[yellow]Deferred; no approval artifact or dispatch was created. "
            "The checkpoint remains pending.[/yellow]"
        )
        return True
    if compact in {"c", "change", "revise", "revision", "変更", "修正", "再提案"}:
        if not revision_supported:
            console.print(
                "[yellow]Checkpoint-bound natural-language revision is only "
                "verified for TurtleBot3 on ros2_nav2_turtlebot3_sim. No "
                "approval artifact or dispatch was created.[/yellow]"
            )
            return True
        if not _set_chat_recovery_revision_context(ctx, pending=pending):
            console.print(
                "[yellow]This proposal has no TurtleBot3 checkpoint binding, so "
                "checkpoint-bound natural-language revision is unavailable. No "
                "approval artifact or dispatch was created.[/yellow]"
            )
            return True
        console.print(
            "[yellow]Recovery revision mode is active for this exact checkpoint. "
            "The robot remains stopped and the current checkpoint remains pending. "
            "Type a natural-language alternative such as '左に大きく旋回してかわして' "
            "or '出発地点へ引き返して'. No approval or dispatch occurs "
            "until the revised checkpoint is reviewed and approved.[/yellow]"
        )
        return True
    allowed_choices = (
        "y, d/Enter, or c"
        if approval_supported and revision_supported
        else "c or d/Enter"
        if revision_supported and operator_guidance_required
        else "y or d/Enter"
        if approval_supported
        else "d/Enter"
    )
    console.print(
        "[yellow]Unknown recovery decision. Choose "
        f"{allowed_choices}. No approval artifact or dispatch was created; the "
        "checkpoint remains pending.[/yellow]"
    )
    return True


def _render_recovery_agent_console(
    task_payload: dict[str, Any],
    *,
    proposal: dict[str, Any] | None,
    show_proposal: bool,
    status: str,
    task_id: str = "",
) -> Panel:
    """Render recovery state after the CLI resolves approval-sensitive context."""
    pending = _pending_recovery_approval_from_task(task_payload)
    return _render_recovery_agent_console_view(
        task_payload,
        proposal=proposal,
        show_proposal=show_proposal,
        status=status,
        task_id=task_id,
        pending=pending,
    )


def _operate_status_group(
    client: MissionOSGatewayClient,
    task_id: str,
) -> tuple[Any, str, str]:
    task_payload, _ = _task_and_timeline(client, task_id, timeline_limit=0)
    status = _task_status(task_payload)
    proposal = _agent_proposal_from_task(task_payload)
    group, fingerprint = _build_operate_status_group_view(
        task_payload,
        proposal=proposal,
        pending=_pending_recovery_approval_from_task(task_payload),
        status=status,
        task_id=task_id,
    )
    return group, status, fingerprint


def _operate_robot_for_task(client: MissionOSGatewayClient, task_id: str) -> str:
    task_payload, _ = _task_and_timeline(client, task_id, timeline_limit=0)
    return _operate_robot_from_task_payload(task_payload)


def _handle_operate_console_command(
    client: MissionOSGatewayClient,
    task_id: str,
    command: OperateConsoleCommand,
) -> bool:
    if command.kind == "quit":
        return False
    if command.kind == "help":
        console.print(
            _operate_console_help_panel(
                task_id,
                robot=_operate_robot_for_task(client, task_id),
            )
        )
        return True
    if command.kind == "refresh":
        return True
    if command.kind == "defer_pending":
        console.print(
            "[yellow]Deferred. The robot remains stopped; no approval or dispatch "
            "was created.[/yellow]"
        )
        return True
    if command.kind == "approve_pending":
        task_payload, _ = _task_and_timeline(client, task_id, timeline_limit=0)
        pending = _pending_recovery_approval_from_task(task_payload)
        if not pending:
            raise click.ClickException("no recovery proposal is awaiting approval")
        if pending.get("operator_guidance_required") is True:
            console.print(
                "[yellow]Recovery Agent needs guidance. Proposal cannot be approved "
                "or dispatched. Type a bounded "
                "natural-language change; no approval artifact was created.[/yellow]"
            )
            return True
        action = str(pending.get("recovery_action") or "")
        if not click.confirm(f"Approve {action} for task {task_id}?", default=False):
            console.print("[yellow]Deferred; no dispatch was sent.[/yellow]")
            return True
        payload = client.recovery_dispatch(
            task_id=task_id,
            recovery_action=action,
            recovery_parameters=dict(pending.get("recovery_parameters") or {}),
            expected_recovery_checkpoint_id=str(pending.get("checkpoint_id") or ""),
            expected_recovery_checkpoint_hash=str(pending.get("checkpoint_hash") or ""),
        )
        _print_recovery_result(payload)
        return True
    if command.kind != "dispatch":
        raise click.ClickException(f"unsupported operate command kind: {command.kind}")
    action = command.action
    label = _OPERATOR_RECOVERY_ACTIONS.get(action, action)
    if not command.assume_yes and not click.confirm(
        f"Send {label} to task {task_id}?", default=False
    ):
        console.print("[yellow]Canceled; no dispatch was sent.[/yellow]")
        return True
    payload = client.recovery_dispatch(
        task_id=task_id,
        recovery_action=action,
        recovery_parameters=command.parameters or {},
    )
    task_payload = _wait_for_active_runner_recovery_observation(client, payload)
    _print_recovery_result(payload, task_payload=task_payload)
    return True


def _handle_operate_natural_language_instruction(
    client: MissionOSGatewayClient,
    task_id: str,
    operator_instruction: str,
) -> bool:
    robot = _operate_robot_for_task(client, task_id)
    if robot == "px4":
        recovery_request = _natural_language_recovery_request(
            operator_instruction
        )
        if recovery_request is None:
            return False
        with console.status(
            "[cyan]Recovery Agent: interpreting and verifying proposal…[/cyan]",
            spinner="dots",
        ):
            payload = client.recovery_agent_propose_for_task(
                task_id=task_id,
                operator_instruction=operator_instruction,
                requested_action=str(
                    recovery_request.get("requested_action") or ""
                ),
                requested_parameters=(
                    recovery_request.get("requested_parameters")
                    if isinstance(
                        recovery_request.get("requested_parameters"), dict
                    )
                    else {}
                ),
            )
        _print_recovery_agent_request_proposal(payload)
        command_raw = _recovery_proposal_command(payload)
        if command_raw:
            operate_command = command_raw.lstrip("/")
            console.print(
                "[yellow]Proposal only; nothing was approved or dispatched. "
                "Review the concrete maneuver above, then type "
                f"[bold]{rich_escape(operate_command)}[/bold] to start a separate "
                "y/N approval step.[/yellow]"
            )
        else:
            console.print(
                "[yellow]No verified bounded maneuver is available from the current "
                "telemetry. No approval artifact or dispatch was created.[/yellow]"
            )
        return True
    if robot != "turtlebot3":
        return False
    task_payload, _ = _task_and_timeline(client, task_id, timeline_limit=0)
    pending = _pending_recovery_approval_from_task(task_payload)
    if pending:
        revision_ctx = click.Context(missionos)
        revision_ctx.obj = {}
        if not _set_chat_recovery_revision_context(revision_ctx, pending=pending):
            return False
        return _handle_chat_recovery_revision_instruction(
            revision_ctx,
            client,
            operator_instruction=operator_instruction,
        )
    console.print(
        "[yellow]No Recovery decision is pending. The current Nav2 action is still "
        "running, so this console did not create a proposal, approval, or dispatch. "
        "Wait for the robot to stop and for a Recovery proposal to appear before "
        "requesting a route change.[/yellow]"
    )
    return True


def _operate_live(
    client: MissionOSGatewayClient,
    task_id: str,
    *,
    poll_interval: float,
    history_path: Path,
) -> None:
    """Run the operator UI while existing callbacks retain authority semantics."""
    run_operate_console(
        client,
        task_id,
        poll_interval=poll_interval,
        history_path=history_path,
        console=console,
        build_session=_build_operate_session,
        help_panel=_operate_console_help_panel,
        robot_for_task=_operate_robot_for_task,
        status_group=_operate_status_group,
        parse_command=_parse_operate_console_command,
        handle_command=_handle_operate_console_command,
        handle_natural_language=_handle_operate_natural_language_instruction,
        terminal_task_statuses=TERMINAL_TASK_STATUSES,
    )


@missionos.command("operate")
@click.option(
    "--task-id",
    default="",
    help="Task/job id to operate. Defaults to the task stored by `run`.",
)
@click.option(
    "--poll-interval",
    default=FLIGHT_MAP_POLL_INTERVAL,
    show_default=True,
    type=click.FloatRange(0.2, 10.0),
    help="Seconds to wait for explicit wait/sleep refresh commands.",
)
@click.option(
    "--history-path",
    default=DEFAULT_OPERATE_HISTORY_PATH,
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Persist operate-console command history.",
)
@click.pass_context
def operate_command(
    ctx: click.Context,
    task_id: str,
    poll_interval: float,
    history_path: Path,
) -> None:
    """Recovery-agent operator console. Type recovery commands here; exit with quit/Ctrl-C."""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    resolved_task_id = _resolve_live_task_id(
        client,
        explicit_task_id=task_id,
        stored_task_id=_stored_sitl_task_id(ctx),
    )
    try:
        _operate_live(
            client,
            resolved_task_id,
            poll_interval=poll_interval,
            history_path=history_path,
        )
    except KeyboardInterrupt:
        pass
    console.print("[yellow](operate stopped)[/yellow]")


def _operator_recovery_command(
    ctx: click.Context,
    *,
    task_id: str,
    action: str,
    assume_yes: bool,
    recovery_parameters: dict[str, Any] | None = None,
) -> None:
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    resolved_task_id = _resolve_operator_recovery_task_id(
        client,
        explicit_task_id=task_id,
        stored_task_id=_stored_sitl_task_id(ctx),
    )
    label = _OPERATOR_RECOVERY_ACTIONS.get(action, action)
    if not assume_yes and not click.confirm(
        f"Send {label} to task {resolved_task_id}?", default=False
    ):
        console.print("[yellow]Canceled; no dispatch was sent.[/yellow]")
        return
    payload = client.recovery_dispatch(
        task_id=resolved_task_id,
        recovery_action=action,
        recovery_parameters=recovery_parameters,
    )
    if ctx.obj["missionos_json_output"]:
        _print_json(payload)
        return
    task_payload = _wait_for_active_runner_recovery_observation(client, payload)
    _print_recovery_result(payload, task_payload=task_payload)


@missionos.command("rtl")
@click.option(
    "--task-id", default="", help="Target task. Defaults to auto-detecting a running task."
)
@click.option("--yes", is_flag=True, help="Skip y/N confirmation and send the dispatch.")
@click.pass_context
def rtl_command(ctx: click.Context, task_id: str, yes: bool) -> None:
    """Dispatch operator-approved RTL (return to launch) with standard y/N confirmation."""
    _operator_recovery_command(ctx, task_id=task_id, action="return_to_launch", assume_yes=yes)


@missionos.command("land")
@click.option(
    "--task-id", default="", help="Target task. Defaults to auto-detecting a running task."
)
@click.option("--yes", is_flag=True, help="Skip y/N confirmation and send the dispatch.")
@click.pass_context
def land_command(ctx: click.Context, task_id: str, yes: bool) -> None:
    """Dispatch operator-approved LAND with standard y/N confirmation."""
    _operator_recovery_command(ctx, task_id=task_id, action="land", assume_yes=yes)


@missionos.command("climb")
@click.option(
    "--task-id", default="", help="Target task. Defaults to auto-detecting a running task."
)
@click.option(
    "--altitude-m", required=True, type=float, help="Target altitude above home in metres."
)
@click.option("--yes", is_flag=True, help="Skip y/N confirmation and send the request.")
@click.pass_context
def climb_command(ctx: click.Context, task_id: str, altitude_m: float, yes: bool) -> None:
    """Request an operator-approved bounded altitude adjustment."""
    _operator_recovery_command(
        ctx,
        task_id=task_id,
        action="adjust_altitude",
        assume_yes=yes,
        recovery_parameters={"target_altitude_m": altitude_m},
    )


@missionos.command("speed")
@click.option(
    "--task-id", default="", help="Target task. Defaults to auto-detecting a running task."
)
@click.option(
    "--speed-mps", required=True, type=float, help="Target groundspeed in metres per second."
)
@click.option("--yes", is_flag=True, help="Skip y/N confirmation and send the request.")
@click.pass_context
def speed_command(ctx: click.Context, task_id: str, speed_mps: float, yes: bool) -> None:
    """Request an operator-approved bounded speed adjustment."""
    _operator_recovery_command(
        ctx,
        task_id=task_id,
        action="adjust_speed",
        assume_yes=yes,
        recovery_parameters={"target_speed_mps": speed_mps},
    )


@missionos.command("reroute")
@click.option(
    "--task-id", default="", help="Target task. Defaults to auto-detecting a running task."
)
@click.option("--target-x-m", required=True, type=float, help="Local NED north target in metres.")
@click.option("--target-y-m", required=True, type=float, help="Local NED east target in metres.")
@click.option(
    "--altitude-m", type=float, default=None, help="Optional target altitude above home in metres."
)
@click.option("--yes", is_flag=True, help="Skip y/N confirmation and send the request.")
@click.pass_context
def reroute_command(
    ctx: click.Context,
    task_id: str,
    target_x_m: float,
    target_y_m: float,
    altitude_m: float | None,
    yes: bool,
) -> None:
    """Request an operator-approved bounded local reroute target."""
    params: dict[str, Any] = {"target_x_m": target_x_m, "target_y_m": target_y_m}
    if altitude_m is not None:
        params["target_altitude_m"] = altitude_m
    _operator_recovery_command(
        ctx,
        task_id=task_id,
        action="reroute",
        assume_yes=yes,
        recovery_parameters=params,
    )


@missionos.command("calibrate-offboard")
@click.option(
    "--task-id",
    default="",
    help="Target live SITL task. Defaults to auto-detecting a running task.",
)
@click.option(
    "--target-x-m",
    required=True,
    type=float,
    help="Short local NED north calibration target in metres.",
)
@click.option(
    "--target-y-m",
    required=True,
    type=float,
    help="Short local NED east calibration target in metres.",
)
@click.option(
    "--altitude-m",
    required=True,
    type=float,
    help="Target altitude above home in metres.",
)
@click.option(
    "--yes",
    is_flag=True,
    help="Skip y/N confirmation and send the explicitly approved calibration.",
)
@click.pass_context
def calibrate_offboard_command(
    ctx: click.Context,
    task_id: str,
    target_x_m: float,
    target_y_m: float,
    altitude_m: float,
    yes: bool,
) -> None:
    """Run one SITL-only bounded OFFBOARD performance calibration."""

    _operator_recovery_command(
        ctx,
        task_id=task_id,
        action="calibrate_offboard",
        assume_yes=yes,
        recovery_parameters={
            "target_x_m": target_x_m,
            "target_y_m": target_y_m,
            "target_altitude_m": altitude_m,
        },
    )


@missionos.command("avoid-obstacle")
@click.option(
    "--task-id", default="", help="Target task. Defaults to auto-detecting a running task."
)
@click.option(
    "--target-x-m",
    required=True,
    type=float,
    help="Obstacle-aware local NED north target in metres.",
)
@click.option(
    "--target-y-m",
    required=True,
    type=float,
    help="Obstacle-aware local NED east target in metres.",
)
@click.option(
    "--altitude-m", type=float, default=None, help="Optional target altitude above home in metres."
)
@click.option("--yes", is_flag=True, help="Skip y/N confirmation and send the request.")
@click.pass_context
def avoid_obstacle_command(
    ctx: click.Context,
    task_id: str,
    target_x_m: float,
    target_y_m: float,
    altitude_m: float | None,
    yes: bool,
) -> None:
    """Request an operator-approved obstacle-avoidance reroute target."""
    params: dict[str, Any] = {
        "target_x_m": target_x_m,
        "target_y_m": target_y_m,
    }
    if altitude_m is not None:
        params["target_altitude_m"] = altitude_m
    _operator_recovery_command(
        ctx,
        task_id=task_id,
        action="avoid_obstacle",
        assume_yes=yes,
        recovery_parameters=params,
    )


def _tutorial_status(
    ctx: click.Context, client: MissionOSGatewayClient, session_id: str
) -> TutorialOutcome:
    payloads = {
        "health": client.health(),
        "form2a": client.get("/missionos/form2a-response-selection"),
        "review": client.get("/missionos/form2a-operator-review"),
        "action": client.get("/missionos/form2a-action-consumption"),
        "repair": client.get("/missionos/llm-repair-planner"),
    }
    _print_status(payloads, base_url=ctx.obj["missionos_gateway_url"])
    return None


def _tutorial_plan(
    ctx: click.Context, client: MissionOSGatewayClient, session_id: str
) -> TutorialOutcome:
    payload = client.conversation(
        TUTORIAL_PLAN_INSTRUCTION,
        session_id=session_id,
        mission_designer_context=_stored_mission_designer_context(ctx, session_id),
        coordinate_route=dict(FUJI_DELIVERY_ROUTE),
        route_hint="mission_designer_plan",
    )
    _remember_mission_designer_context(ctx, payload, session_id=session_id)
    _print_conversation_result(payload)
    return None


def _tutorial_intent(
    intent: str,
) -> Callable[[click.Context, MissionOSGatewayClient, str], TutorialOutcome]:
    def _action(
        ctx: click.Context, client: MissionOSGatewayClient, session_id: str
    ) -> TutorialOutcome:
        payload = client.conversation(
            INTENT_INSTRUCTIONS[intent],
            session_id=session_id,
            mission_designer_context=_stored_mission_designer_context(ctx, session_id),
            route_hint=INTENT_ROUTE_HINTS[intent],
        )
        _remember_mission_designer_context(ctx, payload, session_id=session_id)
        _print_conversation_result(payload)
        return None

    return _action


def _tutorial_resolve_task_id(ctx: click.Context) -> str:
    task_id = _stored_sitl_task_id(ctx)
    if not task_id:
        raise click.ClickException(
            "No prepared SITL task id is stored. The run step must return a task first."
        )
    return task_id


def _tutorial_start_sitl(
    ctx: click.Context, client: MissionOSGatewayClient, session_id: str
) -> TutorialOutcome:
    task_id = _tutorial_resolve_task_id(ctx)
    payload = client.start_sitl(task_id=task_id)
    _remember_sitl_task_id(ctx, task_id)
    _print_sitl_start_result(payload)
    return None


def _tutorial_execute_sitl(
    ctx: click.Context,
    client: MissionOSGatewayClient,
    session_id: str,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> TutorialOutcome:
    task_id = _tutorial_resolve_task_id(ctx)
    payload, task_payload, timeline_payload = _execute_sitl_with_task_polling(
        client,
        task_id=task_id,
        live_flight_mode=True,
        progress_callback=progress_callback,
    )
    _remember_sitl_task_id(ctx, task_id)
    if payload is None and task_payload is not None and timeline_payload is not None:
        console.print(f"[yellow]{LIVE_SITL_RESPONSE_WAIT_EXCEEDED_MESSAGE}[/yellow]")
        _print_job_status(task_payload, timeline_payload)
        return _task_status(task_payload)
    _print_sitl_execution_result(payload)
    if isinstance(payload, dict):
        summary = payload.get("summary")
        task_payload = payload.get("task")
        if isinstance(summary, dict):
            return str(summary.get("task_status") or summary.get("live_flight_status") or "")
        if isinstance(task_payload, dict):
            return _task_status(task_payload)
    return None


def build_tutorial_steps() -> list[TutorialStep]:
    """The ordered Fuji-delivery CLI walkthrough."""
    return _build_tutorial_steps_impl(
        status_action=_tutorial_status,
        plan_action=_tutorial_plan,
        approve_action=_tutorial_intent("approve"),
        run_action=_tutorial_intent("run"),
        start_sitl_action=_tutorial_start_sitl,
        execute_sitl_action=_tutorial_execute_sitl,
    )


def run_fuji_tutorial(
    ctx: click.Context,
    client: MissionOSGatewayClient,
    *,
    session_id: str,
    interactive: bool,
    allow_live: bool,
    reader: TutorialReader | None = None,
) -> None:
    """Drive the guided Fuji-delivery walkthrough.

    Non-live steps run on Enter (interactive) or automatically (auto mode). The
    live Execute Live SITL step never fires without an explicit human 'yes' in
    interactive mode, or the --yes/allow_live opt-in in auto mode.
    """
    run_tutorial_steps(
        ctx,
        client,
        session_id=session_id,
        steps=build_tutorial_steps(),
        interactive=interactive,
        allow_live=allow_live,
        terminal_task_statuses=TERMINAL_TASK_STATUSES,
        progress_status_text=_job_progress_status_text,
        reader=reader,
    )


@missionos.command("tutorial")
@click.option("--session-id", default=DEFAULT_TUTORIAL_SESSION_ID, show_default=True)
@click.option(
    "--auto",
    is_flag=True,
    help="Run each step without pauses (for guided demos).",
)
@click.option(
    "--yes",
    "allow_live",
    is_flag=True,
    help="Allow the live SITL execution step in --auto mode (default stops before live execution).",
)
@click.option(
    "--autostart/--no-autostart",
    default=False,
    show_default=True,
    help="Autostart the Gateway when it is not running, then stop it on exit.",
)
@click.option(
    "--enable-live-sitl/--planning-only",
    default=False,
    show_default=True,
    help="Enable live SITL/dispatch opt-in env for an autostarted Gateway.",
)
@click.pass_context
def tutorial_command(
    ctx: click.Context,
    session_id: str,
    auto: bool,
    allow_live: bool,
    autostart: bool,
    enable_live_sitl: bool,
) -> None:
    """Experimental guided walkthrough; not the public quickstart."""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    gateway_proc = _ensure_gateway(
        client,
        ctx.obj["missionos_gateway_url"],
        autostart=autostart,
        enable_live_sitl=enable_live_sitl,
    )
    try:
        run_fuji_tutorial(
            ctx,
            client,
            session_id=session_id,
            interactive=not auto,
            allow_live=allow_live,
        )
    finally:
        if gateway_proc is not None:
            console.print("[blue]Stopping the autostarted Gateway...[/blue]")
            _terminate_gateway(gateway_proc)


def _stop_chat_companion_terminals(ctx: click.Context) -> None:
    _stop_chat_companion_terminals_impl(
        ctx,
        close_terminals=_close_macos_companion_terminal_titles,
    )


def _ensure_chat_companion_terminals(ctx: click.Context, task_id: str) -> None:
    _ensure_chat_companion_terminals_impl(
        ctx,
        task_id,
        terminal_root=CHAT_COMPANION_TERMINAL_ROOT,
        terminal_surfaces=CHAT_COMPANION_TERMINAL_SURFACES,
        terminals_enabled=_chat_companion_terminals_enabled,
        stop_existing=_stop_chat_companion_terminals,
        launch_terminal=_launch_macos_terminal_script,
    )


def _maybe_open_turtlebot3_companion_terminals(
    ctx: click.Context,
    payload: dict[str, Any],
) -> None:
    _maybe_open_home_robot_companion_terminals_impl(
        ctx,
        payload,
        is_home_robot_execution_target=_is_home_robot_nav2_execution_target,
        payload_task_id=_payload_task_id,
        ensure_terminals=_ensure_chat_companion_terminals,
    )


def _listed_home_robot_task_ids(client: MissionOSGatewayClient) -> set[str] | None:
    return _listed_home_robot_task_ids_impl(
        client,
        is_home_robot_task_artifacts=_is_turtlebot3_task_artifacts,
    )


def _run_turtlebot3_conversation_with_companion_monitor(
    ctx: click.Context,
    client: MissionOSGatewayClient,
    operation: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    return _run_home_robot_conversation_with_companion_monitor_impl(
        ctx,
        client,
        operation,
        terminals_enabled=_chat_companion_terminals_enabled,
        is_home_robot_task_artifacts=_is_turtlebot3_task_artifacts,
        remember_task_id=_remember_sitl_task_id,
        ensure_terminals=_ensure_chat_companion_terminals,
    )


def _start_turtlebot3_chat_task_status_monitor(
    ctx: click.Context,
    client: MissionOSGatewayClient,
    *,
    task_id: str,
) -> None:
    _start_turtlebot3_chat_task_status_monitor_impl(
        ctx,
        client,
        task_id=task_id,
        print_terminal_update=_print_turtlebot3_chat_task_terminal_update,
    )


def _maybe_start_turtlebot3_chat_task_status_monitor(
    ctx: click.Context,
    client: MissionOSGatewayClient,
    payload: dict[str, Any],
) -> None:
    _maybe_start_home_robot_chat_task_status_monitor_impl(
        ctx,
        client,
        payload,
        payload_task_id=_payload_task_id,
        stored_task_id=_stored_sitl_task_id,
        start_monitor=_start_turtlebot3_chat_task_status_monitor,
    )


def _conversation_has_approvable_plan(payload: dict[str, Any]) -> bool:
    mission_designer = payload.get("mission_designer")
    mission_designer = mission_designer if isinstance(mission_designer, dict) else {}
    mission_summary = (
        mission_designer.get("summary") if isinstance(mission_designer.get("summary"), dict) else {}
    )
    mission_context_ref = str(
        mission_designer.get("mission_designer_context_ref")
        or mission_summary.get("mission_designer_context_ref")
        or ""
    ).strip()
    mission_context_sha = str(
        mission_designer.get("mission_designer_context_sha256")
        or mission_summary.get("mission_designer_context_sha256")
        or ""
    ).strip()
    if (
        isinstance(mission_designer.get("scenario_proposal"), dict)
        and isinstance(mission_designer.get("validation_result"), dict)
        and mission_context_ref
        and mission_context_sha
    ):
        return True

    selection = payload.get("selection")
    selection = selection if isinstance(selection, dict) else {}
    if str(selection.get("summary_status") or "").lower() == "form2a_response_selected":
        return True

    operation = payload.get("operation_result")
    operation = operation if isinstance(operation, dict) else {}
    if operation.get("error"):
        return False
    if str(operation.get("summary_status") or "").lower() == "form2a_response_selected":
        return True
    return False


def _conversation_should_advance_suggestion(payload: dict[str, Any]) -> bool:
    action = str(payload.get("routed_action") or payload.get("route") or "")
    message = str(payload.get("message") or "").lower()
    if any(
        marker in message
        for marker in (
            "cannot use",
            "not source-bound",
            "not source bound",
        )
    ):
        return False
    if action == "approve" and "did not approve" in message:
        return False
    if action == "run" and "did not prepare" in message:
        return False
    operation = payload.get("operation_result")
    operation = operation if isinstance(operation, dict) else {}
    summary = operation.get("summary") if isinstance(operation.get("summary"), dict) else {}
    status = str(
        summary.get("status")
        or operation.get("summary_status")
        or operation.get("response_status")
        or ""
    ).lower()
    return not any(marker in status for marker in ("blocked", "failed", "rejected"))


def _update_chat_suggestion_from_conversation(
    ctx: click.Context,
    payload: dict[str, Any],
    client: MissionOSGatewayClient | None = None,
) -> None:
    if not _conversation_should_advance_suggestion(payload):
        _clear_chat_suggestion(ctx)
        return
    action = str(payload.get("routed_action") or payload.get("route") or "")
    repair_prompt = payload.get("missionos_repair_prompt")
    if isinstance(repair_prompt, dict) and repair_prompt.get("suggested_command") == "/repair":
        _set_chat_suggestion(ctx, raw="/repair", label="repair")
        return
    if action in {"fixture_plan", "mission_designer_plan", "plan"}:
        if _conversation_has_approvable_plan(payload):
            _set_chat_suggestion(ctx, raw="/approve", label="approve")
        else:
            _clear_chat_suggestion(ctx)
    elif action == "approve":
        _set_chat_suggestion(ctx, raw="/run", label="prepare")
    elif action == "execute" and _stored_sitl_task_id(ctx):
        operation = payload.get("operation_result")
        operation = operation if isinstance(operation, dict) else {}
        summary = operation.get("summary") if isinstance(operation.get("summary"), dict) else {}
        if _is_home_robot_nav2_execution_target(summary.get("execution_target")):
            task_id = _stored_sitl_task_id(ctx)
            pending = (
                _lookup_pending_recovery_approval(client, task_id=task_id)
                if isinstance(client, MissionOSGatewayClient)
                else _pending_recovery_approval_from_task(
                    {
                        "task_id": task_id,
                        "status": operation.get("task_status")
                        or operation.get("summary_status")
                        or "",
                        "artifacts": {
                            "summary": summary,
                            "turtlebot3_recovery_decision_summary": summary.get(
                                "turtlebot3_recovery_decision_summary"
                            )
                            if isinstance(
                                summary.get("turtlebot3_recovery_decision_summary"),
                                dict,
                            )
                            else {},
                        },
                    }
                )
            )
            if pending:
                console.print(_render_chat_recovery_review(pending))
                _set_chat_suggestion(
                    ctx,
                    raw=f"/review-recovery {task_id}",
                    label="review recovery",
                )
            else:
                _set_chat_suggestion(ctx, raw=f"/map {task_id}", label="map")
        else:
            _set_chat_suggestion(ctx, raw="/start-sitl", label="start")
    else:
        _clear_chat_suggestion(ctx)


def _handle_chat_input(
    ctx: click.Context,
    client: MissionOSGatewayClient,
    raw: str,
    *,
    session_id: str,
) -> bool:
    """Process one chat line. Return False to exit the loop."""
    robot_profile = str(ctx.obj.get("missionos_chat_robot_profile") or "")
    raw = raw.strip()
    if not raw:
        revision_context = _chat_recovery_revision_context(ctx)
        if revision_context:
            console.print(
                "[yellow]Recovery revision mode remains active; Enter does not "
                "approve, dispatch, or alter the pending checkpoint. Type a "
                "natural-language alternative.[/yellow]"
            )
            return True
        suggestion = _chat_suggestion(ctx)
        if not suggestion:
            return True
        raw = suggestion["raw"]
        console.print(f"[dim]Enter -> {suggestion['label']}[/dim]")
    else:
        revision_context = _chat_recovery_revision_context(ctx)
        if revision_context and raw == "/back":
            _clear_chat_recovery_revision_context(ctx)
            _set_chat_suggestion(
                ctx,
                raw=f"/review-recovery {revision_context['task_id']}",
                label="review pending recovery",
            )
            console.print(
                "[yellow]Exited recovery revision mode. No approval artifact or "
                "dispatch was created; the checkpoint remains pending.[/yellow]"
            )
            return True
        if revision_context and not raw.startswith("/"):
            _clear_chat_suggestion(ctx)
            return _handle_chat_recovery_revision_instruction(
                ctx,
                client,
                operator_instruction=raw,
            )
        if _is_chat_back_request(raw):
            raw = "/back"
        else:
            _clear_chat_suggestion(ctx)
    if raw.startswith("missionos "):
        try:
            parts = shlex.split(raw)
        except ValueError:
            parts = raw.split()
        if parts and parts[0] == "missionos":
            args = parts[1:]
            if args and args[0] in {"--json", "--gateway-url", "--timeout", "--state-path"}:
                console.print(
                    "[yellow]Inside MissionOS chat, use slash commands such as /approve or /run.[/yellow]"
                )
                return True
            if len(args) == 1 and args[0] in INTENT_INSTRUCTIONS:
                raw = f"/{args[0]}"
            elif args and args[0] in {"start-sitl", "execute-sitl", "job-status"}:
                raw = "/" + " ".join(args)
            elif args and args[0] == "recover":
                console.print(
                    "[yellow]Inside MissionOS chat, use /land <task_id> or /rtl <task_id>.[/yellow]"
                )
                return True
            elif args and args[0] in {"say", "chat"}:
                raw = " ".join(args[1:]).strip()
                if not raw:
                    console.print(
                        "[yellow]Type the instruction directly inside MissionOS chat.[/yellow]"
                    )
                    return True
            else:
                console.print(
                    "[yellow]Inside MissionOS chat, use slash commands such as /approve or /run.[/yellow]"
                )
                return True
    if raw in {"/quit", "/exit", "exit", "quit", "q"}:
        return False
    if not raw.startswith("/"):
        stored_task_id = _stored_sitl_task_id(ctx)
        lower = raw.lower()
        if any(token in lower for token in ("prepare", "ready", "execution request")):
            raw = "/run"
        elif any(token in lower for token in ("start", "boot", "bring up")):
            raw = "/start-sitl"
        elif stored_task_id and any(
            token in lower for token in ("fly", "launch", "execute live", "start live")
        ):
            raw = "/execute-sitl"
        elif stored_task_id and any(
            token in lower for token in ("map", "地図", "軌跡", "route trace")
        ):
            raw = "/map"
        elif stored_task_id and any(
            token in lower for token in ("status", "progress", "show status")
        ):
            raw = "/job-status"
        elif _is_recovery_approval_text(raw):
            if _handle_chat_recovery_approval(
                ctx,
                client,
                explicit_task_id="",
                quiet_if_missing=True,
            ):
                return True
            raw = "/approve"
        elif _is_chat_back_request(raw):
            raw = "/back"
    if raw == "/help":
        console.print(_chat_help_panel())
        return True
    if raw == "/back":
        if _restore_chat_back_state(ctx):
            suggestion = _chat_suggestion(ctx)
            next_text = (
                f"Returned to the previous chat step. Press Enter for {suggestion['label']}, "
                "or type a different instruction."
                if suggestion
                else "Returned to the previous chat step. Type a new instruction to continue."
            )
            _print_chat_followup(
                next_text + " Already-sent Gateway/simulator actions are not undone by /back."
            )
        else:
            _print_chat_followup(
                "No previous reversible chat step is available. External actions already sent "
                "to the Gateway or simulator cannot be undone with /back."
            )
        return True
    if raw == "/clear":
        console.clear()
        return True
    try:
        if raw == "/status":
            ctx.invoke(status_command)
            return True
        if raw.startswith("/review-recovery"):
            parts = shlex.split(raw)
            if len(parts) > 2:
                console.print("[yellow]Usage: /review-recovery [task_id][/yellow]")
                return True
            task_id = parts[1] if len(parts) == 2 else ""
            return _handle_chat_recovery_review(
                ctx,
                client,
                explicit_task_id=task_id,
            )
        if raw.startswith("/approve-recovery"):
            parts = shlex.split(raw)
            if len(parts) > 2:
                console.print("[yellow]Usage: /approve-recovery [task_id][/yellow]")
                return True
            task_id = parts[1] if len(parts) == 2 else ""
            return _handle_chat_recovery_approval(
                ctx,
                client,
                explicit_task_id=task_id,
            )
        if raw.startswith("/land ") or raw.startswith("/rtl "):
            parts = shlex.split(raw)
            if len(parts) != 2:
                console.print("[yellow]Usage: /land <task_id> or /rtl <task_id>[/yellow]")
                return True
            _clear_chat_back_stack(ctx)
            action = "land" if parts[0] == "/land" else "return_to_launch"
            with console.status("[cyan]dispatching recovery…[/cyan]", spinner="dots"):
                payload = client.recovery_dispatch(task_id=parts[1], recovery_action=action)
                task_payload = _wait_for_active_runner_recovery_observation(client, payload)
            _print_recovery_result(payload, task_payload=task_payload)
            return True
        if raw.startswith(("/climb", "/speed", "/reroute", "/avoid ", "/avoid-obstacle")):
            try:
                parts = shlex.split(raw)
            except ValueError as exc:
                console.print(f"[red]could not parse recovery command: {exc}[/red]")
                return True
            if not parts:
                return True
            command_text = " ".join([parts[0].lstrip("/"), *parts[1:]])
            try:
                command = _parse_operate_console_command(command_text)
            except click.ClickException as exc:
                console.print(f"[red]{exc.message}[/red]")
                return True
            task_id = _resolve_operator_recovery_task_id(
                client,
                explicit_task_id="",
                stored_task_id=_stored_sitl_task_id(ctx),
            )
            if command.kind != "dispatch":
                console.print("[yellow]Usage: /climb, /speed, /reroute, or /avoid[/yellow]")
                return True
            _clear_chat_back_stack(ctx)
            if _handle_operate_console_command(client, task_id, command):
                _set_chat_suggestion(ctx, raw=f"/job-status {task_id}", label="show status")
            return True
        if raw.startswith("/map"):
            parts = shlex.split(raw)
            if len(parts) > 2:
                console.print("[yellow]Usage: /map [task_id][/yellow]")
                return True
            task_id = parts[1] if len(parts) == 2 else _stored_sitl_task_id(ctx)
            if task_id and _chat_companion_terminals_enabled(ctx):
                _ensure_chat_companion_terminals(ctx, task_id)
                console.print(
                    "[cyan]The authenticated live map is the map companion for "
                    f"{task_id}. /map will not open a stale file:// snapshot.[/cyan]"
                )
                _set_chat_suggestion(
                    ctx,
                    raw=f"/job-status {task_id}",
                    label="show status",
                )
                return True
            try:
                ctx.invoke(
                    map_command,
                    task_id=task_id or "",
                    provider="osm",
                    output_path=None,
                    poll_interval=MISSION_MAP_POLL_INTERVAL,
                    snapshot=False,
                    serve_live=True,
                    no_open=False,
                )
            except click.ClickException as exc:
                console.print(f"[red]{exc.message}[/red]")
                return True
            if task_id:
                _set_chat_suggestion(ctx, raw=f"/job-status {task_id}", label="show status")
            return True
        if raw.startswith("/execute-sitl"):
            parts = shlex.split(raw)
            if len(parts) > 2:
                console.print("[yellow]Usage: /execute-sitl [task_id][/yellow]")
                return True
            task_id = parts[1] if len(parts) == 2 else _stored_sitl_task_id(ctx)
            if not task_id:
                console.print(
                    "[yellow]No stored task id; run /run or pass /execute-sitl <task_id>[/yellow]"
                )
                return True
            _clear_chat_back_stack(ctx)
            _ensure_chat_companion_terminals(ctx, task_id)
            with console.status("[green]executing SITL…[/green]", spinner="dots") as status:
                payload, task_payload, timeline_payload = _execute_sitl_with_task_polling(
                    client,
                    task_id=task_id,
                    live_flight_mode=True,
                    progress_callback=lambda latest: status.update(
                        f"[green]{_job_progress_status_text(latest)}[/green]"
                    ),
                )
            if payload is None and task_payload is not None and timeline_payload is not None:
                latest_task_id = _remember_sitl_task_id_from_payload(
                    ctx,
                    task_payload,
                    fallback_task_id=task_id,
                )
                console.print(f"[yellow]{LIVE_SITL_RESPONSE_WAIT_EXCEEDED_MESSAGE}[/yellow]")
                _print_job_status(task_payload, timeline_payload)
                latest_task = task_payload.get("task")
                latest_status = (
                    str(latest_task.get("status") or "").strip().lower()
                    if isinstance(latest_task, dict)
                    else ""
                )
                followup = (
                    "You can inspect the final state if needed. Type 'show status' to view it."
                    if latest_status in TERMINAL_TASK_STATUSES
                    else "The AUTO mission is still running. Type 'show status' to view progress."
                )
                _print_chat_followup(followup)
                _set_chat_suggestion(ctx, raw=f"/job-status {latest_task_id}", label="show status")
                return True
            latest_task_id = _remember_sitl_task_id_from_payload(
                ctx,
                payload,
                fallback_task_id=task_id,
            )
            _print_sitl_execution_result(payload)
            _print_chat_followup(
                "You can inspect the final state if needed. Type 'show status' to view it."
            )
            _set_chat_suggestion(ctx, raw=f"/job-status {latest_task_id}", label="show status")
            return True
        if raw.startswith("/start-sitl"):
            parts = shlex.split(raw)
            if len(parts) > 2:
                console.print("[yellow]Usage: /start-sitl [task_id][/yellow]")
                return True
            task_id = parts[1] if len(parts) == 2 else _stored_sitl_task_id(ctx)
            if not task_id:
                console.print(
                    "[yellow]No stored task id; run /run or pass /start-sitl <task_id>[/yellow]"
                )
                return True
            _clear_chat_back_stack(ctx)
            with console.status("[blue]starting SITL…[/blue]", spinner="dots"):
                payload = client.start_sitl(task_id=task_id)
            latest_task_id = _remember_sitl_task_id_from_payload(
                ctx,
                payload,
                fallback_task_id=task_id,
            )
            _print_sitl_start_result(payload)
            _print_chat_followup("SITL is ready. Start live execution? Type 'fly' to proceed.")
            _set_chat_suggestion(ctx, raw=f"/execute-sitl {latest_task_id}", label="fly")
            return True
        if raw.startswith("/job-status"):
            parts = shlex.split(raw)
            if len(parts) > 2:
                console.print("[yellow]Usage: /job-status [task_id][/yellow]")
                return True
            task_id = parts[1] if len(parts) == 2 else _stored_sitl_task_id(ctx)
            if not task_id:
                console.print(
                    "[yellow]No stored task id; run /run or pass /job-status <task_id>[/yellow]"
                )
                return True
            encoded_task_id = quote(task_id, safe="")
            with console.status("[magenta]checking job status…[/magenta]", spinner="dots"):
                task_payload = client.get(f"/tasks/{encoded_task_id}")
                timeline_payload = client.get(f"/tasks/{encoded_task_id}/timeline?limit=8")
            _print_job_status(task_payload, timeline_payload)
            task = task_payload.get("task") if isinstance(task_payload.get("task"), dict) else {}
            if str(task.get("status") or "").lower() in {"running", "pending"}:
                _set_chat_suggestion(ctx, raw="/job-status", label="refresh")
            else:
                _clear_chat_suggestion(ctx)
            return True
        if not raw.startswith("/") and not _looks_like_mission_planning_request(raw):
            recovery_request = _natural_language_recovery_request(raw)
            if recovery_request is not None:
                task_id = _resolve_operator_recovery_task_id(
                    client,
                    explicit_task_id="",
                    stored_task_id=_stored_sitl_task_id(ctx),
                )
                with console.status(
                    "[cyan]Recovery Agent: planning maneuver…[/cyan]",
                    spinner="dots",
                ):
                    payload = client.recovery_agent_propose_for_task(
                        task_id=task_id,
                        operator_instruction=raw,
                        requested_action=str(recovery_request.get("requested_action") or ""),
                        requested_parameters=(
                            recovery_request.get("requested_parameters")
                            if isinstance(recovery_request.get("requested_parameters"), dict)
                            else {}
                        ),
                    )
                _print_recovery_agent_request_proposal(payload)
                command_raw = _recovery_proposal_command(payload)
                if command_raw:
                    _set_chat_suggestion(
                        ctx,
                        raw=command_raw,
                        label="review recovery",
                    )
                    _print_chat_followup(
                        "Press Enter to review the proposed recovery command; "
                        "dispatch still asks for y/N confirmation."
                    )
                else:
                    _clear_chat_suggestion(ctx)
                return True
        if raw.startswith("/"):
            intent = raw[1:]
            if intent in INTENT_INSTRUCTIONS:
                _push_chat_back_state(ctx)
                with console.status(f"[cyan]MissionOS: {intent}…[/cyan]", spinner="dots"):

                    def conversation() -> dict[str, Any]:
                        return client.conversation(
                            INTENT_INSTRUCTIONS[intent],
                            session_id=session_id,
                            mission_designer_context=_stored_mission_designer_context(
                                ctx, session_id
                            ),
                            route_hint=INTENT_ROUTE_HINTS[intent],
                            client_surface="chat",
                            robot_profile=robot_profile or None,
                        )

                    payload = (
                        _run_turtlebot3_conversation_with_companion_monitor(
                            ctx,
                            client,
                            conversation,
                        )
                        if robot_profile == "turtlebot3" and intent == "run"
                        else conversation()
                    )
                _remember_mission_designer_context(ctx, payload, session_id=session_id)
                _maybe_open_turtlebot3_companion_terminals(ctx, payload)
                _print_conversation_result(payload)
                if robot_profile == "turtlebot3" and intent == "run":
                    _maybe_start_turtlebot3_chat_task_status_monitor(
                        ctx,
                        client,
                        payload,
                    )
                _update_chat_suggestion_from_conversation(ctx, payload, client)
                return True
            console.print(
                "[yellow]Unknown command. Type /help for the slash-command list.[/yellow]"
            )
            return True
        _push_chat_back_state(ctx)
        route_hint = "mission_designer_plan" if _looks_like_mission_planning_request(raw) else None
        with console.status("[cyan]MissionOS…[/cyan]", spinner="dots"):
            payload = client.conversation(
                raw,
                session_id=session_id,
                mission_designer_context=_stored_mission_designer_context(ctx, session_id),
                route_hint=route_hint,
                client_surface="chat",
                robot_profile=robot_profile or None,
            )
        _remember_mission_designer_context(ctx, payload, session_id=session_id)
        _maybe_open_turtlebot3_companion_terminals(ctx, payload)
        _print_conversation_result(payload)
        _update_chat_suggestion_from_conversation(ctx, payload, client)
    except click.ClickException as exc:
        console.print(f"[red]{exc.message}[/red]")
    return True


def _build_chat_session(history_path: Path) -> PromptSession[str]:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.touch(exist_ok=True)
    bindings = KeyBindings()

    @bindings.add(Keys.Escape, Keys.Enter)
    def _(event):  # type: ignore[no-redef]
        event.current_buffer.insert_text("\n")

    return PromptSession(
        history=FileHistory(str(history_path)),
        auto_suggest=AutoSuggestFromHistory(),
        completer=WordCompleter(list(CHAT_SLASH_COMMANDS), ignore_case=True),
        complete_while_typing=True,
        multiline=False,
        key_bindings=bindings,
        mouse_support=False,
    )


def _chat_initial_instruction_and_autostart(
    initial_instruction: tuple[str, ...],
    *,
    autostart: bool,
    enable_live_sitl: bool,
) -> tuple[str, bool, bool]:
    text = " ".join(str(part) for part in initial_instruction).strip()
    while True:
        option_match = re.search(
            r"(?:\s|\u3000)+(--autostart|--no-autostart|--enable-live-sitl|--planning-only)\s*$",
            text,
        )
        if not option_match:
            return text, autostart, enable_live_sitl
        option = option_match.group(1)
        text = text[: option_match.start()].strip()
        if option == "--autostart":
            autostart = True
        elif option == "--no-autostart":
            autostart = False
        elif option == "--enable-live-sitl":
            enable_live_sitl = True
        elif option == "--planning-only":
            enable_live_sitl = False


def _maybe_retarget_turtlebot3_gateway_url(ctx: click.Context) -> None:
    """Preserve the CLI seam while delegating local Gateway retargeting."""
    _turtlebot3_runtime._maybe_retarget_turtlebot3_gateway_url(
        ctx,
        gateway_reachable=_gateway_reachable,
    )


@missionos.command("chat")
@click.argument("initial_instruction", nargs=-1, required=False)
@click.option(
    "--robot",
    type=click.Choice(
        ["default", "turtlebot3", "turtlebot4", "nova-carter"],
        case_sensitive=False,
    ),
    default="default",
    show_default=True,
    help="Route chat through a robot-specific simulator entrypoint.",
)
@click.option(
    "--turtlebot3-build-image/--no-turtlebot3-build-image",
    default=False,
    show_default=True,
    help="Build the TurtleBot3 Docker image before running --robot turtlebot3.",
)
@click.option(
    "--turtlebot3-mid-recovery/--no-turtlebot3-mid-recovery",
    default=False,
    show_default=True,
    help="Run the TurtleBot3 mid-mission low-battery recovery smoke.",
)
@click.option(
    "--turtlebot3-dry-run",
    is_flag=True,
    help="Print the TurtleBot3 simulator/Gateway command without starting Docker.",
)
@click.option(
    "--turtlebot3-smoke",
    is_flag=True,
    help=(
        "Run the non-interactive TurtleBot3 Docker smoke. By default, "
        "`--robot turtlebot3` uses normal MissionOS chat plus operate/watch/map."
    ),
)
@click.option("--session-id", default=DEFAULT_SESSION_ID, show_default=True)
@click.option(
    "--history-path",
    default=DEFAULT_HISTORY_PATH,
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Persist input history between chat sessions (Claude-Code-like ↑/↓).",
)
@click.option(
    "--autostart/--no-autostart",
    default=False,
    show_default=True,
    help="Autostart the Gateway when it is not running, then stop it when chat exits.",
)
@click.option(
    "--enable-live-sitl/--planning-only",
    default=False,
    show_default=True,
    help="Enable live SITL/dispatch opt-in env for an autostarted Gateway.",
)
@click.option(
    "--companion-terminals/--no-companion-terminals",
    default=True,
    show_default=True,
    help=(
        "When an interactive chat starts live flight, open operate/watch/map "
        "companion terminals and close them when chat exits."
    ),
)
@click.pass_context
def chat_command(
    ctx: click.Context,
    initial_instruction: tuple[str, ...],
    robot: str,
    turtlebot3_build_image: bool,
    turtlebot3_mid_recovery: bool,
    turtlebot3_dry_run: bool,
    turtlebot3_smoke: bool,
    session_id: str,
    history_path: Path,
    autostart: bool,
    enable_live_sitl: bool,
    companion_terminals: bool,
) -> None:
    """Start a text-first MissionOS operator session."""
    initial_raw, autostart, enable_live_sitl = _chat_initial_instruction_and_autostart(
        initial_instruction,
        autostart=autostart,
        enable_live_sitl=enable_live_sitl,
    )
    robot_profile = robot.lower()
    if robot_profile == "nova-carter":
        ctx.obj["missionos_chat_robot_profile"] = "nova_carter"
        if not initial_raw:
            initial_raw = DEFAULT_NOVA_CARTER_CHAT_INSTRUCTION
        if session_id == DEFAULT_SESSION_ID:
            session_id = "missionos-cli-nova-carter"
        _floor_turtlebot3_chat_timeout(ctx)
    elif robot_profile in {"turtlebot3", "turtlebot4"}:
        ctx.obj["missionos_chat_robot_profile"] = robot_profile
        _floor_turtlebot3_chat_timeout(ctx)
        if robot_profile == "turtlebot4" and turtlebot3_smoke:
            raise click.UsageError("--turtlebot3-smoke can only be used with --robot turtlebot3.")
        if robot_profile == "turtlebot3" and turtlebot3_smoke:
            raise SystemExit(
                _run_turtlebot3_chat_smoke(
                    instruction=initial_raw or DEFAULT_TURTLEBOT3_CHAT_INSTRUCTION,
                    build_image=turtlebot3_build_image,
                    mid_recovery=turtlebot3_mid_recovery,
                    dry_run=turtlebot3_dry_run,
                )
            )
        if not initial_raw:
            initial_raw = (
                DEFAULT_TURTLEBOT4_CHAT_INSTRUCTION
                if robot_profile == "turtlebot4"
                else DEFAULT_TURTLEBOT3_CHAT_INSTRUCTION
            )
        if session_id == DEFAULT_SESSION_ID:
            session_id = f"missionos-cli-{robot_profile}"
        if robot_profile == "turtlebot4" and turtlebot3_dry_run:
            raise click.UsageError("--turtlebot3-dry-run can only be used with --robot turtlebot3.")
        if robot_profile == "turtlebot3" and turtlebot3_dry_run:
            _maybe_retarget_turtlebot3_gateway_url(ctx)
            _start_turtlebot3_gateway_container(
                gateway_url=ctx.obj["missionos_gateway_url"],
                instruction=initial_raw,
                build_image=turtlebot3_build_image,
                dry_run=True,
            )
            return
        if robot_profile == "turtlebot3":
            _maybe_retarget_turtlebot3_gateway_url(ctx)
    else:
        ctx.obj["missionos_chat_robot_profile"] = ""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    ctx.obj["missionos_chat_session_id"] = session_id
    ctx.obj["missionos_chat_companion_terminals_enabled"] = (
        companion_terminals and sys.stdin.isatty()
    )
    turtlebot3_container_started = False
    if robot_profile == "turtlebot3" and not _gateway_reachable(client):
        turtlebot3_gateway_api_key = client.api_key or secrets.token_urlsafe(32)
        client.api_key = turtlebot3_gateway_api_key
        turtlebot3_container_started = _start_turtlebot3_gateway_container(
            gateway_url=ctx.obj["missionos_gateway_url"],
            instruction=initial_raw,
            build_image=turtlebot3_build_image,
            dry_run=turtlebot3_dry_run,
            gateway_api_key=turtlebot3_gateway_api_key,
        )
        if turtlebot3_dry_run:
            return
    gateway_proc = _ensure_gateway(
        client,
        ctx.obj["missionos_gateway_url"],
        autostart=autostart and robot_profile != "turtlebot3",
        enable_live_sitl=enable_live_sitl,
    )
    console.print(_chat_help_panel())
    session = _build_chat_session(history_path)
    try:
        if initial_raw:
            console.print(f"[bold green]MissionOS>[/bold green] {initial_raw}")
            if not _handle_chat_input(ctx, client, initial_raw, session_id=session_id):
                return
            if not sys.stdin.isatty():
                return
        while True:
            try:
                with patch_stdout(raw=True):
                    raw = session.prompt(_chat_prompt_fragment(ctx))
            except KeyboardInterrupt:
                console.print("[yellow](Ctrl+C — type /quit or Ctrl+D to exit)[/yellow]")
                continue
            except EOFError:
                break
            if not _handle_chat_input(ctx, client, raw, session_id=session_id):
                break
    finally:
        _stop_turtlebot3_chat_task_status_monitor(ctx)
        _stop_chat_companion_terminals(ctx)
        if turtlebot3_container_started:
            console.print("[blue]Stopping the TurtleBot3 Gateway container...[/blue]")
            _stop_turtlebot3_gateway_container()
        if gateway_proc is not None:
            console.print("[blue]Stopping the autostarted Gateway...[/blue]")
            _terminate_gateway(gateway_proc)


# ---------------------------------------------------------------------------
# missionos play — AI mission-control lab (deterministic what-if)
# ---------------------------------------------------------------------------


@missionos.command("play")
@click.argument("destination", nargs=-1, required=False)
@click.option(
    "--scenario",
    "scenario_key",
    default=None,
    help="Bundled scenario key (default: the flagship Fuji mountain-hut delivery).",
)
@click.option(
    "--real-weather/--bundled-weather",
    default=False,
    show_default=True,
    help="Fetch real local weather (Open-Meteo hourly) and drive wind from it.",
)
@click.option(
    "--forecast-hours",
    default=12,
    show_default=True,
    type=click.IntRange(1, 48),
    help="How many forecast hours to pull when --real-weather is on.",
)
@click.option(
    "--flight-duration",
    default=20.0,
    show_default=True,
    type=click.FloatRange(4.0, 300.0),
    help="Seconds to run the live SITL wind-disturbance segment after takeoff.",
)
@click.option(
    "--wind-step",
    default=2.0,
    show_default=True,
    type=click.FloatRange(0.5, 30.0),
    help="Seconds between live wind-force updates during `fly`.",
)
@click.option(
    "--battery-coupling/--no-battery-coupling",
    default=False,
    show_default=True,
    help="Enable the rotor-load-coupled battery in the live `fly` flight.",
)
@click.option(
    "--gps-denied/--gps-available",
    default=False,
    show_default=True,
    help="Disable EKF GPS fusion in the live `fly` flight (GPS-denied scenario).",
)
@click.option(
    "--history-path",
    default=DEFAULT_HISTORY_PATH + "_play",
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
)
def play_command(
    destination: tuple[str, ...],
    scenario_key: str | None,
    real_weather: bool,
    forecast_hours: int,
    flight_duration: float,
    wind_step: float,
    battery_coupling: bool,
    gps_denied: bool,
    history_path: Path,
) -> None:
    """Experimental deterministic lab; not the main LLM chat path.

    Deterministic what-if lab. With --bundled-weather (default) it needs no
    network; --real-weather pulls live Open-Meteo conditions for the scenario
    and drives the wind from the real, altitude-adjusted forecast.
    """
    run_play_command(
        destination=destination,
        scenario_key=scenario_key,
        real_weather=real_weather,
        forecast_hours=forecast_hours,
        flight_duration=flight_duration,
        wind_step=wind_step,
        battery_coupling=battery_coupling,
        gps_denied=gps_denied,
        history_path=history_path,
    )
