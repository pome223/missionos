"""Operator-facing console projections for MissionOS CLI results."""

from __future__ import annotations

from typing import Any
import json

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .job_status import (
    _fmt_metres,
    _format_duration,
    _format_flag,
    _job_operator_summary,
    _operator_recovery_ack_text,
    _status_text,
    _task_artifacts,
    _timeline_detail_text,
    _timeline_events,
    _timeline_time_text,
)


console = Console()


def _safe_get(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _print_status(payloads: dict[str, dict[str, Any]], *, base_url: str) -> None:
    table = Table(
        title=f"MissionOS Gateway: {base_url}",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Surface", style="cyan")
    table.add_column("Status")
    table.add_column("Key Detail", no_wrap=True)

    health = payloads.get("health", {})
    table.add_row(
        "Gateway",
        _status_text(health.get("status"), "reachable"),
        _status_text(health.get("session_backend") or health.get("version")),
    )

    form2a = payloads.get("form2a", {})
    table.add_row(
        "Plan",
        _status_text(form2a.get("summary_status")),
        _status_text(form2a.get("selected_response_kind")),
    )

    review = payloads.get("review", {})
    table.add_row(
        "Human Review",
        _status_text(review.get("summary_status")),
        _status_text(_safe_get(review, "human_operator_review", "review_status")),
    )

    action = payloads.get("action", {})
    blocking = _safe_get(action, "authority_boundary", "blocking_reasons")
    table.add_row(
        "Execution",
        _status_text(action.get("summary_status")),
        ", ".join(str(item) for item in blocking or []) or "-",
    )

    repair = payloads.get("repair", {})
    table.add_row(
        "Repair",
        _status_text(repair.get("summary_status")),
        _status_text(_safe_get(repair, "repair_proposal", "repair_target")),
    )
    console.print(table)


def _print_conversation_result(payload: dict[str, Any]) -> None:
    message = _status_text(payload.get("message"), "MissionOS handled the instruction.")
    routed_action = _status_text(payload.get("routed_action"))
    routing_source = _status_text(payload.get("routing_source"))
    progress = payload.get("progress_counted")
    lines = [
        f"[bold]MissionOS[/bold]: {message}",
        f"route={routed_action}; source={routing_source}; progress_counted={progress}",
    ]

    operation = payload.get("operation_result")
    payload_split_plan = payload.get("missionos_payload_split_plan")
    if isinstance(operation, dict):
        summary = operation.get("summary") if isinstance(operation.get("summary"), dict) else {}
        status = (
            summary.get("status")
            or operation.get("summary_status")
            or operation.get("response_status")
        )
        if status:
            lines.append(f"operation_status={status}")
        if not isinstance(payload_split_plan, dict) or not payload_split_plan:
            payload_split_plan = operation.get("missionos_payload_split_plan")
        repair = operation.get("repair_proposal")
        if isinstance(repair, dict):
            target = repair.get("repair_target")
            if target:
                lines.append(f"repair_target={_status_text(target)}")
            instruction = repair.get("proposed_operator_instruction")
            if instruction:
                lines.append(f"repair_instruction={_status_text(instruction)}")
            parameters = repair.get("proposed_parameters")
            if isinstance(parameters, dict) and parameters:
                lines.append(
                    "repair_parameters="
                    + ", ".join(f"{key}={value}" for key, value in parameters.items())
                )
        repair_warnings = operation.get("repair_followup_warnings")
        if isinstance(repair_warnings, list):
            for warning in repair_warnings:
                if warning:
                    lines.append(f"repair_warning={_status_text(warning)}")
    if isinstance(payload_split_plan, dict) and payload_split_plan:
        sorties = payload_split_plan.get("sorties")
        payload_values = [
            sortie.get("payload_weight_kg")
            for sortie in (sorties if isinstance(sorties, list) else [])
            if isinstance(sortie, dict)
        ]
        if payload_values:
            min_payload = min(payload_values)
            max_payload = max(payload_values)
            per_sortie = (
                f"{max_payload}kg"
                if min_payload == max_payload
                else f"{min_payload}-{max_payload}kg"
            )
        else:
            per_sortie = "-"
        lines.append(
            "payload_split="
            f"{_status_text(payload_split_plan.get('plan_status'))}; "
            f"requested_total={payload_split_plan.get('requested_payload_weight_kg')}kg; "
            f"sorties={payload_split_plan.get('sortie_count')}; "
            f"per_sortie={per_sortie}; planning_only=True"
        )

    repair_prompt = payload.get("missionos_repair_prompt")
    if isinstance(repair_prompt, dict) and repair_prompt:
        reasons = repair_prompt.get("blocking_reasons")
        if isinstance(reasons, list) and reasons:
            lines.append(
                "repair_prompt=Mission blocked: "
                + ", ".join(str(reason) for reason in reasons)
            )
        prompt_text = repair_prompt.get("operator_prompt")
        if prompt_text:
            lines.append(_status_text(prompt_text))

    form2a = payload.get("form2a_ai_agent")
    if isinstance(form2a, dict):
        selection = form2a.get("selection") if isinstance(form2a.get("selection"), dict) else {}
        review = form2a.get("review") if isinstance(form2a.get("review"), dict) else {}
        action = form2a.get("action") if isinstance(form2a.get("action"), dict) else {}
        details = [
            f"selection={_status_text(selection.get('summary_status'))}",
            f"review={_status_text(review.get('summary_status'))}",
            f"action={_status_text(action.get('summary_status'))}",
        ]
        selected = selection.get("selected_response_kind")
        if selected:
            details.append(f"selected={selected}")
        lines.append("; ".join(details))

    console.print(Panel("\n".join(lines), title="Conversation", border_style="cyan"))


def _recovery_runner_observation_lines(task_payload: dict[str, Any] | None) -> list[str]:
    if not isinstance(task_payload, dict):
        return []
    snapshot = _task_artifacts(task_payload).get("missionos_auto_mission_runtime_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    if not snapshot:
        return []
    observed = _format_flag(
        snapshot.get("operator_recovery_request_observed"),
        default="pending",
    )
    ack = _operator_recovery_ack_text(
        observed=snapshot.get("operator_recovery_command_ack_observed"),
        result=snapshot.get("operator_recovery_command_ack_result"),
    )
    lines = [
        f"runner_observed={observed}; runner_ack={ack}; "
        f"nav_state={_status_text(snapshot.get('nav_state'))}; "
        f"home={_fmt_metres(snapshot.get('distance_to_home_m'))}"
    ]
    parameters = snapshot.get("operator_recovery_parameters")
    if isinstance(parameters, dict) and parameters:
        lines.append(
            "runner_parameters="
            + ", ".join(f"{key}={value}" for key, value in sorted(parameters.items()))
        )
    if snapshot.get("post_abort_tracking") is True:
        lines.append(
            f"tracking={_status_text(snapshot.get('operator_recovery_path'))}; "
            f"landed={_status_text(snapshot.get('landed'))}; "
            f"arming={_status_text(snapshot.get('arming_state'))}; "
            f"post_abort={_format_duration(snapshot.get('post_abort_elapsed_seconds'))}"
        )
        outcome = snapshot.get("post_abort_outcome_status")
        if outcome:
            lines.append(
                f"outcome={_status_text(outcome)}; "
                f"home_delta={_fmt_metres(snapshot.get('post_abort_home_distance_delta_m'))}; "
                f"alt_delta={_fmt_metres(snapshot.get('post_abort_altitude_delta_m'))}"
            )
    if any(
        snapshot.get(key) is not None
        for key in (
            "operator_recovery_assist_attempted",
            "operator_recovery_assist_status",
            "operator_recovery_target_reached",
            "operator_recovery_resume_auto_status",
        )
    ):
        assist_ack = _operator_recovery_ack_text(
            observed=snapshot.get(
                "operator_recovery_assist_offboard_ack_observed"
            ),
            result=snapshot.get("operator_recovery_assist_offboard_ack_result"),
        )
        lines.append(
            "assist="
            f"{_status_text(snapshot.get('operator_recovery_assist_status'))}; "
            f"kind={_status_text(snapshot.get('operator_recovery_assist_kind'))}; "
            f"offboard_ack={assist_ack}; "
            f"offboard_state={_status_text(snapshot.get('operator_recovery_assist_offboard_state_observed'))}; "
            f"nav={_status_text(snapshot.get('operator_recovery_assist_offboard_nav_state'))}; "
            f"setpoints={_status_text(snapshot.get('operator_recovery_assist_setpoint_frames_sent'))}; "
            f"target={_status_text(snapshot.get('operator_recovery_target_reached'))}; "
            f"resume={_status_text(snapshot.get('operator_recovery_resume_auto_status'))}"
        )
        if (
            snapshot.get(
                "operator_recovery_assist_low_altitude_disarm_ack_observed"
            )
            is not None
        ):
            disarm_ack = _operator_recovery_ack_text(
                observed=snapshot.get(
                    "operator_recovery_assist_low_altitude_disarm_ack_observed"
                ),
                result=snapshot.get(
                    "operator_recovery_assist_low_altitude_disarm_ack_result"
                ),
            )
            lines.append(f"assist_disarm_ack={disarm_ack}")
        if (
            snapshot.get(
                "operator_recovery_assist_low_altitude_force_disarm_ack_observed"
            )
            is not None
        ):
            force_disarm_ack = _operator_recovery_ack_text(
                observed=snapshot.get(
                    "operator_recovery_assist_low_altitude_force_disarm_ack_observed"
                ),
                result=snapshot.get(
                    "operator_recovery_assist_low_altitude_force_disarm_ack_result"
                ),
            )
            lines.append(f"assist_force_disarm_ack={force_disarm_ack}")
    return lines


def _print_recovery_result(
    payload: dict[str, Any],
    *,
    task_payload: dict[str, Any] | None = None,
) -> None:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    dispatch_status = summary.get("dispatch_status") or payload.get("response_status")
    ack = summary.get("command_ack_result_name") or "-"
    runner_abort = "observed" if summary.get("runner_abort_observed") is True else "not observed yet"
    blocked = summary.get("blocked_reasons") if isinstance(summary.get("blocked_reasons"), list) else []
    active_runner_queued = summary.get("active_runner_request_queued") is True
    lines = [
        f"dispatch_status={_status_text(dispatch_status)}",
        f"recovery_action={_status_text(summary.get('recovery_action'))}",
        f"ACK={ack}; runner_abort={runner_abort}",
        "delivery/progress/physical claim=false",
    ]
    if "recovery_completion_claimed" in summary:
        lines[2] = (
            "recovery_completion_claimed="
            f"{summary.get('recovery_completion_claimed')}; "
            "route_resumed_after_recovery="
            f"{summary.get('route_resumed_after_recovery')}; "
            "route_completed_after_recovery="
            f"{summary.get('route_completed_after_recovery')}"
        )
    recovery_parameters = summary.get("recovery_parameters")
    if isinstance(recovery_parameters, dict) and recovery_parameters:
        parameter_text = ", ".join(
            f"{key}={value}" for key, value in sorted(recovery_parameters.items())
        )
        lines.insert(2, f"recovery_parameters={parameter_text}")
    if active_runner_queued:
        lines.insert(
            2,
            "active_runner_request=queued; polling runner ACK/effect before this panel",
        )
    lines.extend(_recovery_runner_observation_lines(task_payload))
    if blocked:
        lines.append("blocked_reasons=" + ", ".join(str(item) for item in blocked))
    console.print(Panel("\n".join(lines), title="Runtime Recovery", border_style="yellow"))


def _print_sitl_execution_result(payload: dict[str, Any]) -> None:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    blocked = summary.get("blocked_reasons") if isinstance(summary.get("blocked_reasons"), list) else []
    lines = [
        f"task_id={_status_text(summary.get('task_id'))}",
        f"task_status={_status_text(summary.get('task_status'))}",
        f"upload_status={_status_text(summary.get('upload_status'))}",
        f"live_flight_status={_status_text(summary.get('live_flight_status'))}",
        f"dropoff_verified={summary.get('dropoff_verified')} "
        "(phase5 monitor-telemetry gate)",
        f"delivery_completion_claimed={summary.get('delivery_completion_claimed')}",
        f"physical_execution_invoked={summary.get('physical_execution_invoked')}",
    ]
    if blocked:
        lines.append("blocked_reasons=" + ", ".join(str(item) for item in blocked))
    console.print(Panel("\n".join(lines), title="Execute Live SITL", border_style="green"))


def _print_sitl_start_result(payload: dict[str, Any]) -> None:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    readiness = payload.get("px4_gazebo_sitl_execution_readiness")
    if not isinstance(readiness, dict):
        readiness = {}
    blocked = (
        readiness.get("blocked_reasons")
        if isinstance(readiness.get("blocked_reasons"), list)
        else []
    )
    lines = [
        f"task_id={_status_text(summary.get('task_id'))}",
        f"startup_status={_status_text(summary.get('startup_status'))}",
        f"container={_status_text(summary.get('container_name'))}",
        f"readiness_status={_status_text(summary.get('readiness_status') or readiness.get('readiness_status'))}",
        f"mavlink_endpoint_observed={readiness.get('mavlink_endpoint_observed')}",
        "mission_upload_performed=false",
        "live_flight_runner_invoked=false",
    ]
    if blocked:
        lines.append("blocked_reasons=" + ", ".join(str(item) for item in blocked))
    console.print(Panel("\n".join(lines), title="Start SITL", border_style="blue"))


def _print_job_status(
    task_payload: dict[str, Any],
    timeline_payload: dict[str, Any],
) -> None:
    console.print(
        Panel(
            "\n".join(_job_operator_summary(task_payload)),
            title="MissionOS Job",
            border_style="magenta",
        )
    )
    events = _timeline_events(timeline_payload)
    if not events:
        return
    table = Table(title="Recent Progress", show_header=True, header_style="bold cyan")
    table.add_column("Time", no_wrap=True)
    table.add_column("Event")
    table.add_column("Status")
    table.add_column("What Changed")
    for event in events:
        table.add_row(
            _timeline_time_text(
                event.get("created_at") or event.get("observed_at") or event.get("timestamp")
            ),
            _status_text(event.get("event_type") or event.get("type") or event.get("name")),
            _status_text(event.get("status")),
            _timeline_detail_text(event),
        )
    console.print(table)
