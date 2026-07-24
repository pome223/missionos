from __future__ import annotations

import pytest

from missionos_cli import cli as missionos_cli

pytestmark = pytest.mark.contract


def test_operate_console_does_not_treat_uncomputed_route_battery_as_feasible() -> None:
    task_payload = {
        "artifacts": {
            "missionos_runtime_recovery_agent_live_bridge": {
                "telemetry_snapshot": {
                    "battery": {
                        "endurance_projection": {
                            "projection_status": "insufficient_observation",
                            "projected_battery_required_percent": 0.0,
                            "projected_arrival_battery_percent": 77.4,
                            "battery_burn_percent_per_km": 12.0,
                        },
                        "return_home_projection": {
                            "projection_status": "insufficient_observation",
                            "distance_to_home_m": 890.0,
                            "projected_return_battery_required_percent": 22.5,
                            "projected_return_arrival_battery_percent": 54.9,
                        },
                    }
                }
            }
        }
    }
    proposal = {
        "action": "operator_review",
        "status": "proposal_skipped",
        "risks": ["terrain_clearance_below_minimum"],
    }

    panel = missionos_cli._render_recovery_agent_console(
        task_payload,
        proposal=proposal,
        show_proposal=True,
        status="running",
        task_id="task_operator_projection",
    )
    rendered = str(panel.renderable)

    assert "Route battery projection is unavailable" in rendered
    assert "RTL battery projection is unavailable" in rendered
    assert "Operator review required" in rendered
    assert "Type here" in rendered
    assert "climb <m>" in rendered
    assert "reroute <x> <y> (alt)" in rendered
    assert "Continuing appears acceptable" not in rendered
    assert "The route appears battery-feasible" not in rendered
    assert "arrival=77.4%" not in rendered
    assert "arrival=54.9%" not in rendered
    assert "route projection=insufficient_observation" in rendered
    assert "RTL projection=insufficient_observation" in rendered


def test_operate_console_surfaces_waiting_avoidance_assessment_with_parameters() -> None:
    task_payload = {
        "task": {
            "task_id": "task_obstacle",
            "status": "running",
            "artifacts": {
                "missionos_runtime_recovery_agent_live_bridge": {
                    "bridge_status": "proposal_skipped",
                    "telemetry_snapshot": {
                        "battery": {
                            "endurance_projection": {
                                "projection_status": "computed",
                                "projected_battery_required_percent": 10.0,
                                "projected_arrival_battery_percent": 75.0,
                                "battery_burn_percent_per_km": 12.5,
                            },
                            "return_home_projection": {
                                "projection_status": "computed",
                                "distance_to_home_m": 1127.0,
                                "projected_return_battery_required_percent": 14.0,
                                "projected_return_arrival_battery_percent": 72.0,
                                "projected_insufficient_for_return_home": False,
                            },
                        }
                    },
                    "runtime_recovery_agent_result": {
                        "runtime_status": "proposal_skipped",
                        "blocking_reasons": ["runtime_recovery_window_waiting"],
                        "assessment": {
                            "assessment_status": "proposal_guardrail_passed",
                            "selected_bounded_action": "avoid_obstacle",
                            "observed_risk_reasons": ["obstacle_or_building_risk"],
                            "proposed_parameters": {
                                "target_x_m": 744.122,
                                "target_y_m": 333.973,
                                "target_altitude_m": 45.0,
                            },
                        },
                    },
                }
            },
        }
    }

    proposal = missionos_cli._agent_proposal_from_task(task_payload)

    assert proposal is not None
    assert proposal["status"] == "proposal_guardrail_passed"
    assert proposal["parameters"]["target_x_m"] == 744.122

    panel = missionos_cli._render_recovery_agent_console(
        task_payload,
        proposal=proposal,
        show_proposal=True,
        status="running",
        task_id="task_obstacle",
    )
    rendered = str(panel.renderable)

    assert "Suggested command" in rendered
    assert "avoid 744.122 333.973 45" in rendered
    assert "asks y/N before dispatch" in rendered

    hint = missionos_cli._operator_recovery_dispatch_hint(
        task_id="task_obstacle",
        action=proposal["action"],
        parameters=proposal["parameters"],
    )
    assert hint is not None
    assert "missionos avoid-obstacle --task-id task_obstacle" in hint
    assert "--target-x-m 744.122 --target-y-m 333.973" in hint
    assert "--altitude-m 45" in hint


