"""Read-only job, timeline, recovery, and altitude status projections."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import math

from .battery_truth import battery_truth_model


_TERMINAL_TASK_STATUSES = frozenset(
    {"completed", "recovered", "blocked", "failed", "cancelled", "canceled"}
)


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


def _battery_display_text(
    *,
    snapshot: dict[str, Any],
    artifacts: dict[str, Any],
    diagnostics: bool = True,
) -> str:
    model = battery_truth_model(snapshot=snapshot, artifacts=artifacts)
    text = _format_percent(model.get("display_percent"))
    if text == "-" or not diagnostics:
        return text
    if model.get("status") == "suspect_reset":
        return (
            f"{text} trusted (reported "
            f"{_format_percent(model.get('reported_percent'))}; reset rejected)"
        )
    if model.get("status") == "sample_rejected":
        return f"{text} trusted (latest sample rejected)"
    if model.get("status") == "source_missing":
        return f"{text} (source unverified)"
    return text


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


def _turtlebot_indoor_map_from_artifacts(
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    """Return the newest saved indoor-map artifact without changing it."""

    summary = artifacts.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    embedded = summary.get("turtlebot3_indoor_map_model")
    if isinstance(embedded, dict):
        return embedded
    execution = artifacts.get("turtlebot3_home_mission_execution")
    execution = execution if isinstance(execution, dict) else {}
    embedded = execution.get("turtlebot3_indoor_map_model")
    if isinstance(embedded, dict):
        return embedded
    direct = artifacts.get("turtlebot3_indoor_map_model")
    return direct if isinstance(direct, dict) else {}


def _parent_mission_record_from_artifacts(
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    """Return an explicitly stored parent-mission record without inventing one."""

    for key in (
        "missionos_parent_mission_run_record",
        "parent_mission_run_record",
    ):
        record = artifacts.get(key)
        if isinstance(record, dict):
            return record
    return {}


def _artifact_mapping(
    artifacts: dict[str, Any],
    *keys: str,
) -> dict[str, Any]:
    """Return the first explicitly stored mapping for the requested keys."""

    for key in keys:
        value = artifacts.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _artifact_mapping_present(
    artifacts: dict[str, Any],
    *keys: str,
) -> bool:
    """Return whether any requested artifact key stores a mapping, even empty."""

    return any(isinstance(artifacts.get(key), dict) for key in keys)


def _stored_list_text(
    value: dict[str, Any],
    key: str,
    *,
    limit: int = 4,
) -> str:
    """Distinguish an explicitly empty stored list from a missing field."""

    stored = value.get(key)
    if not isinstance(stored, list):
        return "unknown"
    return _compact_values([str(item) for item in stored], limit=limit)


def _physical_ai_control_tower_lines(
    artifacts: dict[str, Any],
    *,
    parent_record: dict[str, Any],
    coordinator: dict[str, Any],
) -> list[str]:
    """Project stored cross-layer facts without inventing missing evidence."""

    receipt = _artifact_mapping(
        artifacts,
        "virtual_to_real_promotion_receipt",
        "v2r_promotion_receipt",
    )
    receipt_present = _artifact_mapping_present(
        artifacts,
        "virtual_to_real_promotion_receipt",
        "v2r_promotion_receipt",
    )
    promotion = _artifact_mapping(
        artifacts,
        "virtual_to_real_promotion_validation",
        "v2r_promotion_validation",
    )
    safe_stop = _artifact_mapping(
        artifacts,
        "safe_stop_receipt_validation",
    )
    # No parent-mission repair or operator-intervention artifact is standardized
    # yet.  Keep those facts unknown instead of projecting backend-local shapes.
    repair: dict[str, Any] = {}
    intervention: dict[str, Any] = {}
    if not any((parent_record, receipt, promotion, safe_stop, repair, intervention)):
        return []

    raw_gaps = receipt.get("gaps")
    gaps_recorded = isinstance(raw_gaps, list) and all(
        isinstance(gap, dict) for gap in raw_gaps
    )
    gaps = list(raw_gaps) if gaps_recorded else []
    resolved_gaps = (
        str(sum(gap.get("status") == "resolved" for gap in gaps))
        if gaps_recorded
        else "unknown"
    )
    unresolved_gap_ids = [
        str(gap.get("gap_id") or "unknown") for gap in gaps if gap.get("status") != "resolved"
    ]
    unresolved_gap_text = (
        _compact_values(unresolved_gap_ids) if gaps_recorded else "unknown"
    )
    gap_denominator = str(len(gaps)) if gaps_recorded else "unknown"
    promotion_reasons = promotion.get("reasons")
    promotion_reasons_text = (
        _compact_values([str(reason) for reason in promotion_reasons], limit=4)
        if isinstance(promotion_reasons, list)
        else "unknown"
    )

    physical_execution = _first_present(
        coordinator.get("physical_execution_invoked"),
        parent_record.get("physical_execution_invoked"),
        promotion.get("physical_execution_invoked"),
    )
    physical_safety = _first_present(
        promotion.get("physical_safety_claimed"),
        parent_record.get("physical_safety_claimed"),
    )
    operational_closure = _first_present(
        intervention.get("operational_closure_created"),
        coordinator.get("operational_closure_created"),
        parent_record.get("operational_closure_created"),
    )

    return [
        "",
        "Physical AI Control Tower:",
        (
            "  Current: "
            "stage="
            f"{_status_text(_first_present(coordinator.get('current_stage_ref'), parent_record.get('current_stage_ref')), default='unknown')}"
        ),
        (
            "  Promotion: "
            f"receipt={'present' if receipt_present else 'absent'}; "
            f"status={_status_text(promotion.get('status'), default='unknown')}; "
            "prerequisite="
            f"{_format_flag(promotion.get('promotion_prerequisite_satisfied'), default='unknown')}; "
            f"source={_status_text(receipt.get('source_scope'), default='unknown')}; "
            f"target={_status_text(receipt.get('target_scope'), default='unknown')}; "
            "executor="
            f"{_short_digest(receipt.get('target_executor_profile_sha256'))}; "
            "controller="
            f"{_short_digest(receipt.get('target_controller_profile_sha256'))}"
        ),
        (
            "  Promotion approval: "
            f"source={_status_text(receipt.get('approval_artifact_ref'), default='unknown')}; "
            f"approver={_status_text(receipt.get('approved_by'), default='unknown')}; "
            f"expires={_status_text(receipt.get('expires_at'), default='unknown')}; "
            "deployment_authority="
            f"{_format_flag(promotion.get('physical_deployment_authority_present'), default='unknown')}"
        ),
        (
            "  Promotion gaps: "
            f"resolved={resolved_gaps}/{gap_denominator}; "
            f"unresolved={unresolved_gap_text}; "
            f"rollback={_stored_list_text(receipt, 'rollback_condition_ids')}; "
            f"disable={_stored_list_text(receipt, 'disable_condition_ids')}"
        ),
        (
            "  Safe stop: "
            f"request={_format_flag(safe_stop.get('request_observed'), default='unknown')}; "
            f"ack={_format_flag(safe_stop.get('ack_observed'), default='unknown')}; "
            f"effect={_format_flag(safe_stop.get('effect_observed'), default='unknown')}; "
            f"status={_status_text(safe_stop.get('status'), default='unknown')}"
        ),
        (
            "  Repair: "
            f"requested={_format_flag(repair.get('requested'), default='unknown')}; "
            f"approved={_format_flag(repair.get('approved'), default='unknown')}; "
            f"result={_status_text(repair.get('result'), default='unknown')}"
        ),
        (
            "  Operator: "
            "intervention="
            f"{_format_flag(intervention.get('intervention_observed'), default='unknown')}; "
            "operational_closure="
            f"{_format_flag(operational_closure, default='unknown')}; "
            f"reason={_status_text(intervention.get('closure_reason'), default='unknown')}"
        ),
        (
            "  Physical: "
            "deployment_authority="
            f"{_format_flag(promotion.get('physical_deployment_authority_present'), default='unknown')}; "
            f"execution={_format_flag(physical_execution, default='unknown')}; "
            f"safety={_format_flag(physical_safety, default='unknown')}"
        ),
        (
            "  Control boundary: "
            "parent_completion="
            f"{_format_flag(_first_present(coordinator.get('mission_completion_claimed'), parent_record.get('mission_completion_claimed')), default='unknown')}; "
            f"promotion_reasons={promotion_reasons_text}"
        ),
    ]


def _is_parent_mission_job(task_payload: dict[str, Any]) -> bool:
    task = _task_record(task_payload)
    if task.get("kind") == "parent_mission_execution":
        return True
    return bool(_parent_mission_record_from_artifacts(_task_artifacts(task_payload)))


def _is_vla_mission_job(task_payload: dict[str, Any]) -> bool:
    task = _task_record(task_payload)
    artifacts = _task_artifacts(task_payload)
    return task.get("kind") == "vla_mission_execution" or isinstance(
        artifacts.get("missionos_vla_mission_run_record"), dict
    )


def _vla_mission_job_operator_summary(
    task_payload: dict[str, Any],
) -> list[str]:
    """Show the exact VLA stage facts without promoting simulator success."""

    task = _task_record(task_payload)
    artifacts = _task_artifacts(task_payload)
    record = artifacts.get("missionos_vla_mission_run_record")
    record = record if isinstance(record, dict) else {}
    proposal = artifacts.get("physical_ai_mission_proposal")
    proposal = proposal if isinstance(proposal, dict) else {}
    approval = artifacts.get("physical_ai_mission_approval")
    approval = approval if isinstance(approval, dict) else {}
    evaluation = record.get("predicate_evaluation")
    evaluation = evaluation if isinstance(evaluation, dict) else {}
    recovery = artifacts.get("missionos_vla_recovery_state")
    recovery = recovery if isinstance(recovery, dict) else {}
    execution_mode = _status_text(
        record.get("execution_mode"),
        default="unknown",
    )
    headline_label = "VLA Fixture" if execution_mode == "fixture" else "VLA Mission"
    headline_suffix = (
        "; live execution not observed" if execution_mode == "fixture" else ""
    )
    return [
        (
            f"{headline_label}: "
            f"task={task.get('task_id')}; status="
            f"{_status_text(task.get('status'), default='unknown')}"
            f"{headline_suffix}"
        ),
        (
            "  Frozen: "
            f"kind={_status_text(proposal.get('mission_kind'), default='unknown')}; "
            f"run={_status_text(record.get('run_identity') or proposal.get('parent_run_identity'), default='unknown')}; "
            f"episode={_status_text(record.get('episode_identity') or proposal.get('episode_identity'), default='unknown')}; "
            f"contract={_short_digest(record.get('contract_sha256'))}; "
            f"approval={_short_digest(approval.get('approval_sha256'))}"
        ),
        (
            "  Observed: "
            f"mode={execution_mode}; "
            "readiness="
            f"{_status_text(evaluation.get('evidence_readiness'), default='unknown')}; "
            f"content={_short_digest(evaluation.get('observation_content_sha256'))}"
        ),
        (
            "  Completion predicate: "
            f"package={_status_text(evaluation.get('predicate_package_id'), default='unknown')}@"
            f"{_status_text(evaluation.get('predicate_package_version'), default='unknown')}; "
            f"status={_status_text(evaluation.get('status'), default='unknown')}; "
            f"basis={_status_text(evaluation.get('actual_verification_basis'), default='unknown')}"
        ),
        (
            "  Bounded outcome: "
            f"claimed={_format_flag(record.get('bounded_outcome_claimed'), default='unknown')}; "
            f"scope={_status_text(evaluation.get('outcome_claim_scope'), default='unknown')}"
        ),
        (
            "  Recovery: "
            f"status={_status_text(recovery.get('recovery_status'), default='unknown')}; "
            f"action={_status_text(recovery.get('repair_action'), default='unknown')}; "
            f"proposal={_status_text(recovery.get('proposal_status'), default='unknown')}; "
            f"retry_task={_status_text(recovery.get('retry_task_id'), default='unknown')}"
        ),
        (
            "  Recovery boundary: "
            "in_episode="
            f"{_format_flag(recovery.get('in_episode_intervention_available'), default='unknown')}; "
            "post_episode="
            f"{_format_flag(recovery.get('post_episode_repair_implemented'), default='unknown')}; "
            "automatic_retry="
            f"{_format_flag(recovery.get('automatic_retry_allowed'), default='unknown')}; "
            "dispatch_authority="
            f"{_format_flag(recovery.get('dispatch_authority_created'), default='unknown')}"
        ),
        (
            "  Unconfirmed: "
            "controller_ack="
            f"{_format_flag(record.get('controller_ack_observed'), default='unknown')}; "
            "parent_completion="
            f"{_format_flag(record.get('mission_completion_claimed'), default='unknown')}; "
            "physical_execution="
            f"{_format_flag(record.get('physical_execution_invoked'), default='unknown')}"
        ),
    ]


def _is_turtlebot_nav2_job(task_payload: dict[str, Any]) -> bool:
    task = _task_record(task_payload)
    if task.get("kind") == "turtlebot3_home_mission_execution":
        return True
    return bool(_turtlebot_indoor_map_from_artifacts(_task_artifacts(task_payload)))


def _short_digest(value: Any) -> str:
    text = str(value or "").strip()
    return f"{text[:12]}..." if len(text) > 12 else (text or "-")


def _compact_values(values: list[str], *, limit: int = 4) -> str:
    unique = list(dict.fromkeys(value for value in values if value))
    if not unique:
        return "-"
    visible = unique[:limit]
    suffix = f",+{len(unique) - limit}" if len(unique) > limit else ""
    return ",".join(visible) + suffix


def _mission_contract_job_status_lines(artifacts: dict[str, Any]) -> list[str]:
    """Project stored Mission Contract facts without creating stronger claims."""

    execution = artifacts.get("turtlebot3_home_mission_execution")
    execution = execution if isinstance(execution, dict) else {}
    stored_segments = execution.get("segment_results")
    segments = (
        [item for item in stored_segments if isinstance(item, dict)]
        if isinstance(stored_segments, list)
        else []
    )
    if not segments:
        return []

    summary = artifacts.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    route_authority = execution.get("route_authority")
    route_authority = route_authority if isinstance(route_authority, dict) else {}
    predicates = [
        evaluation
        for segment in segments
        if isinstance(
            evaluation := segment.get("mission_contract_predicate_evaluation"),
            dict,
        )
    ]
    contracts = [
        contract
        for segment in segments
        if isinstance(contract := segment.get("mission_contract"), dict)
    ]
    stored_transitions = execution.get("segment_transition_authority_records")
    transitions = (
        [item for item in stored_transitions if isinstance(item, dict)]
        if isinstance(stored_transitions, list)
        else []
    )

    contract_ids = [str(contract.get("contract_id") or "") for contract in contracts]
    observed_results = sum(bool(segment.get("result_observed_at")) for segment in segments)
    observed_sources: list[str] = []
    if any(
        isinstance(segment.get("bridge_responses"), list) and bool(segment.get("bridge_responses"))
        for segment in segments
    ):
        observed_sources.append("nav2_bridge_response")
    if any(
        isinstance(segment.get("adapter_evidence"), dict) and bool(segment.get("adapter_evidence"))
        for segment in segments
    ):
        observed_sources.append("adapter_evidence")
    content_bound_results = sum(
        1 for predicate in predicates if predicate.get("observation_content_sha256")
    )
    evidence_origins = [
        str(origin)
        for predicate in predicates
        for origin in (
            predicate.get("evidence_origins")
            if isinstance(predicate.get("evidence_origins"), list)
            else []
        )
    ]
    predicate_packages = [
        (f"{predicate.get('predicate_package_id')}@{predicate.get('predicate_package_version')}")
        for predicate in predicates
        if predicate.get("predicate_package_id")
    ]
    evaluated_predicates = sum(
        predicate.get("predicate_package_evaluated") is True for predicate in predicates
    )
    satisfied_predicates = sum(
        predicate.get("evaluated_outcome_claim") is True for predicate in predicates
    )
    alternatives = [str(predicate.get("satisfied_alternative") or "") for predicate in predicates]
    verification_bases = [
        str(predicate.get("actual_verification_basis") or "") for predicate in predicates
    ]

    completion_claimed = _first_present(
        summary.get("completion_claimed"),
        execution.get("completion_claimed"),
    )
    completion_scope = _first_present(
        summary.get("completion_scope"),
        execution.get("completion_scope"),
    )
    stored_planned_segment_count = route_authority.get("planned_segment_count")
    expected_transition_count = (
        stored_planned_segment_count
        if isinstance(stored_planned_segment_count, int)
        and not isinstance(stored_planned_segment_count, bool)
        and stored_planned_segment_count > 0
        else len(segments)
    )
    authorized_transitions = sum(
        transition.get("transition_status") == "authorized" for transition in transitions
    )
    authority_sources = [
        str(transition.get("dispatch_authority_source") or "") for transition in transitions
    ]
    approval_refs = [
        str(transition.get("operator_approval_ref") or "") for transition in transitions
    ]

    stored_blocking_reasons = summary.get("blocking_reasons")
    blocking_reasons = (
        [str(reason) for reason in stored_blocking_reasons]
        if isinstance(stored_blocking_reasons, list)
        else []
    )
    for predicate in predicates:
        if predicate.get("evaluated_outcome_claim") is True:
            continue
        predicate_reasons = predicate.get("reasons")
        if isinstance(predicate_reasons, list):
            blocking_reasons.extend(str(reason) for reason in predicate_reasons)
    stored_unproven_claims = summary.get("unproven_claims")
    unconfirmed = (
        [str(claim) for claim in stored_unproven_claims]
        if isinstance(stored_unproven_claims, list)
        else []
    )
    for field, label in (
        ("physical_execution_invoked", "physical_execution"),
        ("mission_delivery_completion_claimed", "mission_delivery_completion"),
        ("payload_delivery_completion_claimed", "payload_delivery_completion"),
        ("cleaning_completion_claimed", "cleaning_completion"),
        ("whole_home_loop_completion_claimed", "whole_home_loop_completion"),
    ):
        if field in summary:
            claim_value = summary.get(field)
        elif field in execution:
            claim_value = execution.get(field)
        else:
            claim_value = None
        if claim_value is False:
            unconfirmed.append(label)
        elif claim_value is not True:
            unconfirmed.append(f"{label}:unknown")

    return [
        "",
        "Mission Contract:",
        (
            "  Frozen: "
            f"contracts={len(contracts)}; "
            f"contract_ids={_compact_values(contract_ids, limit=2)}; "
            "route_segments="
            f"{_status_text(route_authority.get('planned_segment_count'))}; "
            "route_authority="
            f"{_short_digest(route_authority.get('route_authority_sha256'))}"
        ),
        (
            "  Observed: "
            f"runtime_results={observed_results}/{len(segments)}; "
            f"sources={_compact_values(observed_sources)}; "
            f"content_bound={content_bound_results}/{len(segments)}; "
            f"origins={_compact_values(evidence_origins)}"
        ),
        (
            "  Completion predicate: "
            f"packages={_compact_values(predicate_packages, limit=2)}; "
            f"evaluated={evaluated_predicates}/{len(segments)}; "
            f"satisfied={satisfied_predicates}/{len(segments)}; "
            f"alternatives={_compact_values(alternatives)}"
        ),
        (
            "  Bounded outcome: "
            f"claimed={_format_flag(completion_claimed, default='pending')}; "
            f"scope={_status_text(completion_scope)}; "
            f"basis={_compact_values(verification_bases)}"
        ),
        (
            "  Transition authority: "
            f"authorized={authorized_transitions}/{expected_transition_count}; "
            f"source={_compact_values(authority_sources)}; "
            f"approval={_compact_values(approval_refs, limit=2)}; "
            "route_authority="
            f"{_short_digest(route_authority.get('route_authority_sha256'))}"
        ),
        (
            "  Unconfirmed: "
            f"claims={_compact_values(unconfirmed, limit=6)}; "
            f"reasons={_compact_values(blocking_reasons, limit=4)}"
        ),
    ]


def _parent_mission_job_operator_summary(
    task_payload: dict[str, Any],
) -> list[str]:
    """Show stored parent-stage facts without synthesizing a parent outcome."""

    task = _task_record(task_payload)
    artifacts = _task_artifacts(task_payload)
    stored_record = _parent_mission_record_from_artifacts(artifacts)
    coordinator = stored_record.get("coordinator_record")
    if not isinstance(coordinator, dict):
        coordinator = (
            stored_record
            if stored_record.get("schema_version") == "missionos_parent_mission_run_record.v1"
            else {}
        )

    task_status = _status_text(task.get("status") or task.get("task_status"))
    coordinator_status = _status_text(
        coordinator.get("coordinator_status"),
        default="unknown",
    )
    raw_stage_records = coordinator.get("stage_records")
    stage_records = (
        [record for record in raw_stage_records if isinstance(record, dict)]
        if isinstance(raw_stage_records, list)
        else []
    )
    raw_stage_count = coordinator.get("stage_count")
    stage_count = (
        raw_stage_count
        if isinstance(raw_stage_count, int)
        and not isinstance(raw_stage_count, bool)
        and raw_stage_count > 0
        else None
    )
    stage_count_text = str(stage_count) if stage_count is not None else "unknown"
    satisfied_count = sum(
        isinstance(record.get("stage_result"), dict)
        and record["stage_result"].get("predicate_satisfied") is True
        for record in stage_records
    )
    execution_mode = _status_text(
        stored_record.get("execution_mode"),
        default="unknown",
    )

    if (
        coordinator_status == "stages_satisfied"
        and stage_count is not None
        and satisfied_count == stage_count
    ):
        if execution_mode == "fixture":
            headline = (
                f"Fixture Stages Satisfied: {satisfied_count}/{stage_count_text}; "
                "live execution not observed; parent mission completion "
                "remains unverified"
            )
        else:
            headline = (
                f"Stages Satisfied: {satisfied_count}/{stage_count_text}; "
                "parent mission completion remains unverified"
            )
    elif coordinator_status == "blocked":
        headline = (
            f"Blocked: parent mission stopped after "
            f"{satisfied_count}/{stage_count_text} satisfied stages"
        )
    else:
        headline = (
            f"Parent Mission: coordinator={coordinator_status}; "
            f"stages={satisfied_count}/{stage_count_text}"
        )

    parent_mission_id = _first_present(
        coordinator.get("parent_mission_id"),
        stored_record.get("parent_mission_id"),
    )
    parent_mission_sha256 = _first_present(
        coordinator.get("parent_mission_sha256"),
        stored_record.get("parent_mission_sha256"),
    )
    approval_binding_sha256 = _first_present(
        coordinator.get("approval_binding_sha256"),
        stored_record.get("approval_binding_sha256"),
    )
    shared_target_descriptor_sha256 = stored_record.get("shared_target_descriptor_sha256")
    frozen_at = stored_record.get("parent_contract_frozen_at")
    lines = [
        headline,
        f"Task: {task.get('task_id')}  ({task_status})",
        "",
        "Parent Mission:",
        (
            "  Frozen: "
            f"id={_status_text(parent_mission_id, default='unknown')}; "
            f"contract={_short_digest(parent_mission_sha256)}; "
            f"approval={_short_digest(approval_binding_sha256)}; "
            f"target={_short_digest(shared_target_descriptor_sha256)}; "
            f"frozen_at={_status_text(frozen_at, default='unknown')}; "
            f"mode={execution_mode}; "
            f"stages={stage_count_text}"
        ),
    ]

    records_by_index = {
        index: record
        for record in stage_records
        if isinstance(index := record.get("stage_index"), int)
        and not isinstance(index, bool)
        and index > 0
    }
    display_indices = (
        list(range(1, stage_count + 1)) if stage_count is not None else sorted(records_by_index)
    )
    for stage_index in display_indices:
        record = records_by_index.get(stage_index)
        stage_denominator = stage_count_text
        if not isinstance(record, dict):
            lines.extend(
                [
                    (
                        f"  Stage {stage_index}/{stage_denominator}: "
                        "ref=unknown; executor=unknown; record=missing"
                    ),
                    ("    Observed: readiness=unknown; content_bound=unknown"),
                    (
                        "    Completion predicate: package=unknown; "
                        "status=unknown; scope=unknown; basis=unknown"
                    ),
                    (
                        "    Transition authority: status=unknown; "
                        "source=unknown; prerequisite=unknown"
                    ),
                ]
            )
            continue

        evaluation = record.get("predicate_evaluation")
        evaluation = evaluation if isinstance(evaluation, dict) else {}
        stage_result = record.get("stage_result")
        stage_result = stage_result if isinstance(stage_result, dict) else {}
        stored_transition = record.get("transition_authority")
        transition = stored_transition if isinstance(stored_transition, dict) else {}
        package_id = evaluation.get("predicate_package_id")
        package_version = evaluation.get("predicate_package_version")
        package = (
            f"{package_id}@{package_version}"
            if package_id and package_version
            else str(package_id or "unknown")
        )
        content_bound = True if evaluation.get("observation_content_sha256") else None
        stored_origins = evaluation.get("evidence_origins")
        evidence_origins = (
            [str(origin) for origin in stored_origins] if isinstance(stored_origins, list) else []
        )
        if (
            stage_index == 1
            and "prerequisite_stage_ref" in transition
            and transition.get("prerequisite_stage_ref") is None
        ):
            prerequisite = "not_applicable"
        else:
            prerequisite = _format_flag(
                transition.get("prerequisite_predicate_satisfied"),
                default="unknown",
            )
        lines.extend(
            [
                (
                    f"  Stage {stage_index}/{stage_denominator}: "
                    f"ref={_status_text(record.get('stage_ref'), default='unknown')}; "
                    "executor="
                    f"{_status_text(record.get('executor_ref'), default='unknown')}; "
                    "controller="
                    f"{_status_text(record.get('controller_ref'), default='unknown')}; "
                    "contract="
                    f"{_short_digest(stage_result.get('child_contract_sha256'))}"
                ),
                (
                    "    Observed: "
                    "readiness="
                    f"{_status_text(evaluation.get('evidence_readiness'), default='unknown')}; "
                    "content_bound="
                    f"{_format_flag(content_bound, default='unknown')}; "
                    "content="
                    f"{_short_digest(evaluation.get('observation_content_sha256'))}; "
                    "origins="
                    f"{_compact_values(evidence_origins)}"
                ),
                (
                    "    Completion predicate: "
                    f"package={package}; "
                    "status="
                    f"{_status_text(stage_result.get('predicate_status'), default='unknown')}; "
                    "scope="
                    f"{_status_text(evaluation.get('outcome_claim_scope'), default='unknown')}; "
                    "basis="
                    f"{_status_text(stage_result.get('actual_verification_basis'), default='unknown')}"
                ),
                (
                    "    Transition authority: "
                    "status="
                    f"{_status_text(transition.get('transition_status'), default='unknown')}; "
                    "present="
                    f"{_format_flag(transition.get('dispatch_authority_present'), default='unknown')}; "
                    "source="
                    f"{_status_text(transition.get('dispatch_authority_source'), default='unknown')}; "
                    f"prerequisite={prerequisite}"
                ),
            ]
        )

    mission_completion_claimed = _first_present(
        coordinator.get("mission_completion_claimed"),
        stored_record.get("mission_completion_claimed"),
    )
    mission_completion_status = coordinator.get("mission_completion_status")
    blocking_reasons = coordinator.get("blocking_reasons")
    blocking_reasons = (
        [str(reason) for reason in blocking_reasons] if isinstance(blocking_reasons, list) else []
    )
    unconfirmed: list[str] = []
    for field, label in (
        ("identity_continuity_claimed", "identity_continuity"),
        ("shared_world_claimed", "shared_world"),
        ("physical_execution_invoked", "physical_execution"),
    ):
        if field in coordinator:
            value = coordinator.get(field)
        elif field in stored_record:
            value = stored_record.get(field)
        else:
            value = None
        if value is False:
            unconfirmed.append(label)
        elif value is not True:
            unconfirmed.append(f"{label}:unknown")

    lines.extend(
        [
            (
                "  Parent outcome: "
                f"stages_satisfied={satisfied_count}/{stage_count_text}; "
                "claimed="
                f"{_format_flag(mission_completion_claimed, default='unknown')}; "
                "status="
                f"{_status_text(mission_completion_status, default='unknown')}"
            ),
            (
                "  Unconfirmed: "
                f"claims={_compact_values(unconfirmed, limit=6)}; "
                f"reasons={_compact_values(blocking_reasons, limit=4)}"
            ),
        ]
    )
    lines.extend(
        _physical_ai_control_tower_lines(
            artifacts,
            parent_record=stored_record,
            coordinator=coordinator,
        )
    )
    return lines


def _turtlebot_job_operator_summary(task_payload: dict[str, Any]) -> list[str]:
    """Build a truthful TB3/TB4 status view from saved task artifacts.

    This is a presentation-only projection.  It deliberately avoids PX4/SITL
    vocabulary and never upgrades approval, dispatch, motion, or completion
    claims beyond the values already present in the task artifacts.
    """

    task = _task_record(task_payload)
    artifacts = _task_artifacts(task_payload)
    summary = artifacts.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    indoor_map = _turtlebot_indoor_map_from_artifacts(artifacts)
    recovery = indoor_map.get("recovery")
    recovery = recovery if isinstance(recovery, dict) else {}
    checkpoint = artifacts.get("turtlebot3_recovery_checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    decision = artifacts.get("turtlebot3_recovery_decision_summary")
    decision = decision if isinstance(decision, dict) else {}
    receipt = artifacts.get("missionos_runtime_recovery_dispatch_receipt")
    receipt = receipt if isinstance(receipt, dict) else {}
    planner = summary.get("recovery_planner_result")
    planner = planner if isinstance(planner, dict) else {}
    invocation = planner.get("llm_invocation_evidence")
    invocation = invocation if isinstance(invocation, dict) else {}

    task_status = _status_text(task.get("status") or task.get("task_status"))
    robot_label = _status_text(
        indoor_map.get("robot_label") or summary.get("robot_label"),
        default="TurtleBot3",
    )
    recovery_triggered = _first_present(
        summary.get("runtime_recovery_triggered"), recovery.get("triggered")
    )
    route_completed = _first_present(
        summary.get("route_completed_after_recovery"),
        decision.get("route_completed_after_recovery"),
        receipt.get("route_completed_after_recovery"),
    )
    checkpoint_status = checkpoint.get("checkpoint_status")
    if task_status == "completed" and route_completed is True:
        headline = f"Complete: {robot_label}/Nav2 simulated route finished after Recovery"
    elif task_status == "completed":
        headline = f"Complete: Gateway recorded a terminal {robot_label}/Nav2 simulator result"
    elif task_status == "running":
        headline = f"Running: {robot_label}/Nav2 simulator telemetry is updating"
    elif task_status == "pending" and checkpoint_status == "awaiting_operator_approval":
        headline = f"Waiting: {robot_label} is stopped for an operator Recovery decision"
    elif task_status == "blocked":
        headline = f"Blocked: {robot_label}/Nav2 stopped without verified completion"
    else:
        headline = f"Status: {task_status} ({robot_label}/Nav2 simulator)"

    completed_segments = _first_present(
        summary.get("segment_completion_count"),
        recovery.get("route_segment_completion_count"),
    )
    planned_segments = _first_present(
        summary.get("planned_segment_count"),
        recovery.get("route_segment_planned_count"),
    )
    route_resumed = _first_present(
        summary.get("route_resumed_after_recovery"),
        receipt.get("route_resumed_after_recovery"),
        recovery.get("route_resumed_after_recovery"),
    )
    recovery_complete = _first_present(
        summary.get("recovery_completion_claimed"),
        receipt.get("recovery_completion_claimed"),
        recovery.get("recovery_completion_claimed"),
    )
    selected_action = _first_present(
        checkpoint.get("selected_action"),
        decision.get("selected_action"),
        receipt.get("recovery_action"),
        recovery.get("selected_action"),
    )
    receipt_action = _first_present(
        receipt.get("recovery_action"),
        recovery.get("selected_action"),
    )
    dispatch_sent = _first_present(
        summary.get("recovery_dispatch_request_sent"),
        receipt.get("recovery_dispatch_request_sent"),
    )
    operator_approved = _first_present(
        receipt.get("explicit_recovery_dispatch_approval"),
        receipt.get("operator_approved"),
    )
    approval = receipt.get("turtlebot3_recovery_operator_approval")
    approval = approval if isinstance(approval, dict) else {}
    approval_source = approval.get("approval_source")
    checkpoint_id = str(checkpoint.get("checkpoint_id") or "")
    checkpoint_hash = str(checkpoint.get("checkpoint_hash") or "")
    approval_checkpoint_id = str(approval.get("checkpoint_id") or "")
    approval_checkpoint_hash = str(approval.get("checkpoint_hash") or "")
    approval_matches_current_checkpoint = bool(operator_approved) and (
        not checkpoint_id
        or (
            approval_checkpoint_id == checkpoint_id
            and (not checkpoint_hash or approval_checkpoint_hash == checkpoint_hash)
        )
    )
    awaiting_new_recovery_decision = (
        checkpoint_status == "awaiting_operator_approval"
        and bool(checkpoint_id)
        and not approval_matches_current_checkpoint
    )
    recovery_goal_status = _first_present(
        summary.get("recovery_goal_status"), recovery.get("goal_status")
    )
    recovery_verification = _first_present(
        summary.get("recovery_verification_status"),
        recovery.get("verification_status"),
    )
    route_resume_status = _first_present(
        summary.get("route_resume_status"), recovery.get("route_resume_status")
    )
    proposal_source = _first_present(
        decision.get("recovery_proposal_source"), planner.get("proposal_source")
    )
    model_text = _status_text(invocation.get("model_id"))
    provider_text = _status_text(invocation.get("provider"))
    odom_delta_m = _first_numeric(summary.get("odom_delta_m"))
    observed_points = indoor_map.get("observed_points")
    observed_count = len(observed_points) if isinstance(observed_points, list) else 0
    recovery_points = recovery.get("observed_points")
    recovery_observed_count = len(recovery_points) if isinstance(recovery_points, list) else 0
    obstacles = indoor_map.get("obstacles")
    obstacle_count = len(obstacles) if isinstance(obstacles, list) else 0
    obstacle_clearance_values = [
        obstacle.get("trajectory_clearance_observed")
        for obstacle in obstacles or []
        if isinstance(obstacle, dict)
        and isinstance(obstacle.get("trajectory_clearance_observed"), bool)
    ]
    obstacle_intersection_values = [
        obstacle.get("trajectory_intersects_obstacle")
        for obstacle in obstacles or []
        if isinstance(obstacle, dict)
        and isinstance(obstacle.get("trajectory_intersects_obstacle"), bool)
    ]
    obstacle_clearance = _first_present(
        indoor_map.get("obstacle_clearance_observed"),
        all(obstacle_clearance_values) if obstacle_clearance_values else None,
    )
    obstacle_intersects = _first_present(
        indoor_map.get("observed_path_intersects_obstacle"),
        any(obstacle_intersection_values) if obstacle_intersection_values else None,
    )

    recovery_lines: list[str]
    if recovery_triggered is not True:
        recovery_lines = [
            "Recovery Dispatch: not triggered",
            "Recovery Outcome: not applicable",
            "Recovery Judgment: not requested",
        ]
    elif awaiting_new_recovery_decision:
        recovery_lines = [
            (
                "Current Recovery Decision: "
                f"status={_status_text(checkpoint_status)}; "
                f"action={_status_text(selected_action)}; "
                "checkpoint_approval=False; "
                "dispatch_authority="
                f"{_format_flag(checkpoint.get('dispatch_authority_created'), default='False')}"
            ),
            (
                "Previous Recovery Attempt: "
                f"status={_status_text(receipt.get('dispatch_status'))}; "
                f"action={_status_text(receipt_action)}; "
                f"request_sent={_format_flag(dispatch_sent)}; "
                f"checkpoint_approval={_format_flag(operator_approved)}; "
                f"approval_source={_status_text(approval_source)}"
            ),
            (
                "Previous Recovery Outcome: "
                f"goal={_status_text(recovery_goal_status)}; "
                f"verification={_status_text(recovery_verification)}; "
                f"resume={_status_text(route_resume_status)}; "
                f"completion={_format_flag(recovery_complete)}"
            ),
            (
                "Current Recovery Judgment: "
                f"source={_status_text(proposal_source)}; "
                f"provider={provider_text}; model={model_text}; "
                "dispatch_authority=False"
            ),
        ]
    else:
        recovery_lines = [
            (
                "Recovery Dispatch: "
                f"status={_status_text(receipt.get('dispatch_status'))}; "
                f"action={_status_text(receipt_action or selected_action)}; "
                f"request_sent={_format_flag(dispatch_sent)}; "
                "checkpoint_approval="
                f"{_format_flag(approval_matches_current_checkpoint)}; "
                "approval_source="
                f"{_status_text(approval_source if approval_matches_current_checkpoint else None)}"
            ),
            (
                "Recovery Outcome: "
                f"goal={_status_text(recovery_goal_status)}; "
                f"verification={_status_text(recovery_verification)}; "
                f"resume={_status_text(route_resume_status)}; "
                f"completion={_format_flag(recovery_complete)}"
            ),
            (
                "Recovery Judgment: "
                f"source={_status_text(proposal_source)}; "
                f"provider={provider_text}; model={model_text}; "
                "dispatch_authority=False"
            ),
        ]

    lines = [
        headline,
        f"Task: {task.get('task_id')}  ({task_status})",
        f"Robot: {robot_label}/Nav2 simulator",
        "",
        (
            "Route: "
            f"segments={_status_text(completed_segments)}/{_status_text(planned_segments)}; "
            f"resumed_after_recovery={_format_flag(route_resumed)}; "
            f"completed_after_recovery={_format_flag(route_completed)}"
        ),
        *recovery_lines,
        (
            "Motion: "
            f"observed={_format_flag(summary.get('robot_motion_observed'))}; "
            f"odom={odom_delta_m:.2f} m; "
            if odom_delta_m is not None
            else "Motion: observed=-; odom=-; "
        )
        + f"saved_samples={observed_count}; recovery_samples={recovery_observed_count}",
        (
            "Obstacles: "
            f"count={obstacle_count}; "
            f"clearance={_format_flag(obstacle_clearance)}; "
            f"intersects={_format_flag(obstacle_intersects)}"
        ),
        "Battery: not observed by this TurtleBot/Nav2 evidence path",
        *_mission_contract_job_status_lines(artifacts),
        (
            "Claims: "
            f"completion_scope={_status_text(summary.get('completion_scope'))}; "
            f"sim_action_completion={_format_flag(summary.get('completion_claimed'))}; "
            "delivery_completion="
            f"{_format_flag(summary.get('mission_delivery_completion_claimed'), default='False')}; "
            "physical_execution="
            f"{_format_flag(summary.get('physical_execution_invoked'), default='False')}"
        ),
    ]
    return lines


def _job_operator_summary(task_payload: dict[str, Any]) -> list[str]:
    if _is_parent_mission_job(task_payload):
        return _parent_mission_job_operator_summary(task_payload)
    if _is_vla_mission_job(task_payload):
        return _vla_mission_job_operator_summary(task_payload)
    if _is_turtlebot_nav2_job(task_payload):
        return _turtlebot_job_operator_summary(task_payload)

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
    safety_hold = artifacts.get("missionos_runtime_recovery_safety_hold_receipt")
    safety_hold = safety_hold if isinstance(safety_hold, dict) else {}

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
    replay_latest = replay.get("latest_sample")
    replay_latest = replay_latest if isinstance(replay_latest, dict) else {}
    if task_status in _TERMINAL_TASK_STATUSES and _as_bool(replay.get("dropoff_verified")) is True:
        # A later post-run return snapshot must not replace the terminal
        # outbound/dropoff evidence in the mission progress line. Return and
        # landing evidence are reported separately below.
        progress_m = _first_numeric(
            replay.get("horizontal_progress_m"),
            replay_latest.get("horizontal_progress_m"),
            progress_m,
        )
        elapsed_seconds = _first_numeric(
            replay.get("elapsed_seconds"),
            replay_latest.get("elapsed_s"),
            elapsed_seconds,
        )
        reached_seq = (
            _as_int(
                _first_present(replay.get("mission_reached_seq"), replay_latest.get("seq_reached"))
            )
            or reached_seq
        )
        current_seq = (
            _as_int(
                _first_present(
                    replay.get("mission_current_seq"),
                    replay_latest.get("mission_current_seq"),
                )
            )
            or current_seq
        )
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
    if task_status in _TERMINAL_TASK_STATUSES:
        eta_seconds = None
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
    battery_text = _battery_display_text(
        snapshot=snapshot,
        artifacts=artifacts,
    )
    terrain_clearance_m = _as_float(snapshot.get("terrain_clearance_m"))
    terrain_clearance_target_m = _as_float(snapshot.get("terrain_clearance_target_m"))
    terrain_clearance_margin_m = _as_float(snapshot.get("terrain_clearance_margin_m"))
    terrain_clearance_status = _status_text(snapshot.get("terrain_clearance_status"))
    terrain_display_landed = snapshot.get("landed") is True or snapshot.get("maybe_landed") is True
    if terrain_display_landed:
        # Minimum AGL is a flight-envelope predicate.  Once ground contact is
        # observed, showing the touchdown AGL as a live minimum-clearance
        # breach makes a safe terminal state look unsafe.
        terrain_clearance_status = "landed_not_applicable"
    monitor_stop = _status_text(snapshot.get("monitor_stop_reason"))
    readiness_text = _status_text(readiness.get("readiness_status"))
    missionos_fixture = metadata.get("missionos_fixture") is True
    actual_sitl_evidence = _first_present(
        metadata.get("actual_sitl_flight_evidence_observed"),
        replay.get("actual_sitl_flight_evidence_observed"),
    )
    dropoff_verified = _first_present(
        dropoff_gate.get("dropoff_verified"),
        sitl_delivery_gate.get("dropoff_verified"),
        replay.get("dropoff_verified"),
        metadata.get("dropoff_verified"),
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
    snapshot_ground_contact = snapshot.get("ground_contact")
    snapshot_has_ground_signal = any(
        value is not None
        for value in (
            snapshot_landed,
            snapshot_maybe_landed,
            snapshot_ground_contact,
        )
    )
    snapshot_ground_confirmed = any(
        value is True
        for value in (
            snapshot_landed,
            snapshot_maybe_landed,
            snapshot_ground_contact,
        )
    )
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
        # The terminal runtime snapshot is newer than the pre-closeout return
        # projection.  Do not let stale false values contradict observed
        # ground contact, landing, and disarm in the same operator panel.
        recovery_latest_ground_confirmed = snapshot_ground_confirmed
    if snapshot_disarmed:
        recovery_disarm_observed = True
    snapshot_land_ack_observed = (
        snapshot.get("operator_recovery_command_ack_observed") is True
        and snapshot.get("operator_recovery_command_ack_result") == 0
    )
    if (
        "land" in recovery_action_text
        and snapshot_land_ack_observed
        and snapshot_landed is True
        and snapshot_ground_confirmed
        and snapshot_disarmed
    ):
        recovery_final_landing_safe = True
        force_disarm_no_ground_confirmation = False
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
        "dropoff_verified="
        f"{_format_flag(dropoff_verified, default='pending')}"
        " (phase5 monitor-telemetry gate); "
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
    if safety_hold:
        hold_observed = safety_hold.get("request_status") == "observed" or (
            snapshot.get("operator_recovery_action") == "safety_hold"
            and snapshot.get("operator_recovery_assist_status") == "safety_hold_observed"
        )
        lines.insert(
            3 if process_status_text else 2,
            "Safety HOLD: "
            f"status={'observed' if hold_observed else _status_text(safety_hold.get('request_status'))}; "
            "source=preauthorized local-conflict policy; "
            "at_hold_operator_approved=false; at_hold_recovery_dispatch=false; "
            "later dispatch is reported separately",
        )
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
            f"battery {_battery_display_text(snapshot=snapshot, artifacts=artifacts)}"
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
