"""Read-only operator views for `missionos operate`."""

from __future__ import annotations

import json
from typing import Any

from rich.console import Group
from rich.markup import escape as rich_escape
from rich.panel import Panel
from rich.text import Text

from .mission_assurance_projection import mission_assurance_projection
from .job_status import (
    _as_float,
    _as_int,
    _battery_display_text,
    _fmt_metres,
    _format_distance,
    _format_percent,
    _operate_altitude_text,
    _operator_recovery_console_command,
    _status_text,
    _task_artifacts,
)
from .map_model import (
    _turtlebot3_core_feasibility_from_artifacts,
    _turtlebot3_indoor_map_model_from_artifacts,
    _turtlebot3_recovery_candidate_resolution_from_artifacts,
    _turtlebot_robot_label_from_artifacts,
    _turtlebot_robot_profile_from_artifacts,
)
from .operate_commands import _humanize_risks
from .vla_operator import _is_vla_operator_task, _render_vla_operator_panel


def _projection_computed(projection: dict[str, Any]) -> bool:
    return projection.get("projection_status") == "computed"


def _is_turtlebot3_task_artifacts(artifacts: dict[str, Any]) -> bool:
    return bool(_turtlebot_robot_profile_from_artifacts(artifacts))


def _humanize_recovery_summary(
    proposal: dict[str, Any],
    endurance: dict[str, Any],
    return_home: dict[str, Any],
) -> list[str]:
    """Plain-language situation + return feasibility + recommendation for a human."""
    route_computed = _projection_computed(endurance)
    rtl_computed = _projection_computed(return_home)
    needs = _as_float(endurance.get("projected_battery_required_percent"))
    route_arrival = _as_float(endurance.get("projected_arrival_battery_percent"))
    route_infeasible = route_computed and (
        (needs is not None and needs > 100.0)
        or (route_arrival is not None and route_arrival < 0.0)
    )
    rtl_insufficient = (
        rtl_computed and return_home.get("projected_insufficient_for_return_home") is True
    )
    rtl_arrival = _as_float(return_home.get("projected_return_arrival_battery_percent"))
    home_m = return_home.get("distance_to_home_m")

    lines: list[str] = []
    if not route_computed:
        lines.append(
            "[yellow]Situation:[/yellow] Route battery projection is unavailable "
            f"({_status_text(endurance.get('projection_status'))})."
        )
    elif route_infeasible and needs is not None:
        lines.append(
            f"[bold red]Situation:[/bold red] This route cannot be completed "
            f"(requires about {needs / 100.0:.1f}x the available battery; "
            "continuing risks depletion)."
        )
    elif route_infeasible:
        lines.append("[bold red]Situation:[/bold red] This route cannot be completed (battery shortfall).")
    else:
        lines.append("[green]Situation:[/green] The route appears battery-feasible.")
    if proposal.get("risks"):
        lines.append(f"[dim]Detected:[/dim] {_humanize_risks(proposal['risks'])}.")
    if return_home:
        if not rtl_computed:
            home_txt = f" (home {_fmt_metres(home_m)})" if home_m is not None else ""
            lines.append(
                "[yellow]Return:[/yellow] RTL battery projection is unavailable"
                f"{home_txt}."
            )
        elif not rtl_insufficient:
            extra = f"; arrival battery {rtl_arrival:.0f}%" if rtl_arrival is not None else ""
            home_txt = (
                f" (home {_fmt_metres(home_m)}{extra})" if home_m is not None else ""
            )
            lines.append(f"[green]Return:[/green] Returning now appears safe{home_txt}.")
        else:
            lines.append("[bold red]Return:[/bold red] Battery is also tight for RTL.")
    if proposal.get("risks"):
        rec = "[bold]-> Operator review required; continuing is not recommended until active risks are resolved.[/bold]"
    elif not route_computed:
        rec = "[bold]-> Operator review required; do not treat route battery as verified.[/bold]"
    elif route_infeasible and return_home and rtl_computed and not rtl_insufficient:
        rec = "[bold]-> RTL (`missionos rtl`) is usually appropriate. Continuing is not recommended.[/bold]"
    elif route_infeasible and (rtl_insufficient or not rtl_computed):
        rec = "[bold]-> Consider LAND (`missionos land`); RTL battery margin is also tight.[/bold]"
    else:
        rec = "[bold]-> Continuing appears acceptable; the proposal is advisory.[/bold]"
    if proposal.get("action") == "operator_review":
        rec += " [dim](the agent leaves the final decision to the operator)[/dim]"
    lines.append(rec)
    return lines