def test_operate_status_line_shows_amsl_home_agl_and_destination_climb() -> None:
    artifacts = {
        "missionos_auto_mission_compilation": {
            "planned_route_m": 1000.0,
            "terrain_clearance_target_m": 30.0,
            "terrain_clearance_profile": [
                {
                    "fraction": 0.0,
                    "distance_m": 0.0,
                    "terrain_elevation_m": 570.0,
                    "target_clearance_m": 30.0,
                    "mission_altitude_m": 30.0,
                },
                {
                    "fraction": 1.0,
                    "distance_m": 1000.0,
                    "terrain_elevation_m": 3700.0,
                    "target_clearance_m": 30.0,
                    "mission_altitude_m": 3160.0,
                },
            ],
        }
    }
    snapshot = {
        "battery_remaining_percent": 65.8,
        "terrain_elevation_m": 570.0,
        "terrain_clearance_m": 30.0,
        "terrain_clearance_target_m": 30.0,
        "terrain_clearance_margin_m": 0.0,
        "altitude_above_home_m": 30.0,
        "mission_reached_seq": 8,
        "waypoint_total": 23,
        "progress_m": 100.0,
        "distance_to_home_m": 100.0,
    }

    rendered = missionos_cli._render_operate_status_line(
        snapshot,
        artifacts=artifacts,
        status="running",
        task_id="task_altitude_refs",
    ).plain

    assert "alt=600m AMSL" in rendered
    assert "alt(home)=+30m" in rendered
    assert "AGL=30m/target 30m (margin +0m)" in rendered
    assert "dest=3.73km AMSL/climb +3.13km" in rendered
    assert "home_dist=100m" in rendered


def test_operate_status_line_does_not_render_negative_zero_climb() -> None:
    artifacts = {
        "missionos_auto_mission_compilation": {
            "planned_route_m": 1000.0,
            "terrain_clearance_target_m": 30.0,
            "terrain_clearance_profile": [
                {
                    "fraction": 0.0,
                    "terrain_elevation_m": 4.0,
                    "target_clearance_m": 30.0,
                },
                {
                    "fraction": 1.0,
                    "terrain_elevation_m": 4.0,
                    "target_clearance_m": 30.0,
                },
            ],
        }
    }
    snapshot = {
        "battery_remaining_percent": 77.0,
        "terrain_elevation_m": 4.0,
        "terrain_clearance_m": 30.2,
        "terrain_clearance_target_m": 30.0,
        "terrain_clearance_margin_m": 0.2,
        "altitude_above_home_m": 30.2,
        "mission_reached_seq": 18,
        "waypoint_total": 23,
        "progress_m": 1930.0,
        "distance_to_home_m": 1930.0,
    }

    rendered = missionos_cli._render_operate_status_line(
        snapshot,
        artifacts=artifacts,
        status="running",
        task_id="task_flat_route",
    ).plain

    assert "alt(home)=+30m" in rendered
    assert "dest=34m AMSL/climb +0m" in rendered
    assert "climb -0m" not in rendered
    assert "home_dist=1.93km" in rendered


def test_turtlebot3_operate_help_uses_ground_robot_commands() -> None:
    panel = missionos_cli._operate_console_help_panel(
        "task_turtlebot3",
        robot="turtlebot3",
    )
    rendered = str(panel.renderable)

    assert "latest TurtleBot3 sim state" in rendered
    assert "左へ大きく迂回して" in rendered
    assert "When Recovery stops the robot" in rendered
    assert "approve the displayed recovery proposal" in rendered
    assert "keep stopped; create no dispatch authority" in rendered
    assert "does not expose land/climb/speed/RTL flight controls" in rendered
    assert "return-to-launch" not in rendered
    assert "request land" not in rendered
    assert "climb 45" not in rendered
    assert "speed 7" not in rendered


