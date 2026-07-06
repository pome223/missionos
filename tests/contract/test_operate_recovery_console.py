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
    assert "avoid -0.85 -0.85" in rendered
    assert "reroute 0.75 0.0" in rendered
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
    assert "avoid -0.85 -0.85" in rendered
    assert "reroute 0.75 0.0" in rendered
    assert "TurtleBot4 operate does not expose land/climb/speed/RTL flight controls" in rendered
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

    assert "TurtleBot3 recovery proposals appear only during an active sim route" in rendered
    assert "TurtleBot3 local XY" in rendered
    assert "avoid <x> <y>" in rendered
    assert "reroute <x> <y>" in rendered
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

    assert "TurtleBot4 recovery proposals appear only during an active sim route" in rendered
    assert "TurtleBot4 local XY" in rendered
    assert "avoid <x> <y>" in rendered
    assert "reroute <x> <y>" in rendered
    assert "only while flying" not in rendered
    assert "land" not in rendered
    assert "climb <m>" not in rendered
    assert "speed <m/s>" not in rendered
    assert "[bold]rtl" not in rendered.lower()
    assert "return-to-launch" not in rendered


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
    assert "map_points=2/2" in rendered
    assert "full indoor map" in rendered
    assert "battery=" not in rendered
    assert "alt=" not in rendered
    assert "wp=" not in rendered


def test_turtlebot4_operate_status_line_uses_indoor_map_evidence() -> None:
    artifacts = {
        "summary": {
            "execution_target": "ros2_nav2_turtlebot4_sim",
            "robot_profile": "turtlebot4",
            "robot_motion_observed": True,
            "odom_delta_m": 2.74,
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
    assert "map_points=2/2" in rendered
    assert "full indoor map" in rendered
    assert "battery=" not in rendered
    assert "alt=" not in rendered
    assert "wp=" not in rendered


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