def _render_recovery_agent_console(
    task_payload: dict[str, Any],
    *,
    proposal: dict[str, Any] | None,
    show_proposal: bool,
    status: str,
    task_id: str = "",
    pending: dict[str, Any] | None = None,
) -> Panel:
    """Operator console for the Runtime Recovery Agent: recognition + proposal + how to act.

    Rendered at the top of `operate` so it is always visible (never scrolled off).
    """
    artifacts = _task_artifacts(task_payload)
    is_home_robot = _is_turtlebot3_task_artifacts(artifacts)
    robot_label = _turtlebot_robot_label_from_artifacts(artifacts)
    summary = artifacts.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    bridge = artifacts.get("missionos_runtime_recovery_agent_live_bridge")
    bridge = bridge if isinstance(bridge, dict) else {}
    telemetry = bridge.get("telemetry_snapshot")
    telemetry = telemetry if isinstance(telemetry, dict) else {}
    battery = telemetry.get("battery") if isinstance(telemetry.get("battery"), dict) else {}
    endurance = battery.get("endurance_projection")
    endurance = endurance if isinstance(endurance, dict) else {}
    return_home = battery.get("return_home_projection")
    return_home = return_home if isinstance(return_home, dict) else {}
    checkpoint = artifacts.get("turtlebot3_recovery_checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    checkpoint_status = str(checkpoint.get("checkpoint_status") or "")
    candidate_resolution = (
        _turtlebot3_recovery_candidate_resolution_from_artifacts(artifacts)
    )
    core_feasibility = _turtlebot3_core_feasibility_from_artifacts(artifacts)

    lines: list[str] = []
    safety_hold = artifacts.get("missionos_runtime_recovery_safety_hold_receipt")
    safety_hold = safety_hold if isinstance(safety_hold, dict) else {}
    snapshot = artifacts.get("missionos_auto_mission_runtime_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    if safety_hold and not is_home_robot and status in {"running", "pending"}:
        hold_observed = (
            snapshot.get("operator_recovery_action") == "safety_hold"
            and snapshot.get("operator_recovery_assist_status")
            == "safety_hold_observed"
        )
        lines.extend(
            [
                "[bold yellow]Aircraft safety HOLD active — human Recovery "
                "decision required[/bold yellow]"
                if hold_observed
                else "[bold yellow]Aircraft safety HOLD queued — awaiting PX4 "
                "observation[/bold yellow]",
                "[dim]The HOLD came from the preauthorized local-conflict safety "
                "policy. It is not LLM approval, human approval, Recovery "
                "success, or mission progress.[/dim]",
                "",
            ]
        )
    if checkpoint_status == "dispatching":
        action = str(checkpoint.get("selected_action") or "recovery action")
        lines.extend(
            [
                "[bold cyan]Approved Recovery workflow is in progress[/bold cyan]",
                f"Action: [bold]{rich_escape(action)}[/bold]",
                "[green]A fresh operator approval is bound to this checkpoint.[/green]",
                "[dim]Nav2 goal="
                f"{rich_escape(_status_text(summary.get('recovery_goal_status')))}; "
                "verification="
                f"{rich_escape(_status_text(summary.get('recovery_verification_status')))}; "
                "route="
                f"{rich_escape(_status_text(summary.get('route_resume_status')))}[/dim]",
                "[dim]MissionOS is executing the approved recovery and any remaining "
                "route segments. The robot may pause while Nav2 replans or runs its "
                "local recovery behaviors. Do not approve the same checkpoint again.[/dim]",
            ]
        )
    elif pending:
        action = str(pending.get("selected_action") or "recovery action")
        operator_guidance_required = (
            pending.get("operator_guidance_required") is True
        )
        observations = pending.get("input_observations")
        observations = observations if isinstance(observations, dict) else {}
        reason = str(
            pending.get("proposal_reason")
            or observations.get("runtime_failure_source")
            or "runtime route failure"
        )
        selected_candidate = candidate_resolution.get("selected_candidate")
        selected_candidate = (
            selected_candidate if isinstance(selected_candidate, dict) else {}
        )
        planner = artifacts.get("recovery_planner_result")
        planner = planner if isinstance(planner, dict) else {}
        if not planner:
            planner = summary.get("recovery_planner_result")
            planner = planner if isinstance(planner, dict) else {}
        runtime_proposal_approval = (
            pending.get("runtime_proposal_approval_supported") is True
        )
        is_repair = bool(checkpoint.get("parent_checkpoint_id"))
        lines.extend(
            [
                "[bold yellow]Aircraft held — recovery decision required[/bold yellow]"
                if runtime_proposal_approval
                else "[bold yellow]Robot stopped — repair decision required[/bold yellow]"
                if is_repair
                else "[bold yellow]Robot stopped — recovery decision required[/bold yellow]",
                f"Recovery Agent proposes: [bold]{rich_escape(action)}[/bold]",
                f"[dim]Reason: {rich_escape(reason)}[/dim]",
            ]
        )
        if runtime_proposal_approval:
            proposal_origin = pending.get("proposal_origin")
            proposal_origin = (
                proposal_origin if isinstance(proposal_origin, dict) else {}
            )
            provider_model = "/".join(
                value
                for value in (
                    str(pending.get("llm_provider") or ""),
                    str(pending.get("llm_model_id") or ""),
                )
                if value
            )
            lines.append(
                "[dim]Proposal evidence: "
                f"source={rich_escape(_status_text(pending.get('proposal_source')))}; "
                f"origin={rich_escape(_status_text(proposal_origin.get('origin_kind')))}; "
                f"provider={rich_escape(provider_model or '-')}; "
                "source_proposal="
                f"{rich_escape(_status_text(proposal_origin.get('source_proposal_id')))}; "
                "obstacle="
                f"{rich_escape(_status_text(pending.get('source_obstacle_name')))}[/dim]"
            )
            feasibility = pending.get("action_feasibility")
            feasibility = feasibility if isinstance(feasibility, dict) else {}
            clearance = feasibility.get("obstacle_clearance_verification")
            clearance = clearance if isinstance(clearance, dict) else {}
            duration_model = feasibility.get("maneuver_duration_model")
            duration_model = duration_model if isinstance(duration_model, dict) else {}
            minimum_clearance_m = _as_float(clearance.get("minimum_clearance_m"))
            required_clearance_m = _as_float(clearance.get("required_clearance_m"))
            minimum_clearance_text = (
                f"{minimum_clearance_m:.1f}" if minimum_clearance_m is not None else "-"
            )
            required_clearance_text = (
                f"{required_clearance_m:.1f}" if required_clearance_m is not None else "-"
            )
            lines.append(
                "[dim]Action feasibility: "
                f"{rich_escape(_status_text(feasibility.get('feasibility_status')))}; "
                f"clearance={rich_escape(_status_text(clearance.get('verification_status')))} "
                f"(min={minimum_clearance_text}m, "
                f"required={required_clearance_text}m); "
                "performance="
                f"{rich_escape(_status_text(duration_model.get('performance_envelope_status')))}"
                "[/dim]"
            )
        else:
            lines.extend(
                [
                    "[dim]Proposal source: "
                    f"{rich_escape(_status_text(planner.get('proposal_source')))}; "
                    f"checkpoint={rich_escape(_status_text(checkpoint.get('checkpoint_id')))}; "
                    f"parent={rich_escape(_status_text(checkpoint.get('parent_checkpoint_id')))}[/dim]",
                    "[dim]Candidate validation: "
                    f"{rich_escape(_status_text(candidate_resolution.get('resolution_status')))}; "
                    f"candidate={rich_escape(_status_text(selected_candidate.get('candidate_id')))}; "
                    f"path={rich_escape(_status_text(selected_candidate.get('path_length_m')))}m; "
                    f"global_max_cost={rich_escape(_status_text(selected_candidate.get('maximum_path_cost')))}; "
                    f"local_max_cost={rich_escape(_status_text(selected_candidate.get('local_maximum_path_cost')))}; "
                    f"bounded_retreat={rich_escape(_status_text(candidate_resolution.get('bounded_retreat_required')))}; "
                    "core="
                    f"{rich_escape(_status_text(core_feasibility.get('candidate_status')))}[/dim]",
                ]
            )
        lines.append("[green]No recovery dispatch has been sent.[/green]")
        if operator_guidance_required:
            lines.extend(
                [
                    "",
                    "[bold yellow]Recovery Agent requested operator guidance; this "
                    "proposal-only checkpoint cannot dispatch.[/bold yellow]",
                    "  [bold]defer[/bold]    keep the robot stopped; create no authority",
                    "  type a bounded change in plain language, e.g. "
                    "[bold]右へ大きく迂回して障害物を避けて[/bold]",
                    "  [dim]approve is unavailable until that change creates a new "
                    "dispatchable checkpoint.[/dim]",
                ]
            )
        else:
            lines.extend(["", "[bold]Choose one:[/bold]"])
            lines.append(
                "  [bold green]approve[/bold green]  execute this exact recovery "
                "(asks y/N)"
            )
            lines.append(
                "  [bold]defer[/bold]    keep the aircraft held; create no authority"
                if runtime_proposal_approval
                else "  [bold]defer[/bold]    keep the robot stopped; create no authority"
            )
            if not runtime_proposal_approval:
                lines.extend(
                    [
                        "  type a change in plain language, e.g. "
                        "[bold]左へ大きく迂回して[/bold]",
                    ]
                )
    elif show_proposal and proposal:
        lines.extend(_humanize_recovery_summary(proposal, endurance, return_home))
        suggested = _operator_recovery_console_command(
            proposal.get("action"),
            proposal.get("parameters") if isinstance(proposal.get("parameters"), dict) else None,
        )
        if suggested:
            lines.append(
                "[bold yellow]Suggested command:[/bold yellow] "
                f"[bold]{suggested}[/bold] "
                "[dim](asks y/N before dispatch)[/dim]"
            )
        detail = (
            f"[dim]Details: proposal={proposal.get('action', '-')} "
            f"({proposal.get('status', '-')}; dispatch_authority=False); "
            f"risk={', '.join(proposal.get('risks', [])) or '-'}"
        )
        if endurance and _projection_computed(endurance):
            detail += (
                "; route "
                f"needs={_format_percent(endurance.get('projected_battery_required_percent'))}/"
                f"arrival={_format_percent(endurance.get('projected_arrival_battery_percent'))}/"
                f"burn={_format_percent(endurance.get('battery_burn_percent_per_km'))}per_km"
            )
        elif endurance:
            detail += (
                "; route projection="
                f"{_status_text(endurance.get('projection_status')) or 'unavailable'}"
            )
        if return_home and _projection_computed(return_home):
            detail += (
                "; RTL "
                f"home={_format_distance(return_home.get('distance_to_home_m'))}/"
                f"needs={_format_percent(return_home.get('projected_return_battery_required_percent'))}/"
                f"arrival={_format_percent(return_home.get('projected_return_arrival_battery_percent'))}"
            )
        elif return_home:
            detail += (
                "; RTL projection="
                f"{_status_text(return_home.get('projection_status')) or 'unavailable'}"
            )
        detail += "[/dim]"
        lines.append(detail)
    elif status == "running":
        lines.append(
            "[green]Mission running — no Recovery decision is pending.[/green]"
        )
        if is_home_robot:
            lines.append(
                "[dim]MissionOS is waiting for the current Nav2 result. If the robot "
                "stops and Recovery Agent creates a proposal, this panel will show "
                "approve / defer / change choices.[/dim]"
            )
        else:
            lines.append(
                "[dim]MissionOS is waiting for current PX4/SITL telemetry. A "
                "natural-language change creates a proposal only; the operator must "
                "review and separately confirm the concrete maneuver before "
                "dispatch.[/dim]"
            )
    else:
        if is_home_robot:
            if status == "completed":
                if summary.get("runtime_recovery_triggered") is True:
                    lines.extend(
                        [
                            "[green]Mission completed after approved Recovery.[/green]",
                            "[dim]Recovery was proposed, explicitly approved, "
                            "completed, and the remaining route finished.[/dim]",
                            "[dim]Core feasibility="
                            f"{rich_escape(_status_text(core_feasibility.get('candidate_status')))}; "
                            "predispatch revalidation="
                            f"{rich_escape(_status_text(core_feasibility.get('predispatch_revalidation_status')))}[/dim]",
                        ]
                    )
                else:
                    lines.extend(
                        [
                            "[green]Mission completed normally.[/green]",
                            "[dim]No Recovery condition was triggered, so Recovery "
                            "Agent created no proposal, approval request, or dispatch.[/dim]",
                        ]
                    )
            else:
                lines.append(
                    f"[dim]status={status} "
                    f"({rich_escape(robot_label)} recovery proposals appear only "
                    "during an active sim route)[/dim]"
                )
        else:
            lines.append(f"[dim]status={status} (proposals are shown only while flying)[/dim]")

    if not pending and checkpoint_status != "dispatching":
        lines.append("")
    tid = task_id or "<task>"
    if is_home_robot:
        if not pending and checkpoint_status != "dispatching":
            lines.append(
                f"[bold]status[/bold] refreshes evidence. [dim]Recovery changes "
                f"become available only after a proposal is displayed "
                f"(task={tid}) · exit: Ctrl-C[/dim]"
            )
    else:
        lines.append(
            "[dim]Type here: describe a recovery change or enter a concrete "
            "command; every "
            "dispatch still uses standard y/N confirmation:[/dim] "
            f"[bold]rtl[/bold] / [bold]land[/bold] / [bold]climb <m>[/bold] / "
            f"[bold]speed <m/s>[/bold] / [bold]reroute <x> <y> (alt)[/bold] / "
            f"[bold]avoid <x> <y> (alt)[/bold]  "
            f"[dim](task={tid}) · exit: Ctrl-C[/dim]"
        )
    border = (
        "cyan"
        if checkpoint_status == "dispatching"
        else "yellow"
        if (pending or (show_proposal and proposal))
        else "cyan"
    )
    return Panel(
        "\n".join(lines),
        title="Runtime Recovery Agent — operator console",
        border_style=border,
    )


def _render_operate_status_line(
    snapshot: dict[str, Any], *, artifacts: dict[str, Any], status: str, task_id: str
) -> Text:
    """One compact live-telemetry line for operate (full map is in `missionos watch`)."""
    if _is_turtlebot3_task_artifacts(artifacts):
        summary = artifacts.get("summary")
        summary = summary if isinstance(summary, dict) else {}
        motion = summary.get("motion_evidence")
        motion = motion if isinstance(motion, dict) else summary
        indoor_map = _turtlebot3_indoor_map_model_from_artifacts(artifacts)
        observed_points = indoor_map.get("observed_points")
        planned_points = indoor_map.get("planned_points")
        dispatched = _as_int(summary.get("segment_dispatch_count")) or 0
        completed = _as_int(summary.get("segment_completion_count")) or 0
        planned = _as_int(summary.get("planned_segment_count")) or 0
        checkpoint = artifacts.get("turtlebot3_recovery_checkpoint")
        checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
        core_feasibility = _turtlebot3_core_feasibility_from_artifacts(artifacts)
        core_status = _status_text(core_feasibility.get("candidate_status"))
        revalidation_status = _status_text(
            core_feasibility.get("predispatch_revalidation_status")
        )
        core_text = (
            f"core={core_status} · revalidation={revalidation_status} · "
            if core_status != "-" or revalidation_status != "-"
            else ""
        )
        phase = (
            "approved Recovery workflow in progress"
            if checkpoint.get("checkpoint_status") == "dispatching"
            else "waiting for Nav2 result"
            if status == "running" and dispatched > completed
            else "recovery decision required"
            if status == "pending" and summary.get("runtime_recovery_triggered") is True
            else status
        )
        robot_label = _turtlebot_robot_label_from_artifacts(artifacts)
        return Text.from_markup(
            f"[dim]task={task_id} · {phase} · "
            f"robot={rich_escape(robot_label)} sim · "
            f"segments={completed}/{planned or '-'} · "
            f"recovery_goal={_status_text(summary.get('recovery_goal_status'))} · "
            f"verification={_status_text(summary.get('recovery_verification_status'))} · "
            f"route={_status_text(summary.get('route_resume_status'))} · "
            f"{core_text}"
            f"motion={_status_text(motion.get('robot_motion_observed'))} · "
            f"odom={_status_text(motion.get('odom_delta_m'))}m · "
            f"observed_samples={len(observed_points) if isinstance(observed_points, list) else 0} · "
            f"planned_waypoints={len(planned_points) if isinstance(planned_points, list) else 0} · "
            "map: `missionos watch`[/dim]"
        )
    designer_snapshot = artifacts.get("mission_designer_live_telemetry_snapshot")
    designer_snapshot = designer_snapshot if isinstance(designer_snapshot, dict) else {}
    latest = designer_snapshot.get("latest_sample")
    latest = latest if isinstance(latest, dict) else {}
    if not snapshot and latest:
        battery_value = latest.get("battery_remaining_percent")
        if battery_value is None:
            battery_value = designer_snapshot.get("battery_remaining_percent")
        local_x = _as_float(latest.get("local_x_m"))
        local_y = _as_float(latest.get("local_y_m"))
        local_z = _as_float(latest.get("local_z_m"))
        position = (
            f"x={local_x:.2f}m y={local_y:.2f}m z={local_z:.2f}m"
            if local_x is not None and local_y is not None and local_z is not None
            else "x/y/z=-"
        )
        return Text.from_markup(
            f"[dim]task={task_id} status={status} · "
            f"battery={_format_percent(battery_value)} · local={position} · "
            f"phase={rich_escape(_status_text(latest.get('phase')))} · "
            f"observed_samples={_status_text(designer_snapshot.get('sample_count'))} · "
            "frame=PX4/Gazebo local XY · map: `missionos map`[/dim]"
        )
    reached = _status_text(_as_int(snapshot.get("mission_reached_seq")))
    total = _status_text(_as_int(snapshot.get("waypoint_total")))
    return Text.from_markup(
        f"[dim]task={task_id} status={status} · "
        f"battery={_battery_display_text(snapshot=snapshot, artifacts=artifacts)} · "
        f"{_operate_altitude_text(snapshot, artifacts)} · "
        f"wp={reached}/{total} · "
        f"progress={_fmt_metres(snapshot.get('progress_m'))} · "
        f"home_dist={_fmt_metres(snapshot.get('distance_to_home_m'))} · "
        "full map in a separate pane: `missionos watch`[/dim]"
    )


def _render_mission_assurance_panel(projection: dict[str, Any]) -> Panel:
    sequence = " -> ".join(projection.get("decision_sequence") or []) or "-"

    def yes_no(value: Any) -> str:
        return "yes" if value is True else "no" if value is False else "-"

    intervention_line = None
    if projection.get("dispatch_prevented_by_mission_assurance") is True:
        intervention_line = (
            "[bold yellow]Intervention:[/bold yellow] feasible recovery proposal "
            "suppressed by MissionAssuranceAgent; dispatch=no; "
            "post-decision re-observation="
            f"{yes_no(projection.get('post_suppression_reobservation_observed'))}."
        )
    lines = [
        f"[bold]Sequence:[/bold] {rich_escape(sequence)}",
        (
            "[cyan]Recovery Agent[/cyan] proposes "
            f"[bold]{rich_escape(_status_text(projection.get('recovery_proposed_action')))}[/bold] "
            f"(model={rich_escape(_status_text(projection.get('recovery_model_id')))}, "
            f"status={rich_escape(_status_text(projection.get('recovery_proposal_status')))})"
        ),
        (
            "[magenta]MissionAssuranceAgent[/magenta] judges "
            f"[bold]{rich_escape(_status_text(projection.get('mission_assurance_response')))}[/bold] "
            f"(model={rich_escape(_status_text(projection.get('mission_assurance_model_id')))}, "
            f"status={rich_escape(_status_text(projection.get('mission_assurance_judgment_status')))})"
        ),
        (
            "[dim]Feasibility: original="
            f"{rich_escape(_status_text(projection.get('original_feasibility')))}; current="
            f"{rich_escape(_status_text(projection.get('current_feasibility')))}; revalidation="
            f"{rich_escape(_status_text(projection.get('revalidation_status')))}; guard="
            f"{rich_escape(_status_text(projection.get('guard_status')))}[/dim]"
        ),
        (
            "[dim]Operator/runtime: route_execution_approval_consumed="
            f"{yes_no(projection.get('route_execution_approval_consumed'))}; "
            "recovery_approval_recorded="
            f"{yes_no(projection.get('recovery_approval_recorded'))}; selected_action="
            f"{rich_escape(_status_text(projection.get('selected_action')))}; state="
            f"{rich_escape(_status_text(projection.get('runtime_state_label')))}; command_ACK="
            f"{yes_no(projection.get('command_ack_observed'))}; final="
            f"{rich_escape(_status_text(projection.get('final_status')))}[/dim]"
        ),
        (
            "[yellow]Authority boundary:[/yellow] agents created no approval or dispatch authority; "
            f"physical_execution={yes_no(projection.get('physical_execution_invoked'))}. "
            "Observed runtime state and command ACK remain separate facts."
        ),
    ]
    if projection.get("fresh_operator_approval_required") is True:
        lines.insert(
            4,
            "[bold yellow]Operator approval required:[/bold yellow] fresh approval for "
            f"{rich_escape(_status_text(projection.get('proposed_action_awaiting_approval')))}. "
            "The route execution approval does not authorize this Recovery action.",
        )
    if projection.get("agent_disagreement_observed") is True:
        lines.insert(
            4,
            "[bold yellow]Agent disagreement:[/bold yellow] Recovery proposed "
            f"{rich_escape(_status_text(projection.get('recovery_no_action_response')))}, "
            "while MissionAssuranceAgent requested "
            f"{rich_escape(_status_text(projection.get('assurance_requested_action')))}. "
            "No action was invented; resolution=operator_escalation.",
        )
    if intervention_line:
        lines.insert(4, intervention_line)
    return Panel(
        "\n".join(lines),
        title="Mission Assurance — Recovery proposal review",
        border_style="magenta",
    )


def _build_operate_status_group(
    task_payload: dict[str, Any],
    *,
    proposal: dict[str, Any] | None,
    pending: dict[str, Any] | None,
    status: str,
    task_id: str,
) -> tuple[Group, str]:
    """Build one read-only operate refresh from a single task payload.

    The fingerprint deliberately contains the checkpoint hash and observation
    count so a revised checkpoint or newly observed motion cannot leave a stale
    proposal panel on screen. Rendering does not create approval or dispatch
    authority.
    """

    if _is_vla_operator_task(task_payload):
        artifacts = _task_artifacts(task_payload)
        fingerprint = json.dumps(
            {
                "status": status,
                "vla_record": artifacts.get("missionos_vla_mission_run_record"),
                "recovery": artifacts.get("missionos_vla_recovery_state"),
                "failure": artifacts.get("physical_ai_execution_failure"),
            },
            sort_keys=True,
            default=str,
        )
        return (
            Group(
                _render_vla_operator_panel(
                    task_payload,
                    title=f"MissionOS Operate · governed VLA · task={task_id}",
                )
            ),
            fingerprint,
        )

    artifacts = _task_artifacts(task_payload)
    snapshot = artifacts.get("missionos_auto_mission_runtime_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    summary = artifacts.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    checkpoint = artifacts.get("turtlebot3_recovery_checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    indoor_map = _turtlebot3_indoor_map_model_from_artifacts(artifacts)
    observed_points = indoor_map.get("observed_points")
    assurance_projection = mission_assurance_projection(artifacts)
    fingerprint = json.dumps(
        {
            "status": status,
            "segment_dispatch_count": summary.get("segment_dispatch_count"),
            "segment_completion_count": summary.get("segment_completion_count"),
            "runtime_recovery_triggered": summary.get("runtime_recovery_triggered"),
            "recovery_action_suggested": summary.get("recovery_action_suggested"),
            "recovery_goal_status": summary.get("recovery_goal_status"),
            "recovery_verification_status": summary.get(
                "recovery_verification_status"
            ),
            "route_resume_status": summary.get("route_resume_status"),
            "checkpoint_status": checkpoint.get("checkpoint_status"),
            "checkpoint_hash": checkpoint.get("checkpoint_hash"),
            "observed_point_count": (
                len(observed_points) if isinstance(observed_points, list) else 0
            ),
            "mission_assurance": assurance_projection,
        },
        sort_keys=True,
        default=str,
    )
    return (
        Group(
            _render_recovery_agent_console(
                task_payload,
                proposal=proposal,
                show_proposal=bool(proposal) and status == "running",
                status=status,
                task_id=task_id,
                pending=pending,
            ),
            *(
                (_render_mission_assurance_panel(assurance_projection),)
                if assurance_projection else ()
            ),
            _render_operate_status_line(
                snapshot,
                artifacts=artifacts,
                status=status,
                task_id=task_id,
            ),
        ),
        fingerprint,
    )


def _operate_robot_from_task_payload(task_payload: dict[str, Any]) -> str:
    """Return the operate help profile derived from this exact task."""

    if _is_vla_operator_task(task_payload):
        return "vla"
    artifacts = _task_artifacts(task_payload)
    return _turtlebot_robot_profile_from_artifacts(artifacts) or "px4"