def test_turtlebot4_operate_help_uses_ground_robot_commands() -> None:
    panel = missionos_cli._operate_console_help_panel(
        "task_turtlebot4",
        robot="turtlebot4",
    )
    rendered = str(panel.renderable)

    assert "latest TurtleBot4 sim state" in rendered
    assert "左へ大きく迂回して" in rendered
    assert "When Recovery stops the robot" in rendered
    assert "TurtleBot4 operate does not expose land/climb/speed/RTL" in rendered
    assert "return-to-launch" not in rendered
    assert "request land" not in rendered
    assert "climb 45" not in rendered
    assert "speed 7" not in rendered


def test_turtlebot3_operate_console_avoids_flight_wording_when_completed() -> None:
    task_payload = {
        "artifacts": {
            "summary": {
                "execution_target": "ros2_nav2_turtlebot3_sim",
            },
            "turtlebot3_home_mission_execution": {
                "execution_target": "ros2_nav2_turtlebot3_sim",
            },
        }
    }

    panel = missionos_cli._render_recovery_agent_console(
        task_payload,
        proposal=None,
        show_proposal=False,
        status="completed",
        task_id="task_turtlebot3",
    )
    rendered = str(panel.renderable)

    assert "Mission completed normally" in rendered
    assert "No Recovery condition was triggered" in rendered
    assert "created no proposal, approval request, or dispatch" in rendered
    assert "Recovery changes become available only after a proposal" in rendered
    assert "only while flying" not in rendered
    assert "land" not in rendered
    assert "climb <m>" not in rendered
    assert "speed <m/s>" not in rendered
    assert "[bold]rtl" not in rendered.lower()
    assert "return-to-launch" not in rendered


def test_turtlebot4_operate_console_avoids_flight_wording_when_completed() -> None:
    task_payload = {
        "artifacts": {
            "summary": {
                "execution_target": "ros2_nav2_turtlebot4_sim",
                "robot_profile": "turtlebot4",
            },
            "turtlebot3_home_mission_execution": {
                "execution_target": "ros2_nav2_turtlebot4_sim",
                "robot_profile": "turtlebot4",
            },
        }
    }

    panel = missionos_cli._render_recovery_agent_console(
        task_payload,
        proposal=None,
        show_proposal=False,
        status="completed",
        task_id="task_turtlebot4",
    )
    rendered = str(panel.renderable)

    assert "Mission completed normally" in rendered
    assert "TurtleBot4 recovery proposals appear only" not in rendered
    assert "only while flying" not in rendered
    assert "land" not in rendered
    assert "climb <m>" not in rendered
    assert "speed <m/s>" not in rendered
    assert "[bold]rtl" not in rendered.lower()
    assert "return-to-launch" not in rendered


def test_turtlebot4_operate_status_line_uses_indoor_map_evidence() -> None:
    artifacts = {
        "summary": {
            "execution_target": "ros2_nav2_turtlebot4_sim",
            "robot_profile": "turtlebot4",
            "robot_motion_observed": True,
            "odom_delta_m": 2.74,
            "recovery_candidate_resolution": {
                "core_adapter_id": "missionos.nav2.action_feasibility.v1",
                "selected_candidate": {
                    "core_action_feasibility_status": "verified_feasible",
                },
            },
        },
        "turtlebot3_recovery_predispatch_revalidation": {
            "revalidation_status": "validated",
        },
        "turtlebot3_indoor_map_model": {
            "execution_target": "ros2_nav2_turtlebot4_sim",
            "robot_profile": "turtlebot4",
            "observed_points": [{"x_m": -2.0}, {"x_m": 0.75}],
            "planned_points": [{"x_m": -2.0}, {"x_m": 0.75}],
        },
    }

    rendered = missionos_cli._render_operate_status_line(
        {},
        artifacts=artifacts,
        status="completed",
        task_id="task_turtlebot4",
    ).plain

    assert "robot=TurtleBot4 sim" in rendered
    assert "motion=True" in rendered
    assert "odom=2.74m" in rendered
    assert "observed_samples=2" in rendered
    assert "planned_waypoints=2" in rendered
    assert "core=verified_feasible" in rendered
    assert "revalidation=validated" in rendered
    assert "battery=" not in rendered
    assert "alt=" not in rendered


