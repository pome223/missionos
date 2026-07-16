"""Read-only job, timeline, recovery, and altitude status projections."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import math


def _status_text(value: Any, default: str = "-") -> str:
    if value is None or value == "":
        return default
    return str(value)


def _task_artifacts(task_payload: dict[str, Any]) -> dict[str, Any]:
    artifacts = task_payload.get("artifacts")
    if isinstance(artifacts, dict):
        return _artifacts_with_latest_runtime_snapshot(artifacts)
    task = task_payload.get("task")
    if isinstance(task, dict) and isinstance(task.get("artifacts"), dict):
        return _artifacts_with_latest_runtime_snapshot(task["artifacts"])
    return {}


def _artifacts_with_latest_runtime_snapshot(
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    snapshot = artifacts.get("missionos_auto_mission_runtime_snapshot")
    if not isinstance(snapshot, dict):
        return artifacts
    latest = _runtime_snapshot_with_latest_file(snapshot)
    if latest is snapshot:
        return artifacts
    updated = dict(artifacts)
    updated["missionos_auto_mission_runtime_snapshot"] = latest
    return updated


def _runtime_snapshot_with_latest_file(snapshot: dict[str, Any]) -> dict[str, Any]:
    snapshot_path = snapshot.get("running_snapshot_path")
    if not isinstance(snapshot_path, str) or not snapshot_path:
        return snapshot
    path = Path(snapshot_path)
    if path.name != "running_snapshot.json" or not path.exists():
        return snapshot
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return snapshot
    if not isinstance(payload, dict):
        return snapshot
    current_index = _as_float(snapshot.get("sample_index"))
    latest_index = _as_float(payload.get("sample_index"))
    if latest_index is None:
        return snapshot
    if current_index is not None and latest_index < current_index:
        return snapshot
    latest = {**snapshot, **payload}
    latest.setdefault("schema_version", snapshot.get("schema_version"))
    latest["running_snapshot_path"] = snapshot_path
    return latest


def _task_record(task_payload: dict[str, Any]) -> dict[str, Any]:
    task = task_payload.get("task")
    if isinstance(task, dict):
        return task
    return task_payload


def _task_status(task_payload: dict[str, Any]) -> str:
    task = _task_record(task_payload)
    return str(task.get("status") or task.get("task_status") or "")


def _as_float(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n"}:
            return False
    return None


def _format_duration(seconds: Any) -> str:
    value = _as_float(seconds)
    if value is None:
        return "-"
    total = max(0, int(round(value)))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _format_distance(meters: Any) -> str:
    value = _as_float(meters)
    if value is None:
        return "-"
    if abs(value) >= 1000:
        return f"{value / 1000:.2f} km"
    return f"{value:.0f} m"


def _format_percent(value: Any) -> str:
    number = _as_float(value)
    if number is None:
        return "-"
    return f"{number:.1f}%"


def _first_numeric(*values: Any) -> float | None:
    for value in values:
        number = _as_float(value)
        if number is not None:
            return number
    return None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _format_flag(value: Any, *, default: str = "-") -> str:
    if value is None or value == "":
        return default
    return str(value)


def _job_route_distance_m(artifacts: dict[str, Any]) -> float | None:
    route = artifacts.get("mission_designer_coordinate_pair_route")
    route = route if isinstance(route, dict) else {}
    route_plan = artifacts.get("digital_twin_route_plan")
    route_plan = route_plan if isinstance(route_plan, dict) else {}
    compilation = artifacts.get("missionos_auto_mission_compilation")
    compilation = compilation if isinstance(compilation, dict) else {}
    return _first_numeric(
        route.get("derived_route_distance_m"),
        route_plan.get("planned_route_distance_m"),
        route_plan.get("requested_distance_m"),
        compilation.get("planned_route_m"),
    )


def _format_mps(value: Any) -> str:
    number = _as_float(value)
    if number is None:
        return "-"
    return f"{number:.1f}m/s"


def _format_degrees(value: Any) -> str:
    number = _as_float(value)
    if number is None:
        return "-"
    return f"{number:.0f}deg"


def _format_temperature_c(value: Any) -> str:
    number = _as_float(value)
    if number is None:
        return "-"
    return f"{number:.1f}C"


def _format_hpa(value: Any) -> str:
    number = _as_float(value)
    if number is None:
        return "-"
    return f"{number:.0f}hPa"


def _format_mm_per_hour(value: Any) -> str:
    number = _as_float(value)
    if number is None:
        return "-"
    return f"{number:.1f}mm/h"


def _job_weather_condition_text(artifacts: dict[str, Any]) -> str | None:
    route = artifacts.get("mission_designer_coordinate_pair_route")
    route = route if isinstance(route, dict) else {}
    keys = (
        "wind_speed_mps",
        "wind_direction_deg",
        "wind_gust_mps",
        "wind_variance",
        "temperature_c",
        "pressure_hpa",
        "precipitation_mm_per_hour",
    )
    if not any(route.get(key) not in (None, "") for key in keys):
        return None
    return (
        "Weather: "
        f"wind={_format_mps(route.get('wind_speed_mps'))}; "
        f"dir={_format_degrees(route.get('wind_direction_deg'))}; "
        f"gust={_format_mps(route.get('wind_gust_mps'))}; "
        f"variance={_status_text(route.get('wind_variance'))}; "
        f"temp={_format_temperature_c(route.get('temperature_c'))}; "
        f"pressure={_format_hpa(route.get('pressure_hpa'))}; "
        f"rain={_format_mm_per_hour(route.get('precipitation_mm_per_hour'))}"
    )


def _job_weather_compact_text(artifacts: dict[str, Any]) -> str | None:
    route = artifacts.get("mission_designer_coordinate_pair_route")
    route = route if isinstance(route, dict) else {}
    if not any(
        route.get(key) not in (None, "")
        for key in (
            "wind_speed_mps",
            "wind_gust_mps",
            "temperature_c",
            "precipitation_mm_per_hour",
        )
    ):
        return None
    return (
        f"weather wind={_format_mps(route.get('wind_speed_mps'))} "
        f"gust={_format_mps(route.get('wind_gust_mps'))} "
        f"temp={_format_temperature_c(route.get('temperature_c'))} "
        f"rain={_format_mm_per_hour(route.get('precipitation_mm_per_hour'))}"
    )


def _job_realism_condition_text(artifacts: dict[str, Any]) -> str | None:
    route = artifacts.get("mission_designer_coordinate_pair_route")
    route = route if isinstance(route, dict) else {}
    thermal_app = artifacts.get("missionos_auto_thermal_weather_simulator_condition_application")
    if not isinstance(thermal_app, dict):
        thermal_app = artifacts.get("thermal_weather_simulator_condition_application")
    thermal_app = thermal_app if isinstance(thermal_app, dict) else {}
    thermal_evidence = artifacts.get("missionos_auto_observed_thermal_weather_evidence")
    if not isinstance(thermal_evidence, dict):
        thermal_evidence = artifacts.get("observed_thermal_weather_evidence")
    thermal_evidence = thermal_evidence if isinstance(thermal_evidence, dict) else {}
    rain_app = artifacts.get("missionos_auto_rain_weather_simulator_condition_application")
    if not isinstance(rain_app, dict):
        rain_app = artifacts.get("rain_weather_simulator_condition_application")
    rain_app = rain_app if isinstance(rain_app, dict) else {}
    rain_evidence = artifacts.get("missionos_auto_observed_rain_weather_evidence")
    if not isinstance(rain_evidence, dict):
        rain_evidence = artifacts.get("observed_rain_weather_evidence")
    rain_evidence = rain_evidence if isinstance(rain_evidence, dict) else {}
    wind_app = artifacts.get("missionos_auto_simulator_condition_application")
    if not isinstance(wind_app, dict):
        wind_app = artifacts.get("simulator_condition_application")
    wind_app = wind_app if isinstance(wind_app, dict) else {}
    wind_evidence = artifacts.get("missionos_auto_observed_environment_evidence")
    if not isinstance(wind_evidence, dict):
        wind_evidence = artifacts.get("observed_environment_evidence")
    wind_evidence = wind_evidence if isinstance(wind_evidence, dict) else {}
    runtime_snapshot = artifacts.get("missionos_auto_mission_runtime_snapshot")
    runtime_snapshot = runtime_snapshot if isinstance(runtime_snapshot, dict) else {}

    thermal_requested = any(
        route.get(key) not in (None, "")
        for key in (
            "temperature_c",
            "thermal_battery_drain_factor",
            "thermal_motor_derate_factor",
        )
    )
    wind_requested = any(
        route.get(key) not in (None, "")
        for key in ("wind_speed_mps", "wind_gust_mps", "wind_variance")
    )
    gust_requested = route.get("wind_gust_mps") not in (None, "")
    rain_requested = any(
        route.get(key) not in (None, "")
        for key in (
            "precipitation_mm_per_hour",
            "rain_visual_mode",
            "rain_battery_drain_factor",
            "rain_sensor_degradation_factor",
            "rain_landing_risk_factor",
        )
    )
    auto_dispatch = any(
        key in artifacts
        for key in (
            "missionos_auto_mission_gui_dispatch_running_receipt",
            "missionos_auto_mission_gui_dispatch_receipt",
            "missionos_auto_mission_runtime_snapshot",
        )
    )
    if not thermal_requested and not wind_requested and not rain_requested:
        return None

    app_status = _status_text(
        thermal_app.get("application_status"),
        default="pending" if thermal_requested else "not_requested",
    )
    observation_status = _status_text(
        thermal_evidence.get("observation_status"),
        default="pending" if thermal_requested else "not_requested",
    )
    applied = thermal_app.get("applied")
    applied = applied if isinstance(applied, dict) else {}
    parts = [
        f"thermal={app_status}",
        f"thermal_observed={observation_status}",
    ]
    if applied:
        parts.extend(
            [
                f"battery_factor={_status_text(applied.get('thermal_battery_drain_factor'))}",
                f"motor_derate={_status_text(applied.get('thermal_motor_derate_factor'))}",
                f"sim_bat_drain={_status_text(applied.get('effective_sim_bat_drain_seconds'))}s",
            ]
        )
    if rain_requested:
        rain_status = _status_text(
            rain_app.get("application_status"),
            default="pending",
        )
        rain_observation_status = _status_text(
            rain_evidence.get("observation_status"),
            default="pending",
        )
        rain_applied = rain_app.get("applied")
        rain_applied = rain_applied if isinstance(rain_applied, dict) else {}
        parts.extend(
            [
                f"rain={rain_status}",
                f"rain_observed={rain_observation_status}",
            ]
        )
        if rain_applied:
            parts.extend(
                [
                    f"rain_battery_factor={_status_text(rain_applied.get('rain_battery_drain_factor'))}",
                    f"rain_sensor_factor={_status_text(rain_applied.get('rain_sensor_degradation_factor'))}",
                    f"rain_landing_factor={_status_text(rain_applied.get('rain_landing_risk_factor'))}",
                ]
            )
    if wind_requested:
        wind_snapshot_default = "pending"
        if runtime_snapshot.get("wind_mean_pending_reason"):
            wind_snapshot_default = str(runtime_snapshot.get("wind_mean_pending_reason"))
        elif runtime_snapshot.get("wind_mean_started"):
            wind_snapshot_default = "wind_topic_publish_observed"
        elif runtime_snapshot.get("wind_gust_window_start_seconds") is not None:
            wind_snapshot_default = "materialized_gz_wind_window"
        wind_status = _status_text(
            wind_app.get("application_status"),
            default=wind_snapshot_default if auto_dispatch else "pending",
        )
        wind_observation = _status_text(
            wind_evidence.get("observation_status"),
            default=(
                str(runtime_snapshot.get("wind_mean_pending_reason"))
                if runtime_snapshot.get("wind_mean_pending_reason")
                else (
                    "wind_gust_window_running"
                    if runtime_snapshot.get("wind_gust_started")
                    else (
                        "wind_topic_publish_observed"
                        if runtime_snapshot.get("wind_mean_started")
                        else ("pending" if auto_dispatch else "pending")
                    )
                )
            ),
        )
        wind_physics = (
            "materialized_gz_wind" if wind_status == "applied_with_approximations" else wind_status
        )
        parts.append(f"wind_physics={wind_physics}")
        parts.append(f"wind_observed={wind_observation}")
        if runtime_snapshot.get("wind_mean_pending_reason"):
            parts.append(
                f"wind_pending={_status_text(runtime_snapshot.get('wind_mean_pending_reason'))}"
            )
    if gust_requested:
        gust_physics = (
            "materialized_gz_wind_window"
            if wind_status == "applied_with_approximations"
            else wind_status
        )
        parts.append("gust_physics=" + gust_physics)
        parts.append(f"gust_observed={wind_observation}")
    return "Realism: " + "; ".join(parts)


def _auto_process_status_text(
    *,
    artifacts: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    snapshot: dict[str, Any] | None = None,
) -> str | None:
    metadata = metadata if isinstance(metadata, dict) else {}
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    receipt = artifacts.get("missionos_auto_mission_gui_dispatch_receipt")
    receipt = receipt if isinstance(receipt, dict) else {}
    running_receipt = artifacts.get("missionos_auto_mission_gui_dispatch_running_receipt")
    running_receipt = running_receipt if isinstance(running_receipt, dict) else {}
    failed_receipt = artifacts.get("missionos_auto_mission_gui_dispatch_failed_receipt")
    failed_receipt = failed_receipt if isinstance(failed_receipt, dict) else {}
    process_status = _first_present(
        receipt.get("auto_mission_process_status"),
        metadata.get("missionos_auto_mission_process_status"),
        failed_receipt.get("auto_mission_process_status"),
    )
    terminal_gates = _first_present(
        receipt.get("auto_mission_terminal_gates_passed"),
        metadata.get("missionos_auto_mission_terminal_gates_passed"),
    )
    if process_status is None and terminal_gates is None:
        return None
    parts = [f"auto_mission={_status_text(process_status)}"]
    if terminal_gates is not None:
        parts.append(f"terminal_gates={_format_flag(terminal_gates, default='pending')}")
    dispatch_status = _first_present(
        receipt.get("dispatch_status"),
        metadata.get("missionos_auto_mission_gui_dispatch_status"),
        running_receipt.get("dispatch_status"),
    )
    if dispatch_status is not None:
        parts.append(f"dispatch={_status_text(dispatch_status)}")
    monitor_stop = _status_text(snapshot.get("monitor_stop_reason"))
    if monitor_stop != "-":
        parts.append(f"stop={monitor_stop}")
    return "Process: " + "; ".join(parts)


def _progress_bar(percent: float | None, *, width: int = 28) -> str:
    if percent is None:
        return "[" + "-" * width + "]"
    clamped = min(100.0, max(0.0, percent))
    filled = int(round(width * clamped / 100.0))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _job_progress_percent(
    *,
    progress_m: float | None,
    route_distance_m: float | None,
    reached_seq: int | None,
    waypoint_total: int | None,
) -> float | None:
    if progress_m is not None and route_distance_m and route_distance_m > 0:
        return min(100.0, max(0.0, progress_m / route_distance_m * 100.0))
    if reached_seq is not None and waypoint_total and waypoint_total > 0:
        return min(100.0, max(0.0, reached_seq / waypoint_total * 100.0))
    return None


def _job_eta_seconds(
    *,
    elapsed_seconds: float | None,
    progress_m: float | None,
    route_distance_m: float | None,
    monitor_seconds: float | None,
) -> float | None:
    if (
        elapsed_seconds is not None
        and progress_m is not None
        and progress_m > 0
        and route_distance_m is not None
        and route_distance_m > progress_m
    ):
        return elapsed_seconds / progress_m * (route_distance_m - progress_m)
    if monitor_seconds is not None and elapsed_seconds is not None:
        return max(0.0, monitor_seconds - elapsed_seconds)
    return None


def _runtime_recovery_agent_action(artifacts: dict[str, Any]) -> Any:
    agent_bridge = artifacts.get("missionos_runtime_recovery_agent_live_bridge")
    agent_bridge = agent_bridge if isinstance(agent_bridge, dict) else {}
    agent_result = agent_bridge.get("runtime_recovery_agent_result")
    agent_result = agent_result if isinstance(agent_result, dict) else {}
    agent_assessment = agent_result.get("assessment")
    agent_assessment = agent_assessment if isinstance(agent_assessment, dict) else {}
    return _first_present(
        agent_assessment.get("selected_bounded_action"),
        agent_assessment.get("recommended_action"),
        agent_assessment.get("recovery_action"),
    )


def _runtime_recovery_agent_parameters(artifacts: dict[str, Any]) -> dict[str, Any]:
    agent_bridge = artifacts.get("missionos_runtime_recovery_agent_live_bridge")
    agent_bridge = agent_bridge if isinstance(agent_bridge, dict) else {}
    agent_result = agent_bridge.get("runtime_recovery_agent_result")
    agent_result = agent_result if isinstance(agent_result, dict) else {}
    agent_assessment = agent_result.get("assessment")
    agent_assessment = agent_assessment if isinstance(agent_assessment, dict) else {}
    parameters = agent_assessment.get("proposed_parameters")
    return dict(parameters) if isinstance(parameters, dict) else {}


def _runtime_recovery_effective_status(
    agent_result: dict[str, Any],
    agent_bridge: dict[str, Any],
    agent_assessment: dict[str, Any],
) -> str:
    status = _status_text(agent_result.get("runtime_status") or agent_bridge.get("bridge_status"))
    assessment_status = _status_text(agent_assessment.get("assessment_status"), "")
    action = _first_present(
        agent_assessment.get("selected_bounded_action"),
        agent_assessment.get("recommended_action"),
        agent_assessment.get("recovery_action"),
    )
    if assessment_status == "proposal_guardrail_passed" and action:
        return assessment_status
    return status


def _operator_recovery_dispatch_command(action: Any) -> tuple[str, str, str] | None:
    normalized = str(action or "").strip().lower().replace("-", "_")
    if normalized in {"return_to_launch", "return_to_home", "return_home", "rtl"}:
        return ("RTL", "/rtl", "return_to_launch")
    if normalized == "land":
        return ("LAND", "/land", "land")
    return None


def _recovery_parameter_text(value: Any) -> str:
    number = _as_float(value)
    if number is None:
        return str(value)
    if math.isfinite(number) and abs(number - round(number)) < 1e-6:
        return str(int(round(number)))
    return f"{number:.3f}".rstrip("0").rstrip(".")


def _operator_recovery_cli_command(
    *,
    task_id: str,
    action: Any,
    parameters: dict[str, Any] | None = None,
) -> str | None:
    normalized = str(action or "").strip().lower().replace("-", "_")
    params = parameters if isinstance(parameters, dict) else {}
    if normalized == "adjust_altitude":
        altitude = params.get("target_altitude_m")
        value = _recovery_parameter_text(altitude) if altitude is not None else "<m>"
        return f"missionos climb --task-id {task_id} --altitude-m {value}"
    if normalized == "adjust_speed":
        speed = params.get("target_speed_mps")
        value = _recovery_parameter_text(speed) if speed is not None else "<m/s>"
        return f"missionos speed --task-id {task_id} --speed-mps {value}"
    if normalized in {"reroute", "avoid_obstacle"}:
        x_value = params.get("target_x_m")
        y_value = params.get("target_y_m")
        x_text = _recovery_parameter_text(x_value) if x_value is not None else "<north_m>"
        y_text = _recovery_parameter_text(y_value) if y_value is not None else "<east_m>"
        command = "avoid-obstacle" if normalized == "avoid_obstacle" else "reroute"
        parts = [
            "missionos",
            command,
            "--task-id",
            task_id,
            "--target-x-m",
            x_text,
            "--target-y-m",
            y_text,
        ]
        altitude = params.get("target_altitude_m")
        if altitude is not None:
            parts.extend(["--altitude-m", _recovery_parameter_text(altitude)])
        return " ".join(parts)
    return None


def _operator_recovery_console_command(
    action: Any,
    parameters: dict[str, Any] | None = None,
) -> str | None:
    normalized = str(action or "").strip().lower().replace("-", "_")
    params = parameters if isinstance(parameters, dict) else {}
    if normalized == "adjust_altitude" and params.get("target_altitude_m") is not None:
        return f"climb {_recovery_parameter_text(params['target_altitude_m'])}"
    if normalized == "adjust_speed" and params.get("target_speed_mps") is not None:
        return f"speed {_recovery_parameter_text(params['target_speed_mps'])}"
    if normalized in {"reroute", "avoid_obstacle"}:
        x_value = params.get("target_x_m")
        y_value = params.get("target_y_m")
        if x_value is None or y_value is None:
            return None
        command = "avoid" if normalized == "avoid_obstacle" else "reroute"
        parts = [
            command,
            _recovery_parameter_text(x_value),
            _recovery_parameter_text(y_value),
        ]
        if params.get("target_altitude_m") is not None:
            parts.append(_recovery_parameter_text(params["target_altitude_m"]))
        return " ".join(parts)
    return None


def _operator_recovery_dispatch_hint(
    *,
    task_id: Any,
    action: Any,
    parameters: dict[str, Any] | None = None,
    compact: bool = False,
) -> str | None:
    task_text = _status_text(task_id)
    if task_text == "-":
        return None
    normalized = str(action or "").strip().lower().replace("-", "_")
    parameterized_command = _operator_recovery_cli_command(
        task_id=task_text,
        action=normalized,
        parameters=parameters,
    )
    if parameterized_command:
        if compact:
            return f"operator_action={parameterized_command}"
        return (
            "Operator action available: "
            f"[bold]{parameterized_command}[/bold]; "
            "Gateway validates approval, parameters, and active-runner support."
        )
    command = _operator_recovery_dispatch_command(action)
    if command is None:
        return None
    label, chat_command, recovery_action = command
    chat_text = f"{chat_command} {task_text}"
    if compact:
        return f"operator_action={chat_text}"
    return (
        f"Operator Recovery: {label} can be operator-approved via {chat_text} "
        f"(chat) or missionos recover --task-id {task_text} --action "
        f"{recovery_action}; Gateway validates the live allowlist."
    )


def _operator_recovery_ack_text(*, observed: Any, result: Any) -> str:
    if observed is True:
        if str(result) in {"0", "ACCEPTED", "MAV_RESULT_ACCEPTED"}:
            return "accepted"
        return f"result={_status_text(result)}"
    if observed is False:
        return "not_observed"
    return "pending"


def _operator_recovery_maneuver_evidence_snapshot(
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    probe = artifacts.get("missionos_auto_mission_probe_observed")
    probe = probe if isinstance(probe, dict) else {}
    monitor = probe.get("monitor")
    monitor = monitor if isinstance(monitor, dict) else {}
    terminal = monitor.get("terminal_snapshot")
    terminal = terminal if isinstance(terminal, dict) else {}
    action = str(terminal.get("operator_recovery_action") or "").strip().lower()
    if action not in {"adjust_altitude", "adjust_speed", "reroute", "avoid_obstacle"}:
        return {}
    if not any(
        terminal.get(key) is not None
        for key in (
            "operator_recovery_assist_status",
            "operator_recovery_target_reached",
            "operator_recovery_resume_auto_status",
        )
    ):
        return {}
    return terminal


def _operator_recovery_assist_status_text(snapshot: dict[str, Any]) -> str:
    if not any(
        snapshot.get(key) is not None
        for key in (
            "operator_recovery_assist_status",
            "operator_recovery_target_reached",
            "operator_recovery_resume_auto_status",
        )
    ):
        return ""
    return (
        f"assist={_status_text(snapshot.get('operator_recovery_assist_status'))}; "
        f"target={_status_text(snapshot.get('operator_recovery_target_reached'))}; "
        f"resume={_status_text(snapshot.get('operator_recovery_resume_auto_status'))}"
    )


def _operator_recovery_dispatch_status_text(
    *,
    artifacts: dict[str, Any],
    snapshot: dict[str, Any],
    compact: bool = False,
) -> str | None:
    receipt = artifacts.get("missionos_runtime_recovery_dispatch_receipt")
    receipt = receipt if isinstance(receipt, dict) else {}
    if not receipt and not snapshot.get("operator_recovery_request_observed"):
        return None
    status = _status_text(receipt.get("dispatch_status"))
    action = _status_text(
        receipt.get("recovery_action") or snapshot.get("operator_recovery_action")
    )
    parameters = (
        receipt.get("recovery_parameters") or snapshot.get("operator_recovery_parameters") or {}
    )
    parameter_text = ""
    if isinstance(parameters, dict) and parameters:
        parameter_text = "; params=" + ",".join(
            f"{key}={value}" for key, value in sorted(parameters.items())
        )
    active_runner = (
        "queued"
        if receipt.get("active_runner_request_queued") is True
        else "not_queued"
        if receipt
        else "observed"
    )
    runner_observed = _format_flag(
        snapshot.get("operator_recovery_request_observed"),
        default="pending",
    )
    ack = _operator_recovery_ack_text(
        observed=snapshot.get("operator_recovery_command_ack_observed"),
        result=snapshot.get("operator_recovery_command_ack_result"),
    )
    tracking_text = ""
    if snapshot.get("post_abort_tracking") is True:
        tracking_text = (
            f"; tracking={_status_text(snapshot.get('operator_recovery_path'))}"
            f"; landed={_status_text(snapshot.get('landed'))}"
            f"; arming={_status_text(snapshot.get('arming_state'))}"
        )
        outcome = snapshot.get("post_abort_outcome_status")
        if outcome:
            tracking_text += f"; outcome={_status_text(outcome)}"
        assist_status = snapshot.get("operator_recovery_assist_status")
        if assist_status:
            tracking_text += f"; assist={_status_text(assist_status)}"
            disarm_ack = _operator_recovery_ack_text(
                observed=snapshot.get("operator_recovery_assist_low_altitude_disarm_ack_observed"),
                result=snapshot.get("operator_recovery_assist_low_altitude_disarm_ack_result"),
            )
            if disarm_ack != "-":
                tracking_text += f"; assist_disarm={disarm_ack}"
            force_disarm_ack = _operator_recovery_ack_text(
                observed=snapshot.get(
                    "operator_recovery_assist_low_altitude_force_disarm_ack_observed"
                ),
                result=snapshot.get(
                    "operator_recovery_assist_low_altitude_force_disarm_ack_result"
                ),
            )
            if force_disarm_ack != "-":
                tracking_text += f"; assist_force_disarm={force_disarm_ack}"
    maneuver_snapshot = _operator_recovery_maneuver_evidence_snapshot(artifacts)
    maneuver_text = ""
    if maneuver_snapshot and not snapshot.get("operator_recovery_assist_status"):
        maneuver_assist = _operator_recovery_assist_status_text(maneuver_snapshot)
        if maneuver_assist:
            maneuver_text = (
                f"; maneuver={_status_text(maneuver_snapshot.get('operator_recovery_action'))}; "
                f"{maneuver_assist}"
            )
    if compact:
        return (
            f"operator_dispatch={status}; action={action}; "
            f"active_runner={active_runner}; runner_observed={runner_observed}; "
            f"ack={ack}{parameter_text}{tracking_text}{maneuver_text}"
        )
    return (
        "Operator Dispatch: "
        f"status={status}; action={action}; active_runner={active_runner}; "
        f"runner_observed={runner_observed}; ack={ack}{parameter_text}{tracking_text}{maneuver_text}"
    )


def _job_operator_summary(task_payload: dict[str, Any]) -> list[str]:
    task = _task_record(task_payload)
    artifacts = _task_artifacts(task_payload)
    snapshot = artifacts.get("missionos_auto_mission_runtime_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    dispatch_receipt = artifacts.get("missionos_auto_mission_gui_dispatch_receipt")
    dispatch_receipt = dispatch_receipt if isinstance(dispatch_receipt, dict) else {}
    running_receipt = artifacts.get("missionos_auto_mission_gui_dispatch_running_receipt")
    running_receipt = running_receipt if isinstance(running_receipt, dict) else {}
    failed_receipt = artifacts.get("missionos_auto_mission_gui_dispatch_failed_receipt")
    failed_receipt = failed_receipt if isinstance(failed_receipt, dict) else {}
    replay = artifacts.get("missionos_auto_mission_runtime_replay")
    replay = replay if isinstance(replay, dict) else {}
    dropoff_gate = artifacts.get("missionos_auto_mission_dropoff_gate_summary")
    dropoff_gate = dropoff_gate if isinstance(dropoff_gate, dict) else {}
    sitl_delivery_gate = artifacts.get("missionos_auto_mission_sitl_delivery_gate_summary")
    sitl_delivery_gate = sitl_delivery_gate if isinstance(sitl_delivery_gate, dict) else {}
    runtime_summary = artifacts.get("missionos_auto_mission_runtime_monitor_summary")
    runtime_summary = runtime_summary if isinstance(runtime_summary, dict) else {}
    agent_bridge = artifacts.get("missionos_runtime_recovery_agent_live_bridge")
    agent_bridge = agent_bridge if isinstance(agent_bridge, dict) else {}
    agent_result = agent_bridge.get("runtime_recovery_agent_result")
    agent_result = agent_result if isinstance(agent_result, dict) else {}
    agent_assessment = agent_result.get("assessment")
    agent_assessment = agent_assessment if isinstance(agent_assessment, dict) else {}
    agent_telemetry = agent_bridge.get("telemetry_snapshot")
    agent_telemetry = agent_telemetry if isinstance(agent_telemetry, dict) else {}
    startup = artifacts.get("px4_gazebo_mission_designer_sitl_startup")
    startup = startup if isinstance(startup, dict) else {}
    readiness = startup.get("readiness") if isinstance(startup.get("readiness"), dict) else {}
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}

    task_status = _status_text(task.get("status") or task.get("task_status"))
    dispatch_status = (
        failed_receipt.get("dispatch_status")
        or dispatch_receipt.get("dispatch_status")
        or metadata.get("missionos_auto_mission_gui_dispatch_status")
        or running_receipt.get("dispatch_status")
        or "-"
    )
    progress_m = _as_float(snapshot.get("progress_m"))
    route_distance_m = _job_route_distance_m(artifacts)
    elapsed_seconds = _as_float(snapshot.get("elapsed_seconds"))
    monitor_seconds = _first_numeric(
        dispatch_receipt.get("monitor_seconds"),
        metadata.get("missionos_auto_mission_monitor_seconds"),
        running_receipt.get("monitor_seconds"),
    )
    reached_seq = _as_int(snapshot.get("mission_reached_seq"))
    current_seq = _as_int(snapshot.get("mission_current_seq"))
    waypoint_total = _as_int(snapshot.get("waypoint_total"))
    progress_percent = _job_progress_percent(
        progress_m=progress_m,
        route_distance_m=route_distance_m,
        reached_seq=reached_seq,
        waypoint_total=waypoint_total,
    )
    eta_seconds = _job_eta_seconds(
        elapsed_seconds=elapsed_seconds,
        progress_m=progress_m,
        route_distance_m=route_distance_m,
        monitor_seconds=monitor_seconds,
    )
    progress_text = (
        f"{_format_distance(progress_m)} / {_format_distance(route_distance_m)}"
        if route_distance_m is not None
        else _format_distance(progress_m)
    )
    waypoint_text = (
        f"{_status_text(reached_seq)}/{_status_text(waypoint_total)} reached"
        if waypoint_total is not None
        else _status_text(reached_seq)
    )
    current_text = f"current seq {_status_text(current_seq)}" if current_seq is not None else "-"
    battery_text = _format_percent(snapshot.get("battery_remaining_percent"))
    terrain_clearance_m = _as_float(snapshot.get("terrain_clearance_m"))
    terrain_clearance_target_m = _as_float(snapshot.get("terrain_clearance_target_m"))
    terrain_clearance_margin_m = _as_float(snapshot.get("terrain_clearance_margin_m"))
    terrain_clearance_status = _status_text(snapshot.get("terrain_clearance_status"))
    monitor_stop = _status_text(snapshot.get("monitor_stop_reason"))
    readiness_text = _status_text(readiness.get("readiness_status"))
    missionos_fixture = metadata.get("missionos_fixture") is True
    actual_sitl_evidence = _first_present(
        metadata.get("actual_sitl_flight_evidence_observed"),
        replay.get("actual_sitl_flight_evidence_observed"),
    )
    dropoff_verified = _first_present(
        metadata.get("dropoff_verified"),
        sitl_delivery_gate.get("dropoff_verified"),
        replay.get("dropoff_verified"),
        dropoff_gate.get("dropoff_verified"),
    )
    sitl_delivery = _first_present(
        metadata.get("sitl_delivery_claimed"),
        sitl_delivery_gate.get("sitl_delivery_claimed"),
        replay.get("sitl_delivery_claimed"),
    )
    delivery_completion = _first_present(
        snapshot.get("delivery_completion_claimed"),
        dispatch_receipt.get("delivery_completion_claimed"),
        metadata.get("delivery_completion_claimed"),
    )
    physical_execution = _first_present(
        snapshot.get("physical_execution_invoked"),
        metadata.get("physical_execution_invoked"),
    )
    recovery_snapshot = runtime_summary.get("recovery_agent_telemetry_snapshot")
    recovery_snapshot = recovery_snapshot if isinstance(recovery_snapshot, dict) else {}
    recovery_detail = recovery_snapshot.get("recovery")
    recovery_detail = recovery_detail if isinstance(recovery_detail, dict) else {}
    recovery_action = _first_present(
        recovery_detail.get("action"),
        runtime_summary.get("recovery_path_taken"),
    )
    recovery_ack = _first_present(
        recovery_detail.get("command_ack_observed"),
        runtime_summary.get("recovery_command_ack_observed"),
    )
    recovery_return_progress = _first_numeric(
        recovery_detail.get("recovery_return_progress_m"),
        runtime_summary.get("recovery_return_progress_m"),
    )
    recovery_final_landing_safe = _first_present(
        recovery_detail.get("final_landing_safe"),
        runtime_summary.get("final_landing_safe"),
    )
    recovery_observation_lost = _first_present(
        recovery_detail.get("observation_lost"),
        runtime_summary.get("recovery_observation_lost"),
    )
    recovery_disarm_observed = recovery_detail.get("recovery_disarm_observed")
    recovery_latest_ground_confirmed = recovery_detail.get("recovery_latest_ground_confirmed")
    force_disarm_no_ground_confirmation = recovery_detail.get("force_disarm_no_ground_confirmation")
    recovery_action_text = str(recovery_action or "").lower()
    snapshot_force_disarm_accepted = (
        snapshot.get("operator_recovery_assist_low_altitude_force_disarm_ack_result") == 0
    )
    snapshot_landed = snapshot.get("landed")
    snapshot_maybe_landed = snapshot.get("maybe_landed")
    snapshot_has_ground_signal = snapshot_landed is not None or snapshot_maybe_landed is not None
    snapshot_ground_confirmed = snapshot_landed is True or snapshot_maybe_landed is True
    snapshot_arming_state = _as_int(snapshot.get("arming_state"))
    snapshot_disarmed = snapshot_arming_state is not None and snapshot_arming_state != 2
    snapshot_force_without_ground = bool(
        "land" in recovery_action_text
        and snapshot_force_disarm_accepted
        and snapshot_has_ground_signal
        and not snapshot_ground_confirmed
    )
    if snapshot_force_without_ground:
        recovery_final_landing_safe = False
        force_disarm_no_ground_confirmation = _first_present(
            force_disarm_no_ground_confirmation,
            True,
        )
    if "land" in recovery_action_text and snapshot_has_ground_signal:
        recovery_latest_ground_confirmed = _first_present(
            recovery_latest_ground_confirmed,
            snapshot_ground_confirmed,
        )
    if snapshot_disarmed:
        recovery_disarm_observed = _first_present(
            recovery_disarm_observed,
            True,
        )
    recovery_evidence_path = runtime_summary.get("recovery_agent_evidence_window_path")
    guard_failure_reasons = runtime_summary.get("guard_failure_reasons")
    guard_failure_reasons = (
        guard_failure_reasons if isinstance(guard_failure_reasons, (list, tuple)) else []
    )
    recovery_was_guard_response = (
        runtime_summary.get("guard_abort_requested") is True
        or bool(guard_failure_reasons)
        or monitor_stop.startswith("auto_mission_")
    )
    recovery_label = "Guarded Recovery" if recovery_was_guard_response else "Post-run Return"
    monitor_window_ended = snapshot.get("monitor_window_ended") is True or (
        snapshot.get("snapshot_status") == "monitor_window_ended"
    )
    if (
        actual_sitl_evidence is None
        and progress_m is not None
        and progress_m > 0
        and physical_execution is not False
        and not missionos_fixture
    ):
        actual_sitl_evidence = True
    operator_recovery_hint = None

    if missionos_fixture:
        headline = "Fixture Only: no dispatch or live SITL flight was invoked"
    elif task_status == "running" and monitor_window_ended:
        headline = "Finalizing: AUTO monitor ended; waiting for terminal receipt"
    elif task_status == "running":
        headline = "In Flight: AUTO mission telemetry is still updating"
    elif task_status == "completed" and recovery_was_guard_response:
        headline = "Guarded Recovery Complete: Gateway recorded a terminal result"
    elif task_status == "completed":
        headline = "Complete: Gateway recorded a terminal live SITL result"
    elif task_status == "blocked":
        headline = "Blocked: Gateway stopped the task before completion"
    else:
        headline = f"Status: {task_status}"

    evidence_line = (
        "Evidence: "
        f"actual_sitl_flight={_format_flag(actual_sitl_evidence, default='pending')}; "
        f"dropoff_verified={_format_flag(dropoff_verified, default='pending')}; "
        f"sitl_delivery={_format_flag(sitl_delivery, default='pending')}"
    )
    lines = [
        headline,
        f"Task: {task.get('task_id')}  ({task_status}; dispatch={dispatch_status})",
        "",
        f"Route: {_progress_bar(progress_percent)} {_format_percent(progress_percent)}",
        f"Distance: {progress_text}",
        f"Waypoint: {waypoint_text}  ({current_text})",
        f"Elapsed: {_format_duration(elapsed_seconds)}"
        + (f"  ETA: ~{_format_duration(eta_seconds)}" if eta_seconds is not None else ""),
        f"Battery: {battery_text}",
        f"Altitude: {_operate_altitude_text(snapshot, artifacts)}",
        (
            "Terrain: "
            f"AGL={_format_distance(terrain_clearance_m)}; "
            f"target={_format_distance(terrain_clearance_target_m)}; "
            f"margin={_format_distance(terrain_clearance_margin_m)}; "
            f"status={terrain_clearance_status}"
        )
        if terrain_clearance_m is not None or terrain_clearance_target_m is not None
        else "Terrain: clearance=not_configured",
        f"SITL: startup={_status_text(startup.get('startup_status'))}; readiness={readiness_text}; mavlink={readiness.get('mavlink_endpoint_observed')}",
        "",
        evidence_line,
    ]
    process_status_text = _auto_process_status_text(
        artifacts=artifacts,
        metadata=metadata,
        snapshot=snapshot,
    )
    if process_status_text:
        lines.insert(2, process_status_text)
    operator_dispatch_text = _operator_recovery_dispatch_status_text(
        artifacts=artifacts,
        snapshot=snapshot,
    )
    if operator_dispatch_text:
        lines.insert(3 if process_status_text else 2, operator_dispatch_text)
    weather_condition = _job_weather_condition_text(artifacts)
    if weather_condition:
        sitl_index = next(
            (index for index, line in enumerate(lines) if line.startswith("SITL:")),
            len(lines),
        )
        lines.insert(sitl_index, weather_condition)
    realism_condition = _job_realism_condition_text(artifacts)
    if realism_condition:
        evidence_index = lines.index(evidence_line)
        lines.insert(evidence_index, realism_condition)
    if agent_bridge:
        agent_battery = agent_telemetry.get("battery")
        agent_battery = agent_battery if isinstance(agent_battery, dict) else {}
        endurance = agent_battery.get("endurance_projection")
        endurance = endurance if isinstance(endurance, dict) else {}
        return_home = agent_battery.get("return_home_projection")
        return_home = return_home if isinstance(return_home, dict) else {}
        agent_action = _first_present(
            agent_assessment.get("selected_bounded_action"),
            agent_assessment.get("recommended_action"),
            agent_assessment.get("recovery_action"),
        )
        agent_risk = agent_assessment.get("observed_risk_reasons")
        if isinstance(agent_risk, (list, tuple)):
            agent_risk_text = ",".join(str(item) for item in agent_risk) or "-"
        else:
            agent_risk_text = _status_text(
                agent_risk
                or agent_assessment.get("trigger_reasons")
                or agent_assessment.get("risk_level")
            )
        blocking_reasons = agent_result.get("blocking_reasons")
        if not isinstance(blocking_reasons, (list, tuple)):
            blocking_reasons = agent_assessment.get("blocking_reasons")
        blocking_text = (
            ",".join(str(item) for item in blocking_reasons)
            if isinstance(blocking_reasons, (list, tuple)) and blocking_reasons
            else "-"
        )
        lines.append(
            "Agent Proposal: "
            f"status={_runtime_recovery_effective_status(agent_result, agent_bridge, agent_assessment)}; "
            f"action={_status_text(agent_action)}; "
            f"risk_observed={agent_risk_text}; "
            f"blocked={blocking_text}; "
            "dispatch_authority=False"
        )
        if task_status == "running" and not monitor_window_ended:
            operator_recovery_hint = _operator_recovery_dispatch_hint(
                task_id=task.get("task_id"),
                action=agent_action,
                parameters=agent_assessment.get("proposed_parameters")
                if isinstance(agent_assessment.get("proposed_parameters"), dict)
                else None,
            )
            if operator_recovery_hint:
                lines.append(operator_recovery_hint)
        if endurance and endurance.get("projection_status") == "computed":
            lines.append(
                "Agent Basis: "
                f"burn={_format_percent(endurance.get('battery_burn_percent_per_km'))}/km; "
                f"remaining={_format_distance(endurance.get('remaining_route_m'))}; "
                f"needs={_format_percent(endurance.get('projected_battery_required_percent'))}; "
                f"arrival={_format_percent(endurance.get('projected_arrival_battery_percent'))}; "
                f"reserve_margin={_format_percent(endurance.get('projected_reserve_margin_percent'))}"
            )
        elif endurance:
            # Don't present a route-battery feasibility number we can't trust
            # (e.g. an arrival % higher than the current charge). The RTL basis
            # below is shown separately when it is computable.
            lines.append(
                "Agent Basis: route battery projection unavailable "
                f"({_status_text(endurance.get('projection_status')) or 'insufficient_observation'})"
            )
        if return_home:
            lines.append(
                "Agent RTL Basis: "
                f"home={_format_distance(return_home.get('distance_to_home_m'))}; "
                f"needs={_format_percent(return_home.get('projected_return_battery_required_percent'))}; "
                f"arrival={_format_percent(return_home.get('projected_return_arrival_battery_percent'))}; "
                f"reserve_margin={_format_percent(return_home.get('projected_return_reserve_margin_percent'))}; "
                f"insufficient={_format_flag(return_home.get('projected_insufficient_for_return_home'), default='pending')}"
            )
        agent_route = agent_telemetry.get("route")
        agent_route = agent_route if isinstance(agent_route, dict) else {}
        drift = agent_route.get("drift_projection")
        drift = drift if isinstance(drift, dict) else {}
        if drift:
            lines.append(
                "Agent Drift: "
                f"cross_track={_format_distance(drift.get('deviation_xy_m'))}; "
                f"along_track={_format_distance(drift.get('along_track_m'))}; "
                f"planned={_format_distance(drift.get('planned_route_m'))}"
            )
        terrain = agent_telemetry.get("terrain")
        terrain = terrain if isinstance(terrain, dict) else {}
        if terrain and terrain.get("projection_status") == "computed":
            lines.append(
                "Agent Terrain: "
                f"current_clearance={_format_distance(terrain.get('terrain_clearance_m'))}; "
                f"target={_format_distance(terrain.get('terrain_clearance_target_m'))}; "
                f"current_margin={_format_distance(terrain.get('terrain_clearance_margin_m'))}; "
                f"current_below_min={_format_flag(terrain.get('terrain_clearance_below_minimum'), default='pending')}"
            )
        obstacle = agent_telemetry.get("obstacle")
        obstacle = obstacle if isinstance(obstacle, dict) else {}
        if obstacle:
            lines.append(
                "Agent Obstacle: "
                f"status={_status_text(obstacle.get('projection_status'))}; "
                f"detected={_format_flag(obstacle.get('obstacle_detected'), default='pending')}; "
                f"building_risk={_format_flag(obstacle.get('building_risk_detected'), default='pending')}; "
                f"gazebo_spawned={_format_flag(obstacle.get('gazebo_obstacle_model_spawned'), default='pending')}"
            )
    if recovery_detail or recovery_evidence_path:
        lines.append(
            f"{recovery_label}: "
            f"action={_status_text(recovery_action)}; "
            f"ack={_format_flag(recovery_ack, default='pending')}; "
            f"return={_format_distance(recovery_return_progress)}; "
            f"final_landing_safe={_format_flag(recovery_final_landing_safe, default='pending')}; "
            f"observation_lost={_format_flag(recovery_observation_lost, default='pending')}"
        )
        if (
            recovery_disarm_observed is not None
            or recovery_latest_ground_confirmed is not None
            or force_disarm_no_ground_confirmation is not None
        ):
            lines.append(
                "Recovery Grounding: "
                f"disarm_observed={_format_flag(recovery_disarm_observed, default='pending')}; "
                f"latest_ground_confirmed={_format_flag(recovery_latest_ground_confirmed, default='pending')}; "
                "force_disarm_no_ground_confirmation="
                f"{_format_flag(force_disarm_no_ground_confirmation, default='pending')}"
            )
    lines.append(
        "Claims: "
        f"delivery_completion={_format_flag(delivery_completion, default='False')}; "
        f"physical_execution={_format_flag(physical_execution, default='False')}"
    )
    if monitor_stop != "-":
        lines.append(f"Monitor stop: {monitor_stop}")
    if recovery_evidence_path:
        evidence_label = "Recovery evidence" if recovery_was_guard_response else "Return evidence"
        lines.append(f"{evidence_label}: {recovery_evidence_path}")
    if failed_receipt:
        lines.extend(["", f"Failure: {_status_text(failed_receipt.get('failure_reason'))}"])
    elif task_status == "running":
        if monitor_window_ended:
            next_text = (
                "Next: wait for the Gateway terminal receipt, then rerun `missionos job-status`."
            )
        elif operator_recovery_hint:
            next_text = (
                "Next: use the operator recovery command above only with operator approval, "
                "or wait and rerun `missionos job-status`."
            )
        else:
            next_text = "Next: wait and rerun `missionos job-status`, or use recovery only if the operator intends LAND/RTL."
        lines.extend(["", next_text])
    return lines


def _timeline_events(timeline_payload: dict[str, Any]) -> list[dict[str, Any]]:
    events = timeline_payload.get("events")
    if isinstance(events, list):
        return [event for event in events if isinstance(event, dict)]
    entries = timeline_payload.get("entries")
    if isinstance(entries, list):
        return [entry for entry in entries if isinstance(entry, dict)]
    timeline = timeline_payload.get("timeline")
    if isinstance(timeline, list):
        return [event for event in timeline if isinstance(event, dict)]
    return []


def _timeline_time_text(value: Any) -> str:
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(timespec="seconds")
    return _status_text(value)


def _timeline_detail_text(event: dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    changes = payload.get("changes") if isinstance(payload.get("changes"), dict) else {}
    artifacts = changes.get("artifacts") if isinstance(changes.get("artifacts"), dict) else {}
    agent_bridge = artifacts.get("missionos_runtime_recovery_agent_live_bridge")
    if isinstance(agent_bridge, dict):
        result = agent_bridge.get("runtime_recovery_agent_result")
        result = result if isinstance(result, dict) else {}
        assessment = result.get("assessment")
        assessment = assessment if isinstance(assessment, dict) else {}
        action = (
            assessment.get("selected_bounded_action")
            or assessment.get("recommended_action")
            or assessment.get("recovery_action")
            or "-"
        )
        risks = assessment.get("observed_risk_reasons") or assessment.get("trigger_reasons")
        risk_text = (
            ",".join(str(item) for item in risks)
            if isinstance(risks, list)
            else _status_text(risks)
        )
        return (
            "agent proposal: "
            f"{_status_text(result.get('runtime_status') or agent_bridge.get('bridge_status'))}; "
            f"action={_status_text(action)}; risk_observed={risk_text}"
        )
    snapshot = artifacts.get("missionos_auto_mission_runtime_snapshot")
    if isinstance(snapshot, dict):
        reached = snapshot.get("mission_reached_seq")
        total = snapshot.get("waypoint_total")
        return (
            f"{_format_duration(snapshot.get('elapsed_seconds'))}; "
            f"{_format_distance(snapshot.get('progress_m'))}; "
            f"wp {_status_text(reached)}/{_status_text(total)}; "
            f"battery {_format_percent(snapshot.get('battery_remaining_percent'))}"
        )
    failed = artifacts.get("missionos_auto_mission_gui_dispatch_failed_receipt")
    if isinstance(failed, dict):
        return "blocked: " + _status_text(failed.get("failure_reason"))
    detail = event.get("detail") or event.get("summary")
    if detail is None:
        detail = payload.get("error") or payload.get("status")
    if isinstance(detail, dict):
        return _status_text(
            detail.get("status")
            or detail.get("after")
            or detail.get("reason")
            or detail.get("message")
            or detail.get("artifact_ref")
        )
    return _status_text(detail)


def _fmt_metres(value: Any) -> str:
    metres = _as_float(value)
    if metres is None:
        return "-"
    if abs(metres) < 0.5:
        metres = 0.0
    if abs(metres) >= 1000.0:
        return f"{metres / 1000.0:.2f}km"
    return f"{metres:.0f}m"


def _fmt_signed_metres(value: Any) -> str:
    metres = _as_float(value)
    if metres is None:
        return "-"
    if abs(metres) < 0.5:
        metres = 0.0
    prefix = "+" if metres >= 0 else ""
    return f"{prefix}{_fmt_metres(metres)}"


def _operate_altitude_text(
    snapshot: dict[str, Any],
    artifacts: dict[str, Any],
) -> str:
    """Show altitude references explicitly: AMSL, home-relative, and AGL."""
    alt_home = _as_float(snapshot.get("altitude_above_home_m"))
    terrain = _as_float(snapshot.get("terrain_elevation_m"))
    clearance = _as_float(snapshot.get("terrain_clearance_m"))
    target = _as_float(snapshot.get("terrain_clearance_target_m"))
    margin = _as_float(snapshot.get("terrain_clearance_margin_m"))

    samples, _planned_route_m = _terrain_profile_samples_for_watch(artifacts)
    first_terrain = samples[0]["terrain_elevation_m"] if samples else None
    current_amsl = (
        terrain + clearance
        if terrain is not None and clearance is not None
        else first_terrain + alt_home
        if first_terrain is not None and alt_home is not None
        else None
    )
    destination_target_amsl = next(
        (
            sample.get("target_amsl_m")
            for sample in reversed(samples)
            if sample.get("target_amsl_m") is not None
        ),
        None,
    )
    climb_to_destination = (
        destination_target_amsl - current_amsl
        if destination_target_amsl is not None and current_amsl is not None
        else None
    )

    parts: list[str] = []
    if current_amsl is not None:
        parts.append(f"alt={_fmt_metres(current_amsl)} AMSL")
    if alt_home is not None:
        parts.append(f"alt(home)={_fmt_signed_metres(alt_home)}")
    if clearance is not None or target is not None:
        agl = f"AGL={_fmt_metres(clearance)}"
        if target is not None:
            agl += f"/target {_fmt_metres(target)}"
        if margin is not None:
            agl += f" (margin {_fmt_signed_metres(margin)})"
        parts.append(agl)
    if destination_target_amsl is not None:
        destination = f"dest={_fmt_metres(destination_target_amsl)} AMSL"
        if climb_to_destination is not None:
            destination += f"/climb {_fmt_signed_metres(climb_to_destination)}"
        parts.append(destination)
    return " · ".join(parts) if parts else "alt=-"


def _terrain_profile_samples_for_watch(
    artifacts: dict[str, Any],
) -> tuple[list[dict[str, float]], float | None]:
    compilation = artifacts.get("missionos_auto_mission_compilation")
    compilation = compilation if isinstance(compilation, dict) else {}
    route = artifacts.get("mission_designer_coordinate_pair_route")
    route = route if isinstance(route, dict) else {}
    raw_profile = compilation.get("terrain_clearance_profile")
    if not raw_profile:
        raw_profile = route.get("terrain_profile")
    if not isinstance(raw_profile, list):
        return [], None

    planned_route_m = _as_float(
        compilation.get("planned_route_m")
        or route.get("planned_route_m")
        or route.get("derived_route_distance_m")
    )
    if planned_route_m is None:
        distances = [
            _as_float(sample.get("distance_m"))
            for sample in raw_profile
            if isinstance(sample, dict)
        ]
        distances = [distance for distance in distances if distance is not None]
        planned_route_m = max(distances) if distances else None

    target_clearance = _as_float(
        compilation.get("terrain_clearance_target_m")
        or route.get("terrain_clearance_agl_m")
        or route.get("terrain_clearance_target_m")
    )
    first_terrain = None
    samples: list[dict[str, float]] = []
    for sample in raw_profile:
        if not isinstance(sample, dict):
            continue
        terrain = _as_float(sample.get("terrain_elevation_m"))
        if terrain is None:
            continue
        if first_terrain is None:
            first_terrain = terrain
        distance = _as_float(sample.get("distance_m"))
        fraction = _as_float(sample.get("fraction"))
        if fraction is None and distance is not None and planned_route_m:
            fraction = distance / planned_route_m
        if fraction is None:
            continue
        mission_altitude = _as_float(sample.get("mission_altitude_m"))
        sample_target = _as_float(sample.get("target_clearance_m")) or target_clearance
        if mission_altitude is not None and first_terrain is not None:
            target_amsl = first_terrain + mission_altitude
        elif sample_target is not None:
            target_amsl = terrain + sample_target
        else:
            target_amsl = None
        normalized = {
            "fraction": min(1.0, max(0.0, fraction)),
            "terrain_elevation_m": terrain,
        }
        if distance is not None:
            normalized["distance_m"] = distance
        if target_amsl is not None:
            normalized["target_amsl_m"] = target_amsl
        samples.append(normalized)
    samples.sort(key=lambda item: item["fraction"])
    return samples, planned_route_m