def test_turtlebot3_operate_renders_pending_recovery_as_a_clear_decision() -> None:
    checkpoint = {
        "schema_version": "turtlebot3_recovery_checkpoint.v1",
        "checkpoint_status": "awaiting_operator_approval",
        "checkpoint_id": "checkpoint_return_home",
        "checkpoint_hash": "sha256-return-home",
        "recovery_proposal_id": "proposal_return_home",
        "recovery_classification_id": "classification_return_home",
        "selected_action": "return_home",
        "approved_parameters": {"return_home_required": True},
        "robot_profile": "turtlebot3",
        "execution_target": "ros2_nav2_turtlebot3_sim",
    }
    task_payload = {
        "task": {
            "task_id": "task_pending_recovery",
            "kind": "turtlebot3_home_mission_execution",
            "status": "pending",
            "artifacts": {
                "turtlebot3_home_mission_plan": {
                    "robot_profile": "turtlebot3",
                    "execution_target": "ros2_nav2_turtlebot3_sim",
                },
                "turtlebot3_recovery_checkpoint": checkpoint,
                "summary": {
                    "robot_profile": "turtlebot3",
                    "execution_target": "ros2_nav2_turtlebot3_sim",
                    "turtlebot3_recovery_checkpoint": checkpoint,
                    "recovery_proposals": [
                        {
                            "proposal_id": "proposal_return_home",
                            "selected_action": "return_home",
                            "input_observations": {
                                "runtime_failure_source": "nav2_goal_timeout"
                            },
                        }
                    ],
                    "recovery_proposal_classifications": [
                        {
                            "classification_id": "classification_return_home",
                            "execution_class": "requires_human_approval",
                        }
                    ],
                },
            },
        }
    }

    panel = missionos_cli._render_recovery_agent_console(
        task_payload,
        proposal=None,
        show_proposal=False,
        status="pending",
        task_id="task_pending_recovery",
    )
    rendered = str(panel.renderable)

    assert "Robot stopped — recovery decision required" in rendered
    assert "return_home" in rendered
    assert "nav2_goal_timeout" in rendered
    assert "No recovery dispatch has been sent" in rendered
    assert "approve" in rendered
    assert "defer" in rendered
    assert "左へ大きく迂回して" in rendered


def test_turtlebot3_operate_renders_ask_human_as_revision_only() -> None:
    checkpoint = {
        "schema_version": "turtlebot3_recovery_checkpoint.v1",
        "checkpoint_status": "awaiting_operator_approval",
        "checkpoint_id": "checkpoint_ask_human",
        "checkpoint_hash": "sha256-ask-human",
        "recovery_proposal_id": "proposal_ask_human",
        "recovery_classification_id": "classification_ask_human",
        "selected_action": "ask_human",
        "approved_parameters": {},
        "operator_guidance_required": True,
        "robot_profile": "turtlebot3",
        "execution_target": "ros2_nav2_turtlebot3_sim",
    }
    task_payload = {
        "task": {
            "task_id": "task_ask_human",
            "kind": "turtlebot3_home_mission_execution",
            "status": "pending",
            "artifacts": {
                "turtlebot3_home_mission_plan": {
                    "robot_profile": "turtlebot3",
                    "execution_target": "ros2_nav2_turtlebot3_sim",
                },
                "turtlebot3_recovery_checkpoint": checkpoint,
                "summary": {
                    "robot_profile": "turtlebot3",
                    "execution_target": "ros2_nav2_turtlebot3_sim",
                    "turtlebot3_recovery_checkpoint": checkpoint,
                    "recovery_proposals": [
                        {
                            "proposal_id": "proposal_ask_human",
                            "selected_action": "ask_human",
                            "reason": "Request bounded operator guidance.",
                            "llm_invocation_evidence": {
                                "provider": "google_adk_gemini",
                                "model_id": "gemini-test",
                            },
                        }
                    ],
                    "recovery_proposal_classifications": [
                        {
                            "classification_id": "classification_ask_human",
                            "execution_class": "requires_human_approval",
                        }
                    ],
                },
            },
        }
    }

    panel = missionos_cli._render_recovery_agent_console(
        task_payload,
        proposal=None,
        show_proposal=False,
        status="pending",
        task_id="task_ask_human",
    )
    rendered = str(panel.renderable)

    assert "Recovery Agent requested operator guidance" in rendered
    assert "Gemini requested operator guidance" not in rendered
    assert "proposal-only checkpoint cannot dispatch" in rendered
    assert "execute this exact recovery" not in rendered
    assert "approve is unavailable" in rendered
    assert "右へ大きく迂回して障害物を避けて" in rendered


def test_turtlebot3_operate_prefers_revision_candidate_over_stale_direct_copy() -> None:
    checkpoint = {
        "schema_version": "turtlebot3_recovery_checkpoint.v1",
        "checkpoint_status": "awaiting_operator_approval",
        "checkpoint_id": "checkpoint_revision_right",
        "checkpoint_hash": "sha256-revision-right",
        "parent_checkpoint_id": "checkpoint_original_west",
        "recovery_proposal_id": "proposal_revision_right",
        "recovery_classification_id": "classification_revision_right",
        "selected_action": "avoid_obstacle",
        "approved_parameters": {"obstacle_avoidance_required": True},
        "robot_profile": "turtlebot3",
        "execution_target": "ros2_nav2_turtlebot3_sim",
    }
    task_payload = {
        "task": {
            "task_id": "task_revision_right",
            "kind": "turtlebot3_home_mission_execution",
            "status": "pending",
            "artifacts": {
                "turtlebot3_home_mission_plan": {
                    "robot_profile": "turtlebot3",
                    "execution_target": "ros2_nav2_turtlebot3_sim",
                },
                "turtlebot3_recovery_checkpoint": checkpoint,
                "recovery_candidate_resolution": {
                    "resolution_status": "validated",
                    "selected_candidate": {
                        "candidate_id": "obstacle_bypass_west",
                        "path_length_m": 0.45,
                        "maximum_path_cost": 0,
                        "local_maximum_path_cost": 54,
                    },
                },
                "summary": {
                    "robot_profile": "turtlebot3",
                    "execution_target": "ros2_nav2_turtlebot3_sim",
                    "turtlebot3_recovery_checkpoint": checkpoint,
                    "recovery_candidate_resolution": {
                        "resolution_status": "validated",
                        "selected_candidate": {
                            "candidate_id": (
                                "operator_revision_right_wide_avoidance_exit"
                            ),
                            "path_length_m": 1.37,
                            "maximum_path_cost": 0,
                            "local_maximum_path_cost": 87,
                        },
                        "dual_costmap_validated": True,
                        "bounded_retreat_required": False,
                    },
                    "recovery_proposals": [
                        {
                            "proposal_id": "proposal_revision_right",
                            "selected_action": "avoid_obstacle",
                            "proposal_reason": "Operator requested a wide right route.",
                        }
                    ],
                    "recovery_proposal_classifications": [
                        {
                            "classification_id": "classification_revision_right",
                            "execution_class": "requires_human_approval",
                        }
                    ],
                },
            },
        }
    }

    panel = missionos_cli._render_recovery_agent_console(
        task_payload,
        proposal=None,
        show_proposal=False,
        status="pending",
        task_id="task_revision_right",
    )
    rendered = str(panel.renderable)

    assert "operator_revision_right_wide_avoidance_exit" in rendered
    assert "local_max_cost=87" in rendered
    assert "bounded_retreat=False" in rendered
    assert "obstacle_bypass_west" not in rendered


def test_empty_current_candidate_resolution_suppresses_stale_direct_copy() -> None:
    selected = (
        missionos_cli._turtlebot3_recovery_candidate_resolution_from_artifacts(
            {
                "recovery_candidate_resolution": {
                    "selected_candidate": {
                        "candidate_id": "obstacle_bypass_west"
                    }
                },
                "summary": {"recovery_candidate_resolution": {}},
            }
        )
    )

    assert selected == {}


def test_operate_console_parses_explicit_pending_recovery_decisions() -> None:
    assert missionos_cli._parse_operate_console_command("approve").kind == (
        "approve_pending"
    )
    assert missionos_cli._parse_operate_console_command("defer").kind == (
        "defer_pending"
    )


def test_turtlebot3_operate_shows_approved_recovery_dispatching_state() -> None:
    artifacts = {
        "summary": {
            "execution_target": "ros2_nav2_turtlebot3_sim",
            "segment_dispatch_count": 4,
            "segment_completion_count": 4,
            "planned_segment_count": 6,
        },
        "turtlebot3_recovery_checkpoint": {
            "checkpoint_status": "dispatching",
            "selected_action": "avoid_obstacle",
        },
    }
    task_payload = {
        "task": {
            "task_id": "task_dispatching_recovery",
            "status": "running",
            "artifacts": artifacts,
        }
    }

    panel = missionos_cli._render_recovery_agent_console(
        task_payload,
        proposal=None,
        show_proposal=False,
        status="running",
        task_id="task_dispatching_recovery",
    )
    rendered = str(panel.renderable)
    status_line = missionos_cli._render_operate_status_line(
        {},
        artifacts=artifacts,
        status="running",
        task_id="task_dispatching_recovery",
    ).plain

    assert "Approved Recovery workflow is in progress" in rendered
    assert "avoid_obstacle" in rendered
    assert "fresh operator approval is bound" in rendered
    assert "Do not approve the same checkpoint again" in rendered
    assert "may pause while Nav2 replans" in rendered
    assert "approved Recovery workflow in progress" in status_line


def test_recovery_dispatch_uses_long_runtime_timeout(monkeypatch) -> None:
    client = missionos_cli.MissionOSGatewayClient(
        base_url="http://127.0.0.1:18792",
        timeout=45.0,
    )
    recorded: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs):
        recorded.update({"method": method, "path": path, **kwargs})
        return {"summary": {"status": "running"}}

    monkeypatch.setattr(client, "_request", fake_request)

    client.recovery_dispatch(
        task_id="task_recovery",
        recovery_action="avoid_obstacle",
    )

    assert recorded["timeout"] == missionos_cli.SITL_DISPATCH_TIMEOUT


def test_turtlebot3_operate_status_line_uses_indoor_map_evidence() -> None:
    artifacts = {
        "summary": {
            "execution_target": "ros2_nav2_turtlebot3_sim",
            "robot_motion_observed": True,
            "odom_delta_m": 2.74,
        },
        "turtlebot3_indoor_map_model": {
            "observed_points": [{"x_m": -2.0}, {"x_m": 0.75}],
            "planned_points": [{"x_m": -2.0}, {"x_m": 0.75}],
        },
    }

    rendered = missionos_cli._render_operate_status_line(
        {},
        artifacts=artifacts,
        status="completed",
        task_id="task_turtlebot3",
    ).plain

    assert "robot=TurtleBot3 sim" in rendered
    assert "motion=True" in rendered
    assert "odom=2.74m" in rendered
    assert "observed_samples=2" in rendered
    assert "planned_waypoints=2" in rendered
    assert "map: `missionos watch`" in rendered
    assert "battery=" not in rendered
    assert "alt=" not in rendered
    assert "wp=" not in rendered


def test_turtlebot3_operate_status_names_nav2_waiting_phase() -> None:
    artifacts = {
        "summary": {
            "execution_target": "ros2_nav2_turtlebot3_sim",
            "segment_dispatch_count": 2,
            "segment_completion_count": 1,
            "planned_segment_count": 6,
        }
    }

    rendered = missionos_cli._render_operate_status_line(
        {},
        artifacts=artifacts,
        status="running",
        task_id="task_waiting_nav2",
    ).plain

    assert "waiting for Nav2 result" in rendered
    assert "segments=1/6" in rendered


def test_watch_profile_names_amsl_altitude_references() -> None:
    artifacts = {
        "missionos_auto_mission_compilation": {
            "planned_route_m": 1000.0,
            "terrain_clearance_profile": [
                {
                    "fraction": 0.0,
                    "terrain_elevation_m": 4.0,
                    "target_clearance_m": 30.0,
                },
                {
                    "fraction": 1.0,
                    "terrain_elevation_m": 4.0,
                    "target_clearance_m": 30.0,
                },
            ],
        }
    }
    snapshot = {
        "progress_m": 500.0,
        "terrain_elevation_m": 4.0,
        "terrain_clearance_m": 30.0,
        "altitude_above_home_m": 30.0,
    }

    panel = missionos_cli._render_elevation_profile(
        snapshot=snapshot,
        artifacts=artifacts,
    )

    assert panel is not None
    assert panel.title == "Altitude Profile (horizontal=route progress / vertical=AMSL)"
    rendered = str(panel.renderable)
    assert "terrain=4m AMSL" in rendered
    assert "drone=34m AMSL" in rendered
    assert "▁=terrain AMSL" in rendered
    assert "·=target altitude" in rendered
    assert "◆=drone AMSL" in rendered


def test_turtlebot3_watch_uses_recovery_current_pose_after_live_telemetry_stops() -> None:
    rendered = missionos_cli._render_turtlebot3_indoor_map(
        indoor_map={
            "planned_points": [{"x_m": 0.0, "y_m": 0.0}],
            "observed_points": [{"x_m": 1.0, "y_m": 0.0}],
            "current_pose": {"x_m": 2.0, "y_m": 0.0},
            "recovery": {
                "triggered": True,
                "observed_points": [{"x_m": 2.0, "y_m": 0.0}],
            },
            "floor_plan": {},
            "obstacles": [],
        },
        status="blocked",
        task_id="task_recovery_watch",
    )

    panel, hud = list(rendered.renderables)
    map_rows = str(panel.renderable).splitlines()
    robot_row = next(row for row in map_rows if "🐢" in row)
    assert "·" in robot_row
    assert robot_row.index("🐢") > robot_row.index("·")
    assert "recovery_observed=1pts" in hud.plain


def test_turtlebot3_watch_prefers_latest_partial_summary_map() -> None:
    stale = {
        "mission_status": "incomplete",
        "observed_points": [{"x_m": 1.0, "y_m": 0.0}],
        "recovery": {"observed_points": []},
    }
    latest = {
        "mission_status": "running",
        "observed_points": [{"x_m": 1.0, "y_m": 0.0}],
        "recovery": {"observed_points": [{"x_m": 2.0, "y_m": 0.0}]},
        "current_pose": {"x_m": 2.0, "y_m": 0.0},
    }

    selected = missionos_cli._turtlebot3_indoor_map_model_from_artifacts(
        {
            "turtlebot3_indoor_map_model": stale,
            "summary": {"turtlebot3_indoor_map_model": latest},
        }
    )

    assert selected["mission_status"] == "running"
    assert selected["current_pose"] == {"x_m": 2.0, "y_m": 0.0}
    assert len(selected["recovery"]["observed_points"]) == 1
